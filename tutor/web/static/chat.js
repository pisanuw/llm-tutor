/* The chat page's script (plan step 5.5, finding D20: it was 1,300 lines
   inline in chat.html). Loaded by chat.html as a classic, parser-blocking
   script, so tutorApp exists before the deferred Alpine starts. The page
   hands it window.TUTOR_COURSE, SLIDE_VERSION, LECTURE_TITLES,
   TUTOR_STUDENT_ID, TUTOR_TTS, and who the tutor is (TUTOR_NAME,
   TUTOR_INSTRUCTOR, TUTOR_AVATAR, TUTOR_CODE_LANGUAGE) with each course's
   label and lecture count (TUTOR_COURSE_LABELS, TUTOR_LECTURE_COUNTS);
   served as /static/chat.js?v=<content hash>. */
/* global marked, Prism */

// Code blocks are highlighted by Prism.highlightAllUnder once a reply is
// rendered; the vendored marked has no highlight option (finding D20).
marked.setOptions({breaks: true, gfm: true});

// A dropped reply reconnects on its own for this long, holding the Send
// button. After it the bubble keeps what streamed with a Retry, which also
// runs when the browser is back online, so a longer outage needs no lock:
// the server keeps the turn's events for 10 minutes (TURN_TTL in
// tutor/streambuf.py) and the saved reply after that.
const RESUME_WINDOW_MS = 90 * 1000;

// What each predicted score means, as the grader's badge words them.
const SCORE_WORDS = ['does not compile', 'incorrect', 'partial', 'correct', 'excellent'];

// Tutor markdown to safe HTML (plan step 3.5, finding D27): no images (a
// reply never needs one, and an <img> can call out), and links to other
// sites open in a new tab so the chat stays put.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName !== 'A' || !node.hasAttribute('href')) return;
  const href = node.getAttribute('href');
  let external = false;
  try { external = new URL(href, location.href).origin !== location.origin; } catch (e) { /* relative */ }
  if (external) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});
function renderMarkdown(md) {
  return DOMPurify.sanitize(marked.parse(md || ''), {FORBID_TAGS: ['img']});
}

// Whether a lecture's deck is on disk, from the list the page carries
// (window.TUTOR_DECKS = {course: [lecture numbers]}); true when the page
// carries no list, so nothing is hidden by mistake.
function deckAvailable(course, n) {
  const decks = window.TUTOR_DECKS;
  // No list, or no deck at all for the course (a fresh checkout, a failed
  // seed): nothing is hidden, and a missing deck is found when it loads,
  // which is the one case the operator should hear about.
  if (!decks || !decks[String(course)] || !decks[String(course)].length) return true;
  return decks[String(course)].indexOf(Number(n)) !== -1;
}

// Who the tutor is, from courses.yaml by way of the page (chat.html). A
// page that carries no value still reads well.
function tutorName() { return window.TUTOR_NAME || 'the tutor'; }
function instructorName() { return window.TUTOR_INSTRUCTOR || 'your instructor'; }

// A course's label ("AST 101") and lecture count, by id.
function courseLabel(course) {
  const labels = window.TUTOR_COURSE_LABELS || {};
  return labels[String(course)] || String(course || '');
}
function lectureCount(course) {
  const counts = window.TUTOR_LECTURE_COUNTS || {};
  const n = parseInt(counts[String(course)], 10);
  if (n > 0) return n;
  // A course the page does not list: as many lectures as it has titles.
  const titles = (window.LECTURE_TITLES || {})[String(course)] || {};
  return Math.max(1, Object.keys(titles).length);
}

// The course's code language for the code box ("" when the course has none):
// named in the message, and lowercased as the fence's tag (with the alias
// Prism needs).
function codeLanguage() { return (window.TUTOR_CODE_LANGUAGE || '').trim(); }
function codeFenceTag() {
  const lang = codeLanguage().toLowerCase();
  return ({'c++': 'cpp'})[lang] || lang.replace(/[^a-z0-9#+-]/g, '');
}

// The tutor's avatar beside a reply: its image, or its initial in a circle,
// the markup _avatar.html renders on the page.
function avatarHtml() {
  const a = window.TUTOR_AVATAR || {};
  const name = escHtml(String(a.name || tutorName()));
  if (a.url) {
    return `<img src="${escHtml(String(a.url))}" alt="${name}"
          class="w-8 h-8 shrink-0 rounded-full object-cover">`;
  }
  return `<span role="img" aria-label="${name}"
          class="w-8 h-8 shrink-0 rounded-full bg-blue-600 text-white font-bold flex items-center justify-center select-none leading-none text-sm">${escHtml(String(a.initial || '?'))}</span>`;
}

// Links the tutor writes (review step 0.3, F7): a same-origin link to the
// slides, the lectures, the book, the map or an explorable opens in a new
// tab so the conversation stays put; a /slides link whose deck is not on
// disk is pointed at the lecture list instead of a 404.
const _NEW_TAB_PREFIXES = ['/slides/', '/lectures', '/book/', '/explorables/', '/map', '/review'];
const _DECK_LINK = /^\/slides\/([A-Za-z0-9]{1,8})\/lecture-(\d{2})\//;
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName !== 'A' || !node.hasAttribute('href')) return;
  let url;
  try { url = new URL(node.getAttribute('href'), location.href); } catch (e) { return; }
  if (url.origin !== location.origin) return;
  const m = _DECK_LINK.exec(url.pathname);
  if (m && !deckAvailable(m[1], parseInt(m[2], 10))) {
    node.setAttribute('href', '/lectures');
    node.setAttribute('title', 'Those slides are not available yet; the lecture list opens instead.');
  }
  if (_NEW_TAB_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener');
  }
});

// Retry buttons on a dropped reply and on a conversation that failed to load.
const RETRY_BTN = 'mt-2 inline-flex items-center justify-center min-h-[44px] px-4 rounded-lg ' +
                  'bg-amber-600 text-white text-sm font-medium hover:bg-amber-700';

// Activity events (docs/activity-analytics-plan-2026-10.md): track.js, loaded
// by base.html after this script, batches them to /api/events. A no-op when
// tracking is off or the script has not run yet; it never throws into the page.
function trackEvent(name, fields) {
  try { if (window.tutorTrack) window.tutorTrack(name, fields || {}); } catch (e) { /* never */ }
}

// Tell the slides frame whether its panel is on screen (a phone shows one
// panel at a time): the deck counts dwell only while it is visible.
function notifyDeckPanel(visible) {
  try {
    const frame = document.querySelector('#slide-container iframe');
    if (frame && frame.contentWindow) {
      frame.contentWindow.postMessage({type: 'tutor-panel', visible: !!visible}, location.origin);
    }
  } catch (e) { /* a deck that is not there */ }
}

function tutorApp() {
  return {
    course: window.TUTOR_COURSE,
    deckCourse: window.TUTOR_COURSE,   // the slides panel's course: a slide may come from another one
    lectureNum: 1,
    slideUrl: '',
    deckMissing: false,    // the deck URL answered with something other than a deck
    deckKnownMissing: false,   // the page's deck list says there is none: never requested
    streaming: false,
    historySeq: 0,         // the latest loadHistory call; older results are dropped
    shownCourse: null,     // the course whose conversation the bubbles show
    showCode: false,
    trackLabel: '',
    starters: [],          // /chat/starters chips for the current course
    showStarters: false,   // strip visible (auto on a first conversation)
    showHelp: false,       // ? modal
    mobilePanel: 'chat',   // phone-only panel toggle: 'chat' | 'slides'
    quotaNote: '',         // the tutor budget line under the input (tutor/quotas.py)
    inputNote: '',         // why the box's text was not sent (too long, R22)
    historyRows: [],       // the saved conversation last shown (R31 reads its end)
    activeCtx: null,       // the reply this page is receiving, for its bubble after a history reload
    quotaBlocked: false,
    slideBadge: false,     // phone: a slide arrived while the chat panel showed
    photoBusy: false,
    photoError: '',
    photoExpired: false,   // the photo's 401: the error gets a sign-in link
    touchScreen: window.matchMedia('(pointer: coarse)').matches,
    predictOpen: false,
    listening: false,
    speechAvailable: !!(window.SpeechRecognition || window.webkitSpeechRecognition),
    _recognizer: null,

    // Photo -> code (ROADMAP capability 7). Normalize client-side to a JPEG
    // capped at 1568px: handles iPhone HEIC (Safari decodes it into the
    // canvas), keeps uploads small, and keeps vision-token cost down. The
    // transcription only fills the box; the student reviews before grading.
    async photoToCode(ev) {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (!file || this.photoBusy) return;
      this.photoBusy = true;
      this.photoError = '';
      try {
        const blob = await this.normalizePhoto(file);
        const fd = new FormData();
        fd.append('image', blob, 'photo.jpg');
        const r = await fetch('/chat/transcribe-photo',
                              { method: 'POST', body: fd, credentials: 'include' });
        if (r.status === 401) {
          // Not a photo problem (R28): the session ended. The question and
          // the code are kept, each for its own box, for when the page
          // comes back.
          keepDrafts(this.course);
          this.photoExpired = true;
          this.photoError = 'Your session has ended, so the photo was not read. Sign in again, then choose it once more.';
          return;
        }
        this.photoExpired = false;
        const j = await r.json().catch(() => ({}));
        const box = document.getElementById('code-input');
        if (j.ok && j.code) {
          box.value = box.value.trim() ? box.value.replace(/\s+$/, '') + '\n\n' + j.code : j.code;
          box.focus();
        } else {
          this.photoError = j.error || 'Could not read code in that photo. Try a sharper, closer shot.';
        }
      } catch (e) {
        this.photoError = 'Could not open that photo. Try another picture.';
      } finally {
        this.photoBusy = false;
      }
    },

    normalizePhoto(file) {
      return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
          const maxDim = 1568;
          const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
          const canvas = document.createElement('canvas');
          canvas.width = Math.round(img.width * scale);
          canvas.height = Math.round(img.height * scale);
          canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
          URL.revokeObjectURL(url);
          canvas.toBlob(
            b => b ? resolve(b) : reject(new Error('encode failed')),
            'image/jpeg', 0.85);
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('unreadable image')); };
        img.src = url;
      });
    },

    // Dictation via the browser's speech recognition (no server, no cost);
    // the button hides itself where the API is unavailable.
    toggleDictation() {
      if (this.listening) {
        if (this._recognizer) this._recognizer.stop();
        return;
      }
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) return;
      const rec = new SR();
      rec.lang = 'en-US';
      rec.interimResults = false;
      rec.continuous = false;
      rec.onresult = (e) => {
        const text = Array.from(e.results).map(r => r[0].transcript).join(' ').trim();
        if (text) {
          const box = document.getElementById('msg-input');
          box.value = (box.value.trim() ? box.value.trim() + ' ' : '') + text;
          box.focus();
        }
      };
      rec.onend = () => { this.listening = false; this._recognizer = null; };
      rec.onerror = () => { this.listening = false; this._recognizer = null; };
      this._recognizer = rec;
      this.listening = true;
      rec.start();
    },

    // Desktop: Enter sends. Soft keyboards have no Shift+Enter, so on touch
    // devices Enter inserts a newline and the Send button sends.
    // Desktop: Enter sends, Shift+Enter (or any modifier, or an input
    // method composing) keeps the newline. The template's ".exact" was not
    // an Alpine 3 modifier, so Enter never sent (the Stage 3 review).
    enterKey(e) {
      if (window.matchMedia('(pointer: coarse)').matches) return;
      if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey || e.isComposing) return;
      e.preventDefault();
      this.sendMessage();
    },
    // Phone: swap the chat and slides panels; seeing the slides clears the dot.
    togglePanel() {
      this.mobilePanel = this.mobilePanel === 'chat' ? 'slides' : 'chat';
      if (this.mobilePanel === 'slides') this.slideBadge = false;
      trackEvent('panel_toggle', {to: this.mobilePanel, lecture_num: this.lectureNum});
      notifyDeckPanel(this.deckOnScreen());
    },
    openSlides() {
      this.mobilePanel = 'slides';
      this.slideBadge = false;
      trackEvent('panel_toggle', {to: 'slides', lecture_num: this.lectureNum});
      notifyDeckPanel(true);
    },
    // Whether the slides panel is on screen: always on a wide screen, on a
    // phone only when it is the panel showing.
    deckOnScreen() {
      return !window.matchMedia('(max-width: 767px)').matches || this.mobilePanel === 'slides';
    },
    // The lecture dropdown: the student chose a deck themselves.
    pickLecture() {
      trackEvent('lecture_select', {from: this.shownLecture || undefined, to: this.lectureNum,
                                    lecture_num: this.lectureNum});
      this.loadSlide();
    },

    // Focus goes back to the Help button that opened the dialog. The header
    // renders its links twice (inline and in the menu sheet), so a single
    // x-ref could name the hidden copy.
    openHelp() {
      this._helpOpener = document.activeElement;
      this.showHelp = true;
      this.$nextTick(() => { if (this.$refs.helpClose) this.$refs.helpClose.focus(); });
    },
    closeHelp() {
      if (!this.showHelp) return;
      this.showHelp = false;
      const back = this._helpOpener;
      this._helpOpener = null;
      if (back && back.isConnected && back.offsetParent !== null) back.focus();
    },
    // Tab and Shift+Tab cycle inside the open help dialog.
    trapHelpFocus(e) {
      const box = this.$refs.helpDialog;
      if (!box) return;
      const items = [...box.querySelectorAll(
        'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])')]
        .filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const inside = box.contains(document.activeElement);
      if (e.shiftKey && (!inside || document.activeElement === first)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (!inside || document.activeElement === last)) {
        e.preventDefault();
        first.focus();
      }
    },

    courseLabel(course) { return courseLabel(course); },
    lectureCount(course) { return lectureCount(course); },

    lectureLabel(n) {
      const titles = (window.LECTURE_TITLES && window.LECTURE_TITLES[this.deckCourse]) || {};
      const t = titles[n] || titles[String(n)];
      const label = t ? `L${n}: ${t}` : `Lecture ${n}`;
      // A lecture whose deck is not on disk is still a lecture to ask about,
      // but the picker says so instead of opening a sentence (review step 0.3).
      return deckAvailable(this.deckCourse, n) ? label : `${label} (no slides yet)`;
    },

    async init() {
      // Read URL params set by the map's "Chat about this" link. The course
      // is the one the server resolved from ?course= (window.TUTOR_COURSE):
      // the raw parameter, hand-edited to 999, failed every request the page
      // made (docs/bulletproofing-plan-2026-10.md, R14).
      const params = new URLSearchParams(window.location.search);
      this.deckCourse = this.course;
      const pressed = forgetOneShotParams(params, this.course);
      // Lectures run 1 to the course's count; anything else keeps lecture 1
      // (was lecture-NaN).
      const lec = parseInt(params.get('lecture_num'), 10);
      if (lec >= 1 && lec <= lectureCount(this.course)) this.lectureNum = lec;
      watchMessagesScroll();
      this.loadSlide();
      this.loadTrack();
      this.loadProgress();
      this.loadQuota();
      setInterval(() => this.loadProgress(), 60000);
      setInterval(() => this.loadQuota(), 5 * 60000);
      // While a used-up quota holds the page, what is typed outlives each
      // load, not only the first (the Stage 4 review: a refused question
      // came back once, then was gone).
      window.addEventListener('pagehide', () => {
        if (this.quotaBlocked) keepDrafts(this.course, 7 * 24 * 3600 * 1000);
      });

      // "Teach me" from the map: try the pre-generated (cached) opener first —
      // it renders instantly with no API call. If served, it was persisted, so
      // loadHistory() shows it. On a cache miss, fall through to the live path
      // (pre-fill "Explain: <topic>" for the student to send).
      const goal = params.get('goal');
      const topic = params.get('topic');
      let served = false;
      // A goal still in the address is a fresh press on the map (a reload no
      // longer carries it), so the server serves the opener again even when
      // it is in the recent history.
      if (goal) served = await this.serveCachedOpener(goal, params.get('lecture_num'), params.get('depth'), pressed);

      // A reply still being written (a reload, iOS reloading a background
      // tab, a second tab) shows under its question as it comes (R30).
      this.loadHistory().then(() => this.reattachRunning());
      if (topic && !served) {
        // The map's Quiz me and Review buttons ask for those, not an
        // explanation (plan step 3.5, finding D6).
        const ask = {quiz: `Quiz me on: ${topic}`, review: `Help me review: ${topic}`}[params.get('mode')]
                    || `Explain: ${topic}`;
        this.$nextTick(() => {
          const inp = document.getElementById('msg-input');
          if (inp) { inp.value = ask; inp.focus(); }
        });
      }
      // Code kept from a lapsed session or a refusal goes back in its panel.
      const codeDraft = takeCodeDraft(this.course);
      if (codeDraft) {
        this.$nextTick(() => {
          const code = document.getElementById('code-input');
          const problem = document.getElementById('problem-input');
          if (code && !code.value) code.value = codeDraft.code;
          if (problem && !problem.value) problem.value = codeDraft.problem || '';
          this.showCode = true;
        });
      }
      // A message typed when the session had expired comes back after login.
      const draft = takeDraft(this.course);
      if (draft) {
        this.$nextTick(() => {
          const inp = document.getElementById('msg-input');
          if (inp && !inp.value) { inp.value = draft; inp.focus(); }
        });
      }
    },

    async serveCachedOpener(goal, lectureNum, depth, again) {
      try {
        const fd = new FormData();
        fd.append('goal', goal);
        fd.append('course', this.course);
        if (lectureNum) fd.append('lecture_num', lectureNum);
        if (depth) fd.append('depth', depth);
        if (again) fd.append('again', '1');
        const r = await fetch('/chat/teachme', { method: 'POST', body: fd, credentials: 'include' });
        if (!r.ok) return false;
        const j = await r.json();
        if (j && j.cached) trackEvent('teachme_start', {lecture_num: lectureNum || undefined});
        return !!(j && j.cached);
      } catch { return false; }
    },

    onCourseChange() {
      trackEvent('course_switch', {from: this.deckCourse, to: this.course, course: this.course});
      window.TUTOR_COURSE = this.course;   // later events carry the course now showing
      this.deckCourse = this.course;
      this.persistCourse();
      this.loadSlide();
      this.loadTrack();
      this.loadHistory().then(() => this.reattachRunning());
      this.loadProgress();
    },

    // Remember the course server-side so a plain /chat visit (bookmark, the
    // header link, the post-login redirect) reopens on it. Fire-and-forget.
    persistCourse() {
      fetch('/path/course', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({course: this.course}),
        credentials: 'include',
      }).catch(() => {});
    },

    // The tutor budget (tutor/quotas.py): a note under the input from 80% of
    // any quota, the blocking message when one is used up. Best-effort.
    async loadQuota() {
      try {
        const r = await fetch('/chat/quota', {credentials: 'include', cache: 'no-store'});
        if (r.status === 401) {
          // The session ended: a held Send would never come back (the Stage 3
          // review). Released, it reaches the expired path, which keeps the
          // text and links to sign in.
          this.quotaBlocked = false;
          this.quotaNote = '';
        } else if (r.ok) {
          const q = await r.json();
          this.quotaNote = q.note || '';
          this.quotaBlocked = !!q.blocked;
        }
      } catch { /* keep the last note */ }
      this.recheckQuota();
    },

    // While Send waits on a used-up quota, look again each minute, so it
    // comes back soon after the reset (or an admin's raise).
    recheckQuota() {
      clearTimeout(this._quotaRecheck);
      if (this.quotaBlocked) this._quotaRecheck = setTimeout(() => this.loadQuota(), 60000);
    },

    // A message not taken: the student's own words (ctx.boxText) go back in
    // the box, and the bubble that showed them goes. When the box already
    // holds new text the bubble stays instead, so the words never vanish
    // (the Stage 3 review). A code send reopens its panel: the code is
    // still there, and only the question typed in front of it comes back.
    giveBack(ctx) {
      const box = document.getElementById('msg-input');
      const words = ctx.boxText || '';
      let back = !words;
      if (words && box && !box.value.trim()) { box.value = words; back = true; }
      if (back && ctx.userEl) ctx.userEl.remove();
      // After the panel's closing animation: reopened during it, Alpine's
      // collapse stays shut while showCode reads true.
      if (ctx.code) setTimeout(() => { this.showCode = true; }, 400);
      return back;
    },

    // Progress + "What's next" for the course being shown: under the slides,
    // and on phones also in the Ideas sheet (the slides are out of sight there).
    async loadProgress() {
      const panels = ['progress-panel', 'progress-panel-m']
        .map((id) => document.getElementById(id)).filter(Boolean);
      if (!panels.length) return;
      try {
        const r = await fetch(`/chat/progress?course=${this.course}`, {credentials: 'include'});
        if (!r.ok) return;
        const html = await r.text();
        for (const panel of panels) panel.innerHTML = html;
      } catch { /* best-effort; the panels keep their last content */ }
    },

    async loadTrack() {
      try {
        const r = await fetch(`/path/current?course=${this.course}`, {credentials: 'include'});
        if (r.ok) this.trackLabel = (await r.json()).title || '';
      } catch { /* best-effort; the chip falls back to "Lecture order" */ }
    },

    // Replace the bubbles with the saved conversation of `course`. Fetch
    // first and clear only on success: a reload that fails (offline after a
    // dropped reply) must never wipe what the student can already read.
    // On failure a notice with a Retry takes the place of the silent empty
    // chat, unless the caller shows its own (notice: false). Returns true
    // when the conversation loaded.
    async loadHistory({course = this.course, notice = true, restoreActive = true} = {}) {
      const seq = ++this.historySeq;
      let rows = null;
      let status = 0;
      try {
        const r = await fetch(`/chat/history?course=${course}`,
                              {credentials: 'include', cache: 'no-store'});
        status = r.status;
        if (r.ok) rows = await r.json();
      } catch { /* offline or unreachable: rows stays null */ }
      if (seq !== this.historySeq) return Array.isArray(rows);   // a newer reload owns the screen
      if (!Array.isArray(rows)) {
        // Another course's bubbles would mislead under this course's name.
        if (this.shownCourse !== course) {
          clearDynamicMessages();
          this.shownCourse = course;
        }
        if (notice) {
          showHistoryNotice(status, course,
            () => this.loadHistory({course}).then((ok) => { if (ok) this.reattachRunning(); }));
        }
        return false;
      }
      clearDynamicMessages();
      this.shownCourse = course;
      this.historyRows = rows;
      for (const m of rows) {
        if (m.role === 'user') appendMessage('user', m.content);
        else                   appendAssistantMessage(m.content, m.id, m.rating);
      }
      // The reply this page is still receiving keeps its bubble: a course
      // switch away and back, or a Retry, used to drop it, and the rest of
      // the stream went nowhere (the Stage 4 review).
      const c = this.activeCtx;
      if (restoreActive && c && !c.finished && c.course === course && !document.getElementById(c.bubbleId)) {
        appendStreamingBubble(c.bubbleId);
        if (c.reattached) document.getElementById(c.bubbleId).dataset.turn = c.turnId;
        if (c.rawText) updateStreamingBubble(c.bubbleId, c.rawText);
      }
      scrollMessages(true);
      // First conversation in this course: offer suggestions unasked.
      if (rows.length === 0) this.openStarters();
      else this.showStarters = false;
      return true;
    },

    async loadStarters() {
      try {
        const r = await fetch(`/chat/starters?course=${this.course}`, {credentials: 'include'});
        this.starters = r.ok ? ((await r.json()).starters || []) : [];
      } catch { this.starters = []; }
    },

    async openStarters() {
      this.showStarters = true;
      await this.loadStarters();
      scrollMessages();
    },

    toggleStarters() {
      if (this.showStarters) { this.showStarters = false; return; }
      this.openStarters();
    },

    // A tapped suggestion: the personalized "Teach me" chip tries the cached
    // opener first (no API call); everything else is sent as a normal turn.
    async pickStarter(s) {
      if (this.streaming) return;
      this.showStarters = false;
      trackEvent('starter_click', {index: Math.max(0, this.starters.indexOf(s)),
                                   lecture_num: s.lecture_num || undefined});
      if (s.goal_id && s.modality) recordPathEvent(s.goal_id, this.course, s.modality, 'student');
      if (s.opener && s.goal_id) {
        const served = await this.serveCachedOpener(s.goal_id, s.lecture_num, null, true);
        if (served) { await this.loadHistory(); return; }
      }
      this.nextKind = s.opener ? 'teachme' : 'starter';
      await this.sendMessage(s.message);
    },

    loadSlide() {
      const lec = String(this.lectureNum).padStart(2, '0');
      // Append the per-deploy version token so a redeployed deck is never
      // served from a stale browser/CDN cache entry (the old "lecture-1 is
      // black" cause). Same URL across a deploy => still cacheable within it.
      const v = window.SLIDE_VERSION ? `?v=${window.SLIDE_VERSION}` : '';
      this.slideUrl = `/slides/${this.deckCourse}/lecture-${lec}/index.html${v}`;
      this.shownLecture = this.lectureNum;
      // A deck the page already knows is not there (the picker says "no
      // slides yet") is never requested: the request would be a 404 row
      // and a dead-link email for the operator. The frame stays out and the
      // sentence shows; a deck the page expected but cannot load is still
      // caught by relayoutDeck.
      this.deckKnownMissing = !deckAvailable(this.deckCourse, this.lectureNum);
      this.deckMissing = this.deckKnownMissing;
      if (this.deckKnownMissing) {
        trackEvent('deck_missing', {course: this.deckCourse, lecture_num: this.shownLecture});
      }
    },

    relayoutDeck(frame) {
      // A missing deck answers with JSON (a frame) or, when the browser did
      // not say it was a frame, the site's error page: say so instead.
      const wasMissing = this.deckMissing;
      try {
        const doc = frame.contentDocument;
        this.deckMissing = !!doc && (doc.contentType !== 'text/html' ||
                                     !!doc.querySelector('[data-error-page]'));
        // The session ran out (signed_out_frame.html): its link signs in the
        // whole page, so it comes back to this course and lecture, and a
        // question typed and not sent is kept as a draft, as the Send path
        // keeps one (docs/bulletproofing-plan-2026-10.md, R20).
        const out = doc && doc.querySelector('[data-signed-out] a');
        if (out) {
          out.href = '/auth/login?next=' + encodeURIComponent(
            `/chat?course=${this.course}&lecture_num=${this.lectureNum}`);
          const keep = () => {
            const box = document.getElementById('msg-input');
            if (box && box.value.trim()) saveDraft(box.value, this.course);
          };
          keep();
          out.addEventListener('click', keep);
        }
      } catch (e) { this.deckMissing = false; }
      // The instructor learns of a deck that is not there (review step 0.2, F5).
      if (this.deckMissing && !wasMissing) {
        trackEvent('deck_missing', {course: this.deckCourse, lecture_num: this.shownLecture});
      }
      // Reveal (embedded:false) computes its scale once on init. On the first
      // page load the Tailwind CDN may not have sized the iframe's container
      // yet, so the initial deck (e.g. lecture-01) initializes at ~0 height and
      // renders blank. Nudge Reveal to re-layout once the iframe has real
      // dimensions. Same-origin; a no-op for decks that already rendered fine.
      const nudge = () => {
        try { frame.contentWindow && frame.contentWindow.dispatchEvent(new Event('resize')); }
        catch (e) { /* not ready / cross-origin — ignore */ }
      };
      requestAnimationFrame(nudge);
      setTimeout(nudge, 300);
      setTimeout(() => notifyDeckPanel(this.deckOnScreen()), 350);
    },

    showSlide(url, title, num) {
      // Called when agent returns a slide reference — jump to that lecture's deck.
      // url is the deck itself, /slides/AST101/lecture-04/index.html; we only need
      // the course + lecture out of it (older clients may still send a
      // /slides/AST101/lecture-04/frames/slide-07.png URL — the regex handles both).
      const m = url.match(/\/slides\/([A-Za-z0-9]{1,8})\/lecture-(\d+)\//);
      if (m) {
        // Only the slides panel follows a slide from another course; the
        // chat stays in its own (it used to switch the course silently).
        this.deckCourse = m[1];
        this.lectureNum = parseInt(m[2], 10);
        this.loadSlide();
      }
      document.getElementById('slide-caption').textContent =
        title ? `${title} — slide ${num}` : `Slide ${num}`;
      // A phone shows one panel at a time: mark the Slides toggle.
      if (this.mobilePanel === 'chat' && window.matchMedia('(max-width: 767px)').matches) {
        this.slideBadge = true;
      }
    },

    async submitCode() {
      const code = document.getElementById('code-input').value.trim();
      if (!code) return;
      // First tap opens the one-question predict strip; the actual send
      // happens from pickPrediction. The moment BEFORE feedback is where
      // the metacognitive signal lives.
      this.predictOpen = true;
    },

    // The "Not now" button under a prerequisite check. It sends its own
    // words: anything typed in the box stays there (R24).
    sendNotNow(text) {
      if (this.streaming) return;
      this.nextKind = 'not_now';
      this.sendMessage(text);
    },

    async pickPrediction(n) {
      this.predictOpen = false;
      if (this.quotaBlocked) {
        // The panel stays open with the code: closing it read as sent.
        this.inputNote = this.quotaNote;
        announce(this.quotaNote);
        return;
      }
      const code    = document.getElementById('code-input').value.trim();
      const problem = document.getElementById('problem-input').value.trim();
      if (!code) return;

      // Build a message that triggers evaluate_code tool. The fence names
      // the course's language when it has one (window.TUTOR_CODE_LANGUAGE).
      const lang = codeLanguage();
      const fence = '```' + codeFenceTag();
      const msg = problem
        ? `Please evaluate my code for this problem: "${problem}"\n\n${fence}\n${code}\n\`\`\``
        : `Please evaluate my ${lang ? lang + ' ' : ''}code:\n\n${fence}\n${code}\n\`\`\``;
      // A question typed in the box goes in front of the code, not under
      // it: Evaluate used to replace it (docs/bulletproofing-plan-2026-10.md, R24).
      const typed = document.getElementById('msg-input').value.trim();
      const full = typed ? `${typed}\n\n${msg}` : msg;
      if (this.tooLong(full)) return;

      this.showCode = false;
      trackEvent('code_submit', {chars: code.length, predicted: (n === null ? undefined : n),
                                 lecture_num: this.lectureNum});
      this.nextKind = 'code';
      // The prediction rides this send only (a refusal above used to leave it
      // for the next, unrelated message), and only the typed part goes back
      // in the box on a refusal: the code is still in its own box (the Stage
      // 3 review: a retried Evaluate sent the code twice).
      await this.sendMessage(full, {takesBox: true, boxText: typed, code: true,
                                    predicted: (n === null ? null : n)});
    },

    // Over the limit, the text stays where it is with a sentence naming its
    // size: a paste of a whole project was sent, billed on every round and
    // resent with every later turn (docs/bulletproofing-plan-2026-10.md, R22).
    tooLong(text) {
      const max = window.TUTOR_MAX_MESSAGE || 30000;
      if (text.length <= max) return false;
      this.inputNote = `That is ${text.length.toLocaleString('en-US')} characters, and the tutor ` +
        `takes up to ${max.toLocaleString('en-US')} at once (about 500 lines of code). Send the part ` +
        `your question is about, or split it into two messages.`;
      announce(this.inputNote);
      return true;
    },

    // Sends what is in the box, or ``given`` (a starter, "Not now", a code
    // submission) without touching the box unless ``takesBox`` says the box's
    // text went into ``given`` (R24: a starter used to replace a typed question).
    async sendMessage(given = null, {takesBox = false, boxText = null, code = false, predicted = null} = {}) {
      const input = document.getElementById('msg-input');
      const fromBox = typeof given !== 'string';
      const text  = (fromBox ? input.value : given).trim();
      if (!text || this.streaming) return;
      // Every way of sending waits on a used-up quota, not only the button.
      if (this.quotaBlocked) { announce(this.quotaNote); return; }
      if (this.tooLong(text)) return;
      this.inputNote = '';

      stopTTS();  // don't let a prior reply keep narrating over the new turn
      // A "Not now" offer belongs to the check it came with; a new message
      // answers or passes it.
      document.querySelectorAll('[data-not-now]').forEach((n) => n.remove());
      if (fromBox || takesBox) input.value = '';
      this.streaming = true;
      this.showStarters = false;

      // Append user bubble
      const userEl = appendMessage('user', text);
      scrollMessages(true);

      // Append streaming assistant bubble (empty, will fill as tokens arrive)
      const bubbleId = 'bubble-' + Date.now();
      appendStreamingBubble(bubbleId);

      const formData = new FormData();
      formData.append('message',     text);
      formData.append('course',      this.course);
      formData.append('lecture_num', this.lectureNum);
      if (predicted !== null) formData.append('predicted_score', predicted);

      // Turn context shared between the first request and any resumes after a
      // dropped connection: the server buffers every event per turn
      // (tutor/streambuf.py), so we can reconnect and continue at `count`.
      // boxText: the student's own words that go back in the box when the
      // message is not taken (the box's text; the question typed in front of
      // an Evaluate; none for a starter or Not now).
      const ctx = { bubbleId, rawText: '', turnId: null, count: 0, finished: false, text, userEl,
                    course: this.course, sentAt: Date.now(), replyChars: 0, code,
                    boxText: fromBox ? text : (takesBox ? (boxText || '') : null) };
      this.activeCtx = ctx;
      const kind = this.nextKind || 'typed';
      this.nextKind = null;
      trackEvent('question_sent', {chars: text.length, kind, lecture_num: this.lectureNum,
                                   course: this.course});

      try {
        try {
          const resp = await fetch('/chat/message', {
            method: 'POST',
            body: formData,
            credentials: 'include',
          });

          if (!resp.ok) {
            if (resp.status === 401) {
              // The session expired: nothing was sent. Put the student's words
              // back in the box and keep a copy that survives the login round
              // trip; a starter's words never replace what was typed.
              if (ctx.boxText && !input.value.trim()) input.value = ctx.boxText;
              keepDrafts(this.course);
              trackEvent('reply_done', {outcome: 'expired', value_ms: Date.now() - ctx.sentAt});
              const next = encodeURIComponent('/chat?course=' + this.course);
              finishBubble(bubbleId,
                `<div class="text-amber-700 text-sm" role="alert">Your session expired, so this ` +
                `message was not sent. <a class="underline font-medium" href="/auth/login?next=${next}">` +
                `Log in again</a>; your message will be back in the box.</div>`);
              return;
            }
            // Nothing was taken: the message goes back in the box, with a
            // plain sentence instead of "Error 500: Internal Server Error",
            // or the server's own when it says why (a message too long).
            this.giveBack(ctx);
            trackEvent('reply_done', {outcome: 'error', value_ms: Date.now() - ctx.sentAt});
            let said = '';
            try { said = (await resp.json()).error || ''; } catch { /* not JSON */ }
            finishBubble(bubbleId,
              `<div class="text-amber-800 text-sm" role="alert">` +
              (said ? `${escHtml(said)} It is back in the box.`
                : (resp.status >= 500
                  ? 'The tutor is having trouble right now, so this message was not sent. '
                  : 'The tutor could not take that message. ') +
                  `It is back in the box: try again in a minute. (Error ${resp.status})`) + `</div>`);
            return;
          }
          await this.pumpEvents(resp, ctx);
        } catch { /* the connection dropped: reconnect below */ }
        // Every turn ends with done (or busy or error). A stream that failed
        // or closed without one (a proxy ending it cleanly) dropped.
        if (!ctx.finished) await this.resumeTurn(ctx);
      } finally {
        this.streaming = false;
        if (this.activeCtx === ctx) this.activeCtx = null;
        scrollMessages();
        this.loadQuota();
        if (ctx.reattachTo) setTimeout(() => this.reattach(ctx.reattachTo), 0);
      }
    },

    // Reconnect to a turn whose connection dropped mid-stream. Retries for
    // RESUME_WINDOW_MS, waking immediately when the browser regains
    // connectivity: a real drop (airplane mode, elevator, dead zone) routinely
    // outlasts any few-second retry burst, which is why the first version of
    // this gave up too early. Each resume continues from ctx.count, so no
    // tokens are duplicated or lost. After the window the saved conversation
    // replaces the bubbles; offline, the bubble stays with a Retry.
    async resumeTurn(ctx) {
      if (ctx.finished) return;
      if (!ctx.turnId) {
        // The message may never have reached the tutor: put it back in the
        // box to send again, as for a busy turn.
        this.giveBack(ctx);
        finishBubble(ctx.bubbleId,
          `<div class="text-amber-800 text-sm" role="alert">Could not reach ${escHtml(tutorName())}` +
          `${navigator.onLine === false ? ' (you are offline)' : ''}. Your message is back in ` +
          `the box: send it again when you are online.</div>`);
        return;
      }
      updateStreamingBubble(ctx.bubbleId,
        ctx.rawText + "\n\n*(connection lost, reconnecting...)*");
      const deadline = Date.now() + RESUME_WINDOW_MS;
      let delay = 1500;
      while (!ctx.finished && Date.now() < deadline) {
        await this.waitOnlineOr(delay);
        delay = Math.min(delay * 2, 8000);
        if (navigator.onLine === false) continue;  // radio still down; wait more
        try {
          const resp = await fetch(
            `/chat/message/${ctx.turnId}?offset=${ctx.count}`,
            { credentials: 'include', cache: 'no-store' });
          if (resp.status === 404) { ctx.gone = true; break; }   // expired: the reply is in the history
          if (resp.status === 401) { this.markExpired(ctx); return; }
          if (!resp.ok) continue;
          ctx.resumed = true;
          await this.pumpEvents(resp, ctx);
          if (ctx.finished) return;
          // Stream ended without done/error: dropped again; loop resumes
          // from the advanced ctx.count.
        } catch (e) { /* still offline or mid-recovery; keep trying */ }
      }
      // Done, or the student has since switched course (the bubble is gone).
      if (ctx.finished || !document.getElementById(ctx.bubbleId)) return;
      // The reply is saved once it finishes, so show the saved conversation.
      // If that cannot load either, keep the bubble and what streamed.
      if (await this.loadHistory({course: ctx.course, notice: false, restoreActive: false})) {
        this.noteInterrupted(ctx);
        return;
      }
      this.markDropped(ctx);
    },

    // The saved conversation ends on this question with no reply, or on the
    // note a restart leaves: the site restarted under the reply (a deploy),
    // which used to leave the question sitting unanswered with no word
    // (R31). The question goes back in the box, with a sentence.
    noteInterrupted(ctx) {
      // Only when the turn is gone from the server (a restart): a reply
      // that is merely slow outlasts the reconnect window too.
      if (!ctx.gone) return;
      const rows = this.historyRows || [];
      const last = rows[rows.length - 1];
      if (!last) return;
      // A reattached reply's question is the conversation's last row: the
      // turn saves it first.
      const prev = rows[rows.length - 2];
      const q = ctx.text || (ctx.reattached
        ? ((last.role === 'user' ? last : (prev && prev.role === 'user' ? prev : {})).content || '').trim()
        : '');
      if (!q) return;
      const unanswered = last.role === 'user' && (last.content || '').trim() === q;
      const noted = last.role === 'assistant' && (last.content || '').startsWith(INTERRUPTED_PREFIX);
      if (!unanswered && !noted) return;
      const box = document.getElementById('msg-input');
      // The student's own words: for a code send, only what was typed in
      // front of it, since the code is still in its box (the Stage 4 review).
      const words = ctx.code ? (ctx.boxText || '') : (ctx.boxText || q);
      if (box && words && !box.value.trim()) box.value = words;
      if (ctx.code) setTimeout(() => { this.showCode = true; }, 400);
      if (unanswered) {
        const id = 'bubble-' + Date.now() + '-cut';
        appendStreamingBubble(id);
        finishBubble(id, `<div class="text-amber-800 text-sm" role="status" data-interrupted>The reply to this ` +
          `was cut off (the site restarted). Your question is back in the box: send it again.</div>`);
      }
      announce('The reply was cut off by a restart. Your question is back in the box.');
    },

    // Show a reply that is still being written: from the page's load, a
    // course switch, or a "busy" answer naming the running turn (R30). It
    // streams from its first event into a bubble under its question; if the
    // saved conversation already has it, the bubble goes.
    async reattachRunning() {
      if (this.streaming) return;
      try {
        const r = await fetch(`/chat/running?course=${this.course}`, {credentials: 'include', cache: 'no-store'});
        if (!r.ok) return;
        const j = await r.json();
        if (j.turn_id) {
          await this.reattach(j.turn_id);
        } else {
          // Nothing running, but the history ended on a question: its reply
          // may have been saved between the two requests (the Stage 4 review).
          const rows = this.historyRows || [];
          const last = rows[rows.length - 1];
          if (last && last.role === 'user') await this.loadHistory({notice: false});
        }
      } catch { /* nothing running, or offline: the history stands */ }
    },

    async reattach(turnId) {
      if (!turnId || this.streaming || document.querySelector(`[data-turn="${turnId}"]`)) return;
      const bubbleId = 'bubble-' + Date.now() + '-again';
      appendStreamingBubble(bubbleId);
      const el = document.getElementById(bubbleId);
      if (el) el.dataset.turn = turnId;
      const ctx = { bubbleId, rawText: '', turnId, count: 0, finished: false, text: null, userEl: null,
                    course: this.course, sentAt: Date.now(), replyChars: 0, code: false, boxText: null,
                    resumed: true, reattached: true };
      this.streaming = true;
      this.activeCtx = ctx;
      try {
        try {
          const resp = await fetch(`/chat/message/${turnId}?offset=0`, {credentials: 'include', cache: 'no-store'});
          if (!resp.ok) {
            // Finished and gone, or another student's: the history has it.
            removeBubble(bubbleId);
            return;
          }
          await this.pumpEvents(resp, ctx);
        } catch { /* dropped: reconnect below */ }
        if (!ctx.finished) await this.resumeTurn(ctx);
      } finally {
        this.streaming = false;
        if (this.activeCtx === ctx) this.activeCtx = null;
        scrollMessages();
        if (ctx.reattachTo) setTimeout(() => this.reattach(ctx.reattachTo), 0);
      }
    },

    // The reconnect window ran out: keep what streamed, say nothing is lost,
    // and try again on a tap or as soon as the browser is back online.
    markDropped(ctx) {
      const el = document.getElementById(ctx.bubbleId);
      if (!el) return;
      const partial = ctx.rawText.trim() ? renderMarkdown(ctx.rawText) : '';
      const why = navigator.onLine === false
        ? 'You are offline. Nothing is lost: the reply finishes on the server and appears ' +
          'here when you are back online.'
        : `Could not reach ${escHtml(tutorName())}. Nothing is lost: the reply finishes on the server.`;
      finishBubble(ctx.bubbleId, partial +
        `<div class="mt-2 text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 ` +
        `text-sm" role="status" data-dropped><p>${why}</p>` +
        `<button type="button" data-retry class="${RETRY_BTN}">Retry</button></div>`);
      el.querySelector('[data-retry]').addEventListener('click', () => this.retryDropped(ctx));
      announce(why);
      ctx.onOnline = ctx.onOnline || (() => this.retryDropped(ctx));
      window.addEventListener('online', ctx.onOnline);   // the same listener twice is one
    },

    // The session ended while a reply was coming: retrying cannot help, and
    // it read as a network problem (docs/bulletproofing-plan-2026-10.md,
    // R28). The reply finishes and is saved on the server; signing in shows it.
    markExpired(ctx) {
      ctx.finished = true;
      if (!document.getElementById(ctx.bubbleId)) return;
      const partial = ctx.rawText.trim() ? renderMarkdown(ctx.rawText) : '';
      const next = encodeURIComponent('/chat?course=' + ctx.course);
      finishBubble(ctx.bubbleId, partial +
        `<div class="mt-2 text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 ` +
        `text-sm" role="alert" data-expired>Your session ended while this reply was coming. ` +
        `<a class="underline font-medium" href="/auth/login?next=${next}">Sign in again</a>: ` +
        `the reply is saved and will be in the conversation.</div>`);
      announce('Your session ended while this reply was coming. Sign in again to see it.');
    },

    // Retry a dropped reply: continue its stream while the server still has
    // it (10 minutes), else show the saved conversation. Offline, the bubble
    // goes back to its Retry.
    async retryDropped(ctx) {
      if (ctx.finished || !document.getElementById(ctx.bubbleId)) {
        window.removeEventListener('online', ctx.onOnline);
        return;
      }
      if (ctx.retrying || this.streaming) return;
      ctx.retrying = true;
      this.streaming = true;
      try {
        reopenBubble(ctx.bubbleId);
        updateStreamingBubble(ctx.bubbleId, ctx.rawText + "\n\n*(reconnecting...)*");
        try {
          const resp = await fetch(`/chat/message/${ctx.turnId}?offset=${ctx.count}`,
                                   { credentials: 'include', cache: 'no-store' });
          if (resp.status === 401) { window.removeEventListener('online', ctx.onOnline); this.markExpired(ctx); return; }
          if (resp.ok) await this.pumpEvents(resp, ctx);
        } catch (e) { /* still offline: try the saved conversation */ }
        const settled = ctx.finished
          || !document.getElementById(ctx.bubbleId)
          || await this.loadHistory({course: ctx.course, notice: false});
        if (settled) window.removeEventListener('online', ctx.onOnline);
        else this.markDropped(ctx);
      } finally {
        ctx.retrying = false;
        this.streaming = false;
        scrollMessages();
      }
    },

    // Resolve after ms, or as soon as the browser reports connectivity is
    // back, whichever comes first.
    waitOnlineOr(ms) {
      return new Promise((resolve) => {
        let timer = null;
        const fin = () => {
          if (timer) clearTimeout(timer);
          window.removeEventListener('online', fin);
          resolve();
        };
        timer = setTimeout(fin, ms);
        window.addEventListener('online', fin, { once: true });
      });
    },

    // Consume one SSE response, applying events to the turn context. Shared
    // by the initial POST and by resumes.
    async pumpEvents(resp, ctx) {
      const app = this;
      const reader   = resp.body.getReader();
      const decoder  = new TextDecoder();
      let   buffer   = '';

      while (true) {
        const {done, value} = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, {stream: true});

        // SSE lines: "data: {...}\n\n"
        const parts = buffer.split('\n\n');
        buffer = parts.pop();   // keep incomplete last chunk

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith('data: ')) continue;
          let event;
          try { event = JSON.parse(line.slice(6)); } catch { continue; }

          if (event.type === 'turn') {
            ctx.turnId = event.id;   // resume handle; not counted in offset
            if (!ctx.started) {
              ctx.started = true;
              trackEvent('reply_started', {turn_id: ctx.turnId, value_ms: Date.now() - ctx.sentAt});
            }
            continue;
          }
          ctx.count += 1;

          if (event.type === 'token') {
            if (!ctx.firstToken) {
              ctx.firstToken = true;
              // A reattached reply was timed by the page that asked (the
              // Stage 4 review: a replay counted it twice, from the reload).
              if (!ctx.reattached) {
                trackEvent('reply_first_token', {turn_id: ctx.turnId, value_ms: Date.now() - ctx.sentAt});
              }
            }
            ctx.rawText += event.text;
            updateStreamingBubble(ctx.bubbleId, ctx.rawText);

          } else if (event.type === 'slide') {
            app.showSlide(event.url, event.title, event.num);
            const cited = (event.url || '').match(/\/slides\/([A-Za-z0-9]{1,8})\/lecture-(\d+)\//);
            if (!ctx.reattached) {
              trackEvent('slide_cited', {turn_id: ctx.turnId, slide_num: event.num,
                                         lecture_num: cited ? parseInt(cited[2], 10) : undefined,
                                         course: cited ? cited[1] : undefined});
            }
            showSlideChip(ctx.bubbleId, event.title, event.num, () => {
              trackEvent('slide_chip_click', {turn_id: ctx.turnId, slide_num: event.num,
                                              lecture_num: cited ? parseInt(cited[2], 10) : undefined});
              app.openSlides();
            });

          } else if (event.type === 'eval') {
            showEvalBadge(event.score, event.correct);

          } else if (event.type === 'exam') {
            ctx.exam = event.not_now || 'Not now';   // a prerequisite check follows

          } else if (event.type === 'prediction') {
            showPredictionNote(event.predicted, event.actual);

          } else if (event.type === 'done') {
            ctx.finished = true;
            // Every turn ends with done, including one that failed. After an
            // error the bubble already shows it; the done must not replace it
            // (that used to leave an empty bubble when the tutor was down).
            if (ctx.errored) continue;
            if (event.limited) {
              // A quota or cap is used up: the question was not saved, so it
              // goes back in the box, and the notice is a note, not a reply
              // to speak or rate; Send waits, the line under it says until
              // when (docs/bulletproofing-plan-2026-10.md, R21).
              trackEvent('reply_done', {turn_id: ctx.turnId, outcome: 'limited',
                                        value_ms: Date.now() - ctx.sentAt});
              app.giveBack(ctx);
              // The notice says to step away: the question outlives the tab
              // until the limit lifts (a week at most), and the code its box.
              if (ctx.boxText) saveDraft(ctx.boxText, ctx.course, 7 * 24 * 3600 * 1000);
              if (ctx.code) saveCodeDraft(ctx.course);
              finishBubble(ctx.bubbleId,
                `<div class="text-gray-700 text-sm" role="status">${escHtml(event.full_text)}</div>`);
              app.quotaBlocked = true;
              app.quotaNote = event.full_text;
              app.recheckQuota();
              announce(event.full_text);
              continue;
            }
            // A reattached reply the saved conversation already shows.
            if (ctx.reattached && shownAlready(event.message_id)) {
              removeBubble(ctx.bubbleId);
              continue;
            }
            // A reply came: a question kept from an earlier refusal is spent.
            clearDraft();
            trackEvent('reply_done', {turn_id: ctx.turnId, message_id: event.message_id,
                                      value_ms: ctx.reattached ? undefined : Date.now() - ctx.sentAt,
                                      chars: ctx.rawText.length, outcome: ctx.resumed ? 'resumed' : 'done'});
            finishBubble(ctx.bubbleId, renderMarkdown(ctx.rawText), ctx.rawText, event.message_id);
            Prism.highlightAllUnder(document.getElementById(ctx.bubbleId));
            announceReply(ctx.bubbleId);
            if (ctx.exam) showNotNow(ctx.bubbleId, ctx.exam, () => app.sendNotNow(ctx.exam));

          } else if (event.type === 'section') {
            // One turn, two saved replies (an exam pass, then the question it
            // held back; a deferral, then the answer). Close this bubble on
            // its own saved text, so its speak and feedback buttons match
            // the stored reply, and stream the rest into a new bubble.
            if (ctx.reattached && shownAlready(event.message_id)) {
              // The first part is in the saved conversation: this bubble
              // carries only the rest (the Stage 4 review: it showed twice).
              ctx.rawText = '';
              const el = document.getElementById(ctx.bubbleId);
              if (el) { el._pending = null; el.innerHTML = ''; }
              continue;
            }
            trackEvent('reply_done', {turn_id: ctx.turnId, message_id: event.message_id,
                                      value_ms: Date.now() - ctx.sentAt,
                                      chars: (event.full_text || '').length, outcome: 'done'});
            finishBubble(ctx.bubbleId, renderMarkdown(event.full_text), event.full_text, event.message_id);
            Prism.highlightAllUnder(document.getElementById(ctx.bubbleId));
            announceReply(ctx.bubbleId);
            const nextId = 'bubble-' + Date.now() + '-' + ctx.count;
            appendStreamingBubble(nextId);
            ctx.bubbleId = nextId;
            ctx.rawText = '';

          } else if (event.type === 'busy') {
            // The previous reply is still running (another tab, a double
            // send): nothing was sent. Put the message back in the box.
            ctx.finished = true;
            ctx.errored = true;
            trackEvent('reply_done', {turn_id: ctx.turnId, outcome: 'busy',
                                      value_ms: Date.now() - ctx.sentAt});
            app.giveBack(ctx);
            finishBubble(ctx.bubbleId,
              `<div class="text-gray-600 text-sm" role="status">${escHtml(event.message)}</div>`);
            announce(event.message);
            // The reply it waits on, shown as it comes once this send ends.
            if (event.turn_id) ctx.reattachTo = event.turn_id;

          } else if (event.type === 'error') {
            ctx.finished = true;
            ctx.errored = true;
            trackEvent('reply_done', {turn_id: ctx.turnId, outcome: 'error', chars: ctx.rawText.length,
                                      value_ms: Date.now() - ctx.sentAt});
            // Keep whatever part of the reply already streamed and put the
            // error under it. A failed turn gets no speak or feedback buttons.
            const partial = ctx.rawText.trim()
              ? renderMarkdown(ctx.rawText) : '';
            finishBubble(ctx.bubbleId, partial +
              `<div class="mt-2 text-amber-600 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-sm" role="alert">${escHtml(event.message)}</div>`);
            announce(event.message);
          }
        }
      }
    },
  };
}

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

// A student's message as HTML: the text escaped, and fenced code shown as a
// code block, monospace with its spacing, instead of prose with visible
// fences (plan step 3.5, finding D19).
function renderUserText(text) {
  const fence = /(```|~~~)[^\n]*\n([\s\S]*?)(?:\1[ \t]*(?:\n|$)|$)/g;
  const out = [];
  let last = 0;
  let m;
  while ((m = fence.exec(text)) !== null) {
    const before = text.slice(last, m.index).replace(/\s+$/, '');
    if (before) out.push(escHtml(before));
    const code = m[2].replace(/\n$/, '');
    out.push('<pre class="my-2 p-2 rounded-lg bg-gray-800 text-green-200 font-mono text-xs ' +
             'whitespace-pre-wrap break-words overflow-x-auto"><code>' +
             code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</code></pre>');
    last = fence.lastIndex;
  }
  const rest = text.slice(last).replace(/^\s+/, '');
  if (rest) out.push(escHtml(rest));
  return out.join('');
}

function appendMessage(role, text) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  if (role === 'user') {
    div.className = 'flex gap-3 justify-end';
    div.innerHTML =
      `<div class="bg-gray-200 rounded-2xl rounded-tr-none px-4 py-3 whitespace-pre-wrap
                   max-w-prose min-w-0 text-sm text-gray-800 break-words">${renderUserText(text)}</div>
       <div class="w-8 h-8 rounded-full bg-gray-400 text-white flex items-center
                   justify-center text-sm font-bold shrink-0">S</div>`;
  }
  msgs.appendChild(div);
  scrollMessages();
  return div;
}

// Render a stored assistant message (from history) as a finished bubble.
function appendAssistantMessage(rawText, messageId, rating) {
  const id = 'hist-' + Math.random().toString(36).slice(2);
  appendStreamingBubble(id);
  // A "[No reply: ...]" note (a turn that failed) is not a tutor reply, so it
  // gets no speak or feedback buttons.
  // A partial reply saved with "[Reply incomplete: ...]" failed too, as it
  // did live (plan step 4.3, finding D30).
  const failedTurn = /^\[(No reply:|The tutor hit a connection problem)/.test(rawText)
                     || /\[Reply incomplete: [^\]]*\]\s*$/.test(rawText);
  finishBubble(id, renderMarkdown(rawText), failedTurn ? undefined : rawText, messageId, rating);
  Prism.highlightAllUnder(document.getElementById(id));
}

// A message typed while the session had expired is kept for the login round
// trip. localStorage, because the magic link opens a new tab; tagged with the
// student id so a shared lab computer never hands one student's draft to the
// next, and dropped after six hours.
const DRAFT_KEY = 'tutor-draft';
// The map's links carry one-shot parameters: goal (serve the Teach-me
// opener), topic and mode (prefill the box), depth. Read once, they leave the
// address, so a reload or a restored tab does not serve the opener again
// below the follow-ups (docs/bulletproofing-plan-2026-10.md, R18). The
// course stays, as the one the page opened on; so does the lecture.
// True when they are gone (or were never there), so a load that read them
// was a fresh press, not a reload.
function forgetOneShotParams(params, course) {
  const kept = new URLSearchParams();
  if (params.has('course')) kept.set('course', course);
  const lec = params.get('lecture_num');
  if (lec) kept.set('lecture_num', lec);
  if (kept.toString() === params.toString()) return true;
  const q = kept.toString();
  try {
    history.replaceState(history.state, '', window.location.pathname + (q ? `?${q}` : '') + window.location.hash);
    return true;
  } catch (e) { return false; /* a sandboxed frame: the address keeps them */ }
}

// ``ttl`` is how long the draft keeps: six hours by default, longer for a
// question a quota refused, which waits for the reset.
function saveDraft(text, course, ttl) {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(
      {text, course, sid: window.TUTOR_STUDENT_ID, at: Date.now(), ttl: ttl || 6 * 3600 * 1000}));
  } catch { /* storage blocked: the message is still in the box */ }
}
function clearDraft() {
  try { localStorage.removeItem(DRAFT_KEY); } catch { /* nothing kept */ }
}
// The code panel's boxes keep their own draft: restored into the message box
// the code read as a question (the Stage 3 review).
const CODE_DRAFT_KEY = 'tutor-code-draft';
function saveCodeDraft(course) {
  const code = (document.getElementById('code-input') || {}).value || '';
  const problem = (document.getElementById('problem-input') || {}).value || '';
  if (!code.trim()) return;
  try {
    localStorage.setItem(CODE_DRAFT_KEY, JSON.stringify(
      {code, problem, course, sid: window.TUTOR_STUDENT_ID, at: Date.now()}));
  } catch { /* storage blocked */ }
}
function takeCodeDraft(course) {
  try {
    const d = JSON.parse(localStorage.getItem(CODE_DRAFT_KEY) || 'null');
    if (!d) return null;
    if (Date.now() - (d.at || 0) > 7 * 24 * 3600 * 1000 || d.sid !== window.TUTOR_STUDENT_ID) {
      localStorage.removeItem(CODE_DRAFT_KEY);
      return null;
    }
    if (d.course && d.course !== course) return null;
    localStorage.removeItem(CODE_DRAFT_KEY);
    return d;
  } catch { return null; }
}
// Everything typed and not sent, kept for the page that comes back after a
// sign-in: the message box (unless empty) and the code panel.
function keepDrafts(course, ttl) {
  const box = document.getElementById('msg-input');
  if (box && box.value.trim()) saveDraft(box.value, course, ttl);
  saveCodeDraft(course);
}
// Any "Sign in again" or "Log in again" link on the page (an expired reply, a
// photo, the history notice) keeps what is typed, as the Send path does.
document.addEventListener('click', (e) => {
  const a = e.target && e.target.closest && e.target.closest('a[href^="/auth/login"]');
  if (a) keepDrafts(window.TUTOR_COURSE);
}, true);
function takeDraft(course) {
  try {
    const d = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null');
    if (!d) return '';
    const fresh = Date.now() - (d.at || 0) < (d.ttl || 6 * 3600 * 1000);
    if (!fresh || d.sid !== window.TUTOR_STUDENT_ID) {
      localStorage.removeItem(DRAFT_KEY);   // stale, or another student's
      return '';
    }
    if (d.course && d.course !== course) return '';
    localStorage.removeItem(DRAFT_KEY);
    return typeof d.text === 'string' ? d.text : '';
  } catch { return ''; }
}

// Clear all chat bubbles except the static welcome bubble (first child).
function clearDynamicMessages() {
  const msgs = document.getElementById('messages');
  if (!msgs) return;
  while (msgs.children.length > 1) msgs.removeChild(msgs.lastChild);
}

// The saved conversation could not load: say so under the bubbles, with a
// Retry that also runs when the browser is back online. One notice at a
// time; the next successful load clears it with the bubbles.
let historyNoticeRetry = null;
window.addEventListener('online', () => {
  if (historyNoticeRetry && document.getElementById('history-notice')) historyNoticeRetry();
});

function showHistoryNotice(status, course, retry) {
  const msgs = document.getElementById('messages');
  if (!msgs) return;
  const old = document.getElementById('history-notice');
  if (old) old.remove();
  const div = document.createElement('div');
  div.id = 'history-notice';
  div.setAttribute('role', 'status');
  div.className = 'text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-sm';
  if (status === 401) {
    historyNoticeRetry = null;
    const next = encodeURIComponent('/chat?course=' + course);
    div.innerHTML = `Your session expired. <a class="underline font-medium" ` +
      `href="/auth/login?next=${next}">Log in again</a> to see your conversation.`;
  } else {
    historyNoticeRetry = retry;
    div.innerHTML = `<p>` + (navigator.onLine === false
        ? 'You are offline, so your conversation could not load. It loads when you are back online.'
        : 'Your conversation could not load.') +
      `</p><button type="button" class="${RETRY_BTN}">Retry</button>`;
    div.querySelector('button').addEventListener('click', () => retry());
  }
  msgs.appendChild(div);
  announce(div.querySelector('p') ? div.querySelector('p').textContent : div.textContent);
  scrollMessages();
}

// A slide the tutor brings up gets a chip under the reply that names it and
// opens the slides panel (plan step 3.3, finding D5): on a phone the slides
// are a panel away, so without it the slide went unseen. One chip per reply,
// naming the latest slide; wide screens show the slides beside the chat, so
// the chip is hidden there.
function showSlideChip(bubbleId, title, num, open) {
  const bubble = document.getElementById(bubbleId);
  if (!bubble) return;
  const row = bubble.parentElement;
  let chip = row.nextElementSibling;
  if (!chip || chip.dataset.slideChip !== bubbleId) {
    chip = document.createElement('div');
    chip.className = 'md:hidden pl-11';
    chip.dataset.slideChip = bubbleId;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'inline-flex items-center gap-2 max-w-full min-h-[44px] px-3 rounded-full ' +
                    'border border-blue-200 bg-white text-sm text-blue-800 hover:bg-blue-50 text-left';
    btn.addEventListener('click', open);
    chip.appendChild(btn);
    row.after(chip);
  }
  const name = title ? `${title}, slide ${num}` : `Slide ${num}`;
  const btn = chip.firstChild;
  btn.setAttribute('aria-label', `Show the slide: ${name}`);
  btn.replaceChildren();
  const icon = document.createElement('span');
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = '📽';
  const label = document.createElement('span');
  label.className = 'min-w-0 truncate';
  label.textContent = name;
  const view = document.createElement('span');
  view.className = 'shrink-0 font-medium underline';
  view.textContent = 'View';
  btn.append(icon, label, view);
  scrollMessages();
}

// A prerequisite check can be put off with one tap (plan step 3.7, finding
// B13): the button sends "Not now", which answers the question the check
// held back and leaves this lecture's check for a day.
function showNotNow(bubbleId, text, send) {
  const bubble = document.getElementById(bubbleId);
  if (!bubble) return;
  const row = document.createElement('div');
  row.className = 'pl-11 flex flex-wrap items-center gap-x-3 gap-y-1';
  row.dataset.notNow = '';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'inline-flex items-center min-h-[44px] px-4 rounded-full border border-gray-300 ' +
                  'bg-white text-sm text-gray-700 hover:bg-gray-50';
  btn.textContent = text;
  btn.addEventListener('click', send);
  const hint = document.createElement('span');
  hint.className = 'text-xs text-gray-500';
  hint.textContent = 'I answer your question; the check waits a day.';
  row.append(btn, hint);
  bubble.parentElement.after(row);
  scrollMessages();
}

// Let a finished bubble stream again (a Retry continuing a dropped reply).
function reopenBubble(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el._done = false;
  el._pending = null;
  el.classList.add('cursor');
  startThinking(id);
}

// Take a bubble off the page, with its row and its "Thinking" note.
function removeBubble(id) {
  removeThinking(id);
  const el = document.getElementById(id);
  if (el && el.parentElement) el.parentElement.remove();
  document.querySelectorAll(`[data-slide-chip="${id}"]`).forEach((chip) => chip.remove());
}

// A saved reply already on the page.
function shownAlready(messageId) {
  return !!messageId && !!document.querySelector(`#messages [data-message-id="${messageId}"]`);
}

// The note a restart leaves in the history (INTERRUPTED_NOTE in
// tutor/agent/state.py; tests/test_stage4_reloads.py holds the two equal).
const INTERRUPTED_PREFIX = '[No reply: the site restarted';

function appendStreamingBubble(id) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'flex gap-3';
  div.innerHTML =
    `${avatarHtml()}
     <div id="${id}"
          class="bg-blue-50 rounded-2xl rounded-tl-none px-4 py-3 max-w-prose min-w-0
                 text-sm text-gray-800 msg-content cursor"></div>`;
  msgs.appendChild(div);
  startThinking(id);
  scrollMessages();
}

// "Thinking ..." under a reply, one more dot every THINKING_TICK_MS, from the
// moment the message is sent until text arrives, so the student knows it was
// received. It comes back when a started reply pauses for THINKING_PAUSE_MS
// (the tutor searching the course materials mid-reply) and goes for good when
// the bubble is finished.
const THINKING_TICK_MS = 500;
const THINKING_PAUSE_MS = 2000;
const THINKING_MAX_DOTS = 40;

function startThinking(id) {
  const el = document.getElementById(id);
  if (!el) return;
  let note = document.getElementById(id + '-thinking');
  if (!note) {
    // A row of its own under the bubble's row, so the bubble's markup (which
    // the feedback and Not-now code walk) stays as it was.
    note = document.createElement('div');
    note.id = id + '-thinking';
    note.className = 'thinking text-sm';
    note.setAttribute('aria-hidden', 'true');   // a dot every tick would flood a screen reader
    el.parentElement.after(note);
  }
  stopThinking(id);
  el._lastText = 0;          // 0: nothing streamed yet
  let dots = 3;
  const draw = () => {
    const waiting = !el._lastText || Date.now() - el._lastText >= THINKING_PAUSE_MS;
    if (!waiting) {
      note.textContent = '';
      dots = 3;
      return;
    }
    note.textContent = 'Thinking ' + '.'.repeat(dots);
    dots = dots >= THINKING_MAX_DOTS ? 3 : dots + 1;
  };
  draw();
  el._thinkingTimer = setInterval(draw, THINKING_TICK_MS);
}

function stopThinking(id) {
  const el = document.getElementById(id);
  if (el && el._thinkingTimer) {
    clearInterval(el._thinkingTimer);
    el._thinkingTimer = null;
  }
  const note = document.getElementById(id + '-thinking');
  if (note) note.textContent = '';
}

function removeThinking(id) {
  stopThinking(id);
  const note = document.getElementById(id + '-thinking');
  if (note) note.remove();
}

function updateStreamingBubble(id, rawText) {
  const el = document.getElementById(id);
  if (!el || el._done) return;
  // Render markdown live so formatting appears as it streams (no jarring
  // raw-then-reformat snap at the end). Coalesce bursts of tokens into one
  // parse per animation frame so a long reply stays smooth.
  el._pending = rawText;
  el._lastText = Date.now();
  const note = document.getElementById(id + '-thinking');
  if (note) note.textContent = '';
  if (el._renderQueued) return;
  el._renderQueued = true;
  requestAnimationFrame(() => {
    el._renderQueued = false;
    if (el._done || el._pending == null) return;
    el.innerHTML = renderMarkdown(el._pending);
    scrollMessages();
  });
}

function finishBubble(id, html, speakText, messageId, rating) {
  const el = document.getElementById(id);
  if (!el) return;
  el._done = true;   // block any queued streaming render from overwriting this
  if (messageId) el.dataset.messageId = String(messageId);   // the saved reply, for the events
  removeThinking(id);
  el.classList.remove('cursor');
  el.innerHTML = html;
  if (speakText) {
    // 44 px targets (plan step 3.4, finding D8); the negative margins keep
    // the row's look while the buttons grow.
    const row = document.createElement('div');
    row.className = '-ml-3 -mb-2 mt-1 flex items-center';
    // Only a saved reply gets the speaker, as only a saved one gets thumbs:
    // the voice speaks the stored text, and a notice that was never saved
    // answered "Voice unavailable" on every press (R29).
    if (window.TUTOR_TTS && messageId) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tts-btn w-11 h-11 inline-flex items-center justify-center rounded-full ' +
                      'text-base leading-none opacity-50 hover:opacity-100 hover:bg-blue-100 transition';
      _ttsIdle(btn);
      btn.dataset.text = speakText;
      btn.dataset.messageId = String(messageId);
      btn.addEventListener('click', () => playTTS(btn));
      row.appendChild(btn);
    }
    // Only a saved reply can be rated (review step 0.4, F16): a cap notice
    // streams with no message row and used to get thumbs that always failed.
    if (messageId) row.appendChild(buildFeedbackControls(speakText, messageId, rating));
    if (row.childElementCount) el.appendChild(row);
  }
  scrollMessages();
}

// Thumbs up/down on a finished tutor reply. A thumbs down is recorded at once
// (so a silent click still counts), then asks what was wrong; the reason is a
// second POST that fills in the same row. The reply is named by its id (the
// stream's done event and /chat/history carry it; review step 0.4), with its
// text as the fallback the server also accepts; a thumb the student already
// gave shows as selected after a reload.
function buildFeedbackControls(replyText, messageId, rating) {
  const wrap = document.createElement('div');
  wrap.className = 'fb-wrap flex items-center min-w-0';
  wrap.dataset.messageId = String(messageId || '');
  const up   = document.createElement('button');
  const down = document.createElement('button');
  for (const [b, glyph, title, value] of [[up, '👍', 'This was helpful', 1],
                                           [down, '👎', 'Something was wrong', -1]]) {
    b.type = 'button';
    b.className = 'fb-btn w-11 h-11 inline-flex items-center justify-center rounded-full ' +
                  'text-base leading-none hover:opacity-100 hover:bg-blue-100 transition ' +
                  (rating === value ? 'opacity-100' : 'opacity-50');
    b.textContent = glyph;
    b.title = title;
    b.setAttribute('aria-label', title);
    b.setAttribute('aria-pressed', rating === value ? 'true' : 'false');
    b.addEventListener('click', () => rateReply(wrap, replyText, value, up, down, messageId));
    wrap.appendChild(b);
  }
  const note = document.createElement('span');
  note.className = 'fb-note ml-1 text-xs text-gray-500';
  wrap.appendChild(note);
  return wrap;
}

async function postFeedback(replyText, rating, comment, messageId) {
  const fd = new FormData();
  if (messageId) fd.append('message_id', String(messageId));
  fd.append('text', replyText);
  fd.append('rating', String(rating));
  if (comment) fd.append('comment', comment);
  const r = await fetch('/chat/feedback', {method: 'POST', body: fd, credentials: 'include'});
  if (!r.ok) throw new Error('feedback failed (' + r.status + ')');
  const j = await r.json();
  trackEvent('feedback', {rating, message_id: j && j.message_id});
  return j;
}

function showRating(up, down, rating) {
  up.classList.toggle('opacity-100', rating === 1);
  up.classList.toggle('opacity-50',  rating !== 1);
  up.setAttribute('aria-pressed', rating === 1 ? 'true' : 'false');
  down.classList.toggle('opacity-100', rating === -1);
  down.classList.toggle('opacity-50',  rating !== -1);
  down.setAttribute('aria-pressed', rating === -1 ? 'true' : 'false');
}

async function rateReply(wrap, replyText, rating, up, down, messageId) {
  const note = wrap.querySelector('.fb-note');
  // One save at a time per reply: a double click sent two at once, and the
  // second could fail on the first's row (docs/bulletproofing-plan-2026-10.md, R9).
  if (up.disabled) return;
  up.disabled = down.disabled = true;
  try {
    await postFeedback(replyText, rating, undefined, messageId);
  } catch (err) {
    // The thumb shows the saved state only (it used to look selected beside
    // "Could not save feedback.").
    note.textContent = 'Could not save feedback.';
    announce(note.textContent);
    return;
  } finally {
    up.disabled = down.disabled = false;
  }
  showRating(up, down, rating);
  const old = wrap.parentElement.parentElement.querySelector('.fb-reason');
  if (old) old.remove();
  if (rating === 1) { note.textContent = 'Thanks!'; announce('Thanks for the feedback.'); return; }
  note.textContent = '';
  showFeedbackReason(wrap, replyText, messageId);
}

function showFeedbackReason(wrap, replyText, messageId) {
  const bubble = wrap.parentElement.parentElement;   // the tutor bubble div
  const box = document.createElement('div');
  box.className = 'fb-reason mt-2 w-full';
  const fieldId = 'fb-reason-' + Math.random().toString(36).slice(2);
  box.innerHTML =
    `<label for="${fieldId}" class="block text-xs text-gray-600 mb-1">What was wrong with this response?</label>
     <textarea id="${fieldId}" rows="2" maxlength="2000" placeholder="Wrong answer, off topic, too long, ..."
       class="w-full rounded-lg border border-gray-300 px-2 py-1 text-sm text-gray-800 bg-white"></textarea>
     <div class="mt-1 flex items-center gap-2">
       <button type="button" class="fb-send px-4 min-h-[44px] rounded-lg bg-gray-700 text-white text-xs font-medium hover:bg-gray-800">Send</button>
       <button type="button" class="fb-skip px-3 min-w-[44px] min-h-[44px] text-xs text-gray-500 hover:text-gray-700">Skip</button>
     </div>`;
  bubble.appendChild(box);
  const ta = box.querySelector('textarea');
  const note = wrap.querySelector('.fb-note');
  const finish = (msg) => { box.remove(); note.textContent = msg; announce(msg); };
  box.querySelector('.fb-skip').addEventListener('click', () => finish('Thanks, noted.'));
  box.querySelector('.fb-send').addEventListener('click', async () => {
    const comment = ta.value.trim();
    if (!comment) { finish('Thanks, noted.'); return; }
    try {
      await postFeedback(replyText, -1, comment, messageId);
      finish(`Thanks, ${instructorName()} will see this.`);
    } catch (err) {
      note.textContent = 'Could not save feedback.';
    }
  });
  ta.focus();
  scrollMessages();
}

// Single active narration at a time: { audio, btn }. Lets the speaker button be a
// play/pause toggle and lets Esc / a new message stop whatever is playing.
let currentTTS = null;

// The speak button's glyph and label follow its state; the glyph is hidden
// from screen readers, which read the label.
function _ttsLook(btn, glyph, label) {
  btn.innerHTML = `<span aria-hidden="true">${glyph}</span>`;
  btn.title = label;
  btn.setAttribute('aria-label', label);
}
function _ttsIdle(btn) { _ttsLook(btn, '🔊', `Play in ${instructorName()}'s voice`); }
// A failure says so beside the button: a tooltip never shows on a phone.
function _ttsNote(btn, text) {
  let note = btn.parentElement && btn.parentElement.querySelector('.tts-note');
  if (!note) {
    note = document.createElement('span');
    note.className = 'tts-note text-xs text-gray-500 mr-1';
    btn.after(note);
  }
  note.textContent = text;
  announce(text);
}

// The reply a speak button belongs to, when the client knows it (a reply
// that streamed in this page; history bubbles carry none).
function _bubbleMessageId(btn) {
  const bubble = btn && btn.closest ? btn.closest('[data-message-id]') : null;
  return bubble ? parseInt(bubble.dataset.messageId, 10) || undefined : undefined;
}

// One tts_* event per change of state, with the span played since the last.
function _ttsEvent(name, state) {
  const now = Date.now();
  const played = state.playingSince ? now - state.playingSince : 0;
  state.playingSince = name === 'tts_play' ? now : null;
  let position = 0;
  try { position = Math.round(state.audio.currentTime || 0); } catch (e) { /* ignore */ }
  trackEvent(name, {message_id: _bubbleMessageId(state.btn), value_ms: played, position_s: position});
}

// Stop the active narration (if any) and reset its button to idle.
function _ttsClearNote(btn) {
  const n = btn && btn.parentElement && btn.parentElement.querySelector('.tts-note');
  if (n) n.remove();
}

function stopTTS() {
  if (currentTTS) _ttsClearNote(currentTTS.btn);
  if (!currentTTS) return;
  const state = currentTTS;
  const { audio, btn } = state;
  currentTTS = null;
  if (state.playingSince) _ttsEvent('tts_pause', state);
  try { audio.pause(); audio.currentTime = 0; } catch (e) { /* ignore */ }
  _ttsIdle(btn);
}

async function playTTS(btn) {
  _ttsClearNote(btn);   // "Ready: tap to play." and the like belong to the last press
  // Toggle if this button already owns the active clip.
  if (currentTTS && currentTTS.btn === btn) {
    const { audio } = currentTTS;
    if (audio.paused) { audio.play(); _ttsLook(btn, '⏸', 'Pause'); _ttsEvent('tts_play', currentTTS); }
    else              { audio.pause(); _ttsLook(btn, '▶', 'Resume'); _ttsEvent('tts_pause', currentTTS); }
    return;
  }
  stopTTS();  // a different reply was playing — only one voice at a time

  const text = btn.dataset.text;
  if (!text) return;

  // Reuse the clip we already fetched for this button (resume/replay = no re-fetch).
  let url = btn.dataset.audioUrl;
  if (!url) {
    btn.disabled = true;
    _ttsLook(btn, '⏳', 'Loading the voice');
    try {
      const fd = new FormData();
      fd.append('text', text);
      // The saved reply by id: one older than the last 20 is still spoken (R29).
      if (btn.dataset.messageId) fd.append('message_id', btn.dataset.messageId);
      const r = await fetch('/chat/tts', {method: 'POST', body: fd, credentials: 'include'});
      if (!r.ok) throw new Error(r.status === 503 ? 'voice not configured' : 'tts failed');
      url = URL.createObjectURL(await r.blob());
      btn.dataset.audioUrl = url;
    } catch (err) {
      _ttsLook(btn, '🔇', 'Voice unavailable');
      _ttsNote(btn, 'Voice unavailable right now.');
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
  }

  const audio = new Audio(url);
  const state = { audio, btn, playingSince: null };
  audio.addEventListener('ended', () => {
    _ttsEvent('tts_ended', state);
    if (currentTTS && currentTTS.btn === btn) currentTTS = null;
    _ttsIdle(btn);
  });
  currentTTS = state;
  try {
    await audio.play();
    _ttsLook(btn, '⏸', 'Pause');
    _ttsEvent('tts_play', state);
  } catch (err) {
    if (err && err.name === 'NotAllowedError') {
      // iPhone Safari allows sound only within a tap, and the synthesis
      // outlasted it. The clip is fetched: the next tap plays it (R33).
      _ttsLook(btn, '▶', 'Play');
      _ttsNote(btn, 'Ready: tap to play.');
      return;
    }
    if (currentTTS && currentTTS.btn === btn) currentTTS = null;
    _ttsLook(btn, '🔇', 'Voice unavailable');
    _ttsNote(btn, 'Could not play the voice.');
  }
}

// Esc stops any narration in progress.
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') stopTTS(); });

// Follow new text only while the student is at the bottom (plan step 3.5,
// finding D14): scrolled up to re-read, they keep their place and a "New
// reply" pill appears. A scroll up leaves the bottom; reaching it again (or
// the pill, or sending) resumes following.
let followMessages = true;
let lastMessagesTop = 0;
function watchMessagesScroll() {
  const el = document.getElementById('messages');
  if (!el) return;
  el.addEventListener('scroll', () => {
    const fromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (fromBottom < 40) followMessages = true;
    else if (el.scrollTop < lastMessagesTop) followMessages = false;
    lastMessagesTop = el.scrollTop;
    if (followMessages) showNewPill(false);
  }, {passive: true});
}
// The `hidden` class, not the attribute: the pill's inline-flex would
// override the attribute and keep it on screen.
function showNewPill(show) {
  const pill = document.getElementById('new-pill');
  if (pill) pill.classList.toggle('hidden', !show);
}
function scrollMessages(force) {
  const el = document.getElementById('messages');
  if (!el) return;
  if (force) followMessages = true;
  if (followMessages) {
    el.scrollTop = el.scrollHeight;
    lastMessagesTop = el.scrollTop;
    showNewPill(false);
  } else {
    showNewPill(true);
  }
}

function showPredictionNote(predicted, actual) {
  const delta = predicted - actual;
  let cls, text;
  if (delta === 0) {
    cls = 'bg-green-50 text-green-700 border-green-200';
    text = `🎯 Well calibrated: you predicted ${predicted} and scored ${actual}.`;
  } else if (delta > 0) {
    cls = 'bg-amber-50 text-amber-700 border-amber-200';
    text = `You predicted ${predicted}, scored ${actual}. Noticing the gap is how calibration improves.`;
  } else {
    cls = 'bg-blue-50 text-blue-700 border-blue-200';
    text = `You predicted ${predicted}, scored ${actual}: better than you expected!`;
  }
  const note = document.createElement('div');
  note.className = 'flex justify-end mb-2';
  const chip = document.createElement('span');
  chip.className = `text-xs px-2 py-1 rounded-lg border ${cls}`;
  chip.textContent = text;
  note.appendChild(chip);
  document.getElementById('messages').appendChild(note);
  announce(text.replace('🎯 ', ''));
  scrollMessages();
}

function showEvalBadge(score, correct) {
  const colors = ['bg-red-100 text-red-700', 'bg-orange-100 text-orange-700',
                  'bg-yellow-100 text-yellow-700', 'bg-blue-100 text-blue-700',
                  'bg-green-100 text-green-700'];
  const labels = ['0 — Does not compile', '1 — Incorrect', '2 — Partial',
                  '3 — Correct', '4 — Excellent'];
  const color = colors[Math.min(score, 4)] || colors[0];
  const label = labels[Math.min(score, 4)] || labels[0];
  const badge = document.createElement('div');
  badge.className = `flex justify-end mb-1`;
  badge.innerHTML = `<span class="text-xs px-2 py-0.5 rounded-full font-semibold ${color}">Score: ${label}</span>`;
  document.getElementById('messages').appendChild(badge);
  announce(`Score: ${label.replace(' — ', ' out of 4, ')}`);
  scrollMessages();
}

// The polite live region (#sr-status): screen readers read each message
// added here, in order, so a score and the reply that follows it are both
// heard (replacing the text would drop the first). The last few are kept.
function announce(text) {
  const region = document.getElementById('sr-status');
  if (!region || !text) return;
  const line = document.createElement('p');
  line.textContent = text;
  region.appendChild(line);
  while (region.children.length > 5) region.firstElementChild.remove();
}

// A finished reply, as a screen reader should hear it: the prose, with each
// code block named rather than read out character by character.
function announceReply(bubbleId) {
  const el = document.getElementById(bubbleId);
  if (!el) return;
  const copy = el.cloneNode(true);
  copy.querySelectorAll('button, .fb-wrap, .fb-reason').forEach((n) => n.remove());
  copy.querySelectorAll('pre').forEach((n) => n.replaceWith(' (code block) '));
  const text = copy.textContent.replace(/\s+/g, ' ').trim();
  if (text) announce('Tutor: ' + (text.length > 1500 ? text.slice(0, 1500) + '...' : text));
}

function escHtml(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/\n/g,'<br>');
}

function recordPathEvent(goalId, course, modality, source) {
  fetch('/path/event', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({goal_id: goalId, course: course, modality: modality, source: source || 'student'}),
  }).catch(() => {});  // fire-and-forget; never block navigation
}
