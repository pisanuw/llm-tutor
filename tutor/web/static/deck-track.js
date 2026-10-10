// Activity events for a Reveal deck (docs/activity-analytics-plan-2026-10.md).
//
// Added to every deck the app serves under /slides (tutor/deck_tracking.py).
// It records the deck opening, each slide shown with the on-screen dwell on
// the one before it, a heartbeat while the deck is on screen, the narration
// player's play, pause, seek and end with the span played, and the close. It
// posts to /api/events itself (same origin, the session cookie goes along),
// so it works in the chat's slides panel, in the lecture page's frame, and
// in a tab of its own; a deck opened as a plain file has no /api and does
// nothing.
//
// Time counts only while the deck is on screen: the tab visible, the deck's
// frame intersecting the top viewport (on a phone the slides panel sits
// behind the chat as display:none), and, as a belt to that brace, the chat
// page's own word on whether the panel shows (postMessage tutor-panel).
(function () {
  'use strict';
  if (!window.fetch || !window.JSON) return;

  var ENDPOINT = '/api/events';
  var FLUSH_EVERY_MS = 10000;
  var FLUSH_AT = 20;
  var MAX_BATCH = 100;
  var script = document.currentScript;
  var heartbeatMs = 1000 * (parseInt(script && script.getAttribute('data-heartbeat'), 10) || 60);

  var where = location.pathname.match(/\/slides\/([A-Za-z0-9]{1,8})\/lecture-(\d+)\//);
  if (!where) return;   // not a deck the app serves
  var COURSE = where[1];
  var LECTURE = parseInt(where[2], 10);

  function randomId() {
    var bytes = new Uint8Array(8);
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
    else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    var out = '';
    for (var j = 0; j < bytes.length; j++) out += ('0' + bytes[j].toString(16)).slice(-2);
    return out;
  }

  // The same visit id as the page around the deck: sessionStorage is per
  // tab and per origin, so the frame and its parent share it.
  function visitId() {
    try {
      var id = sessionStorage.getItem('tutor_visit');
      if (!id || !/^[0-9a-f]{16}$/.test(id)) {
        id = randomId();
        sessionStorage.setItem('tutor_visit', id);
      }
      return id;
    } catch (e) { return randomId(); }
  }

  function context() {
    try {
      if (window.parent === window) return 'tab';
      var path = window.parent.location.pathname || '';
      return path.indexOf('/lectures/') === 0 ? 'lecture' : 'chat';
    } catch (e) { return 'tab'; }   // a cross-origin parent: treat as its own page
  }

  var VISIT = visitId();
  var PAGE = randomId();
  var CONTEXT = context();
  var seq = 0;
  var queue = [];
  var timer = null;

  function send(batch, leaving) {
    var body = JSON.stringify({ sent_at: Date.now(), events: batch });
    try {
      if (leaving && navigator.sendBeacon) {
        if (navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }))) return;
      }
      fetch(ENDPOINT, {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json' }, body: body
      }).catch(function () { /* lost; the sequence shows the gap */ });
    } catch (e) { /* never into the deck */ }
  }

  function flush(leaving) {
    if (timer) { clearTimeout(timer); timer = null; }
    while (queue.length) send(queue.splice(0, MAX_BATCH), leaving);
  }

  function track(name, fields) {
    try {
      seq += 1;
      var event = {
        name: name, seq: seq, client_ts: Date.now(), visit_id: VISIT, page_view_id: PAGE,
        path: location.pathname, course: COURSE, lecture_num: LECTURE, slide_num: currentSlide()
      };
      for (var key in fields) {
        if (Object.prototype.hasOwnProperty.call(fields, key) && fields[key] !== undefined && fields[key] !== null) {
          event[key] = fields[key];
        }
      }
      queue.push(event);
      if (queue.length >= FLUSH_AT) flush(false);
      else if (!timer) timer = setTimeout(function () { flush(false); }, FLUSH_EVERY_MS);
    } catch (e) { /* never into the deck */ }
  }

  // --- Reveal -----------------------------------------------------------------
  var deck = null;

  function currentSlide() {
    try {
      if (!deck) return undefined;
      var idx = deck.getIndices();
      return (idx.h || 0) + 1;   // 1-based, like the clips slide-NN.mp3
    } catch (e) { return undefined; }
  }

  // --- on-screen time -----------------------------------------------------------
  var tabVisible = document.visibilityState !== 'hidden';
  var panelVisible = true;      // the chat page says otherwise on a phone
  var inView = true;            // the IntersectionObserver's word
  var activeSince = null;       // when the current on-screen span began
  var activeTotal = 0;
  var slideDwell = 0;           // on-screen ms on the current slide
  var slidesSeen = {};

  function onScreen() { return tabVisible && panelVisible && inView; }

  function settle() {
    // Fold the running span into the totals; restart it if still on screen.
    var now = Date.now();
    if (activeSince !== null) {
      var span = Math.max(0, now - activeSince);
      activeTotal += span;
      slideDwell += span;
      heartbeatSpan += span;
    }
    activeSince = onScreen() ? now : null;
  }

  var heartbeatSpan = 0;

  document.addEventListener('visibilitychange', function () {
    tabVisible = document.visibilityState !== 'hidden';
    settle();
    if (!tabVisible) flush(true);
  });
  window.addEventListener('message', function (e) {
    try {
      if (e.origin !== location.origin || !e.data || e.data.type !== 'tutor-panel') return;
      panelVisible = !!e.data.visible;
      settle();
    } catch (err) { /* ignore */ }
  });
  try {
    // With a same-origin parent the root is the top viewport, so a frame
    // hidden by the chat's panel toggle stops intersecting.
    new IntersectionObserver(function (entries) {
      inView = entries.some(function (entry) { return entry.isIntersecting; });
      settle();
    }, { threshold: 0 }).observe(document.documentElement);
  } catch (e) { /* no observer: the tab and the panel still decide */ }

  // --- narration ------------------------------------------------------------------
  var audio = document.getElementById('deck-narration');
  var playingSince = null;
  var lastEnded = 0;

  function narrationEvent(name) {
    var now = Date.now();
    var played = playingSince ? now - playingSince : 0;
    playingSince = name === 'narration_play' ? now : null;
    var position = 0;
    try { position = Math.round(audio.currentTime || 0); } catch (e) { /* ignore */ }
    track(name, { value_ms: played, position_s: position });
  }
  if (audio) {
    audio.addEventListener('play', function () { narrationEvent('narration_play'); });
    audio.addEventListener('pause', function () {
      // 'pause' also fires at the end of a clip, after 'ended' handles it.
      if (!audio.ended) narrationEvent('narration_pause');
    });
    audio.addEventListener('seeking', function () { narrationEvent('narration_seek'); if (!audio.paused) playingSince = Date.now(); });
    audio.addEventListener('ended', function () { lastEnded = Date.now(); narrationEvent('narration_ended'); });
  }

  // --- the deck's events ------------------------------------------------------------
  function start() {
    var total = 0;
    try { total = deck.getTotalSlides(); } catch (e) { total = 0; }
    activeSince = onScreen() ? Date.now() : null;
    slidesSeen[currentSlide()] = true;
    track('deck_open', { slides_total: total, context: CONTEXT, has_audio: !!audio });

    deck.on('slidechanged', function (ev) {
      settle();
      var prev = (ev && ev.previousSlide && ev.previousSlide.parentNode)
        ? Array.prototype.indexOf.call(ev.previousSlide.parentNode.children, ev.previousSlide) + 1
        : undefined;
      var now = currentSlide();
      var how = 'jump';
      if (Date.now() - lastEnded < 1500) how = 'auto';            // the narration advanced it
      else if (prev !== undefined && now === prev + 1) how = 'next';
      else if (prev !== undefined && now === prev - 1) how = 'prev';
      track('slide_view', { prev_slide: prev, value_ms: slideDwell, how: how });
      slideDwell = 0;
      slidesSeen[now] = true;
    });

    setInterval(function () {
      if (!onScreen()) return;
      settle();
      track('deck_heartbeat', { narration_playing: !!(audio && !audio.paused && !audio.ended),
                                value_ms: heartbeatSpan });
      heartbeatSpan = 0;
    }, heartbeatMs);

    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      settle();
      if (playingSince) narrationEvent('narration_pause');
      track('deck_close', { value_ms: slideDwell, active_ms: activeTotal,
                            slides_seen: Object.keys(slidesSeen).length });
      flush(true);
    }
    window.addEventListener('pagehide', close);
    window.addEventListener('beforeunload', close);
  }

  // Reveal sets window.deck once it has initialized (the renderer's template).
  var waited = 0;
  (function waitForDeck() {
    if (window.deck && typeof window.deck.on === 'function') { deck = window.deck; start(); return; }
    waited += 100;
    if (waited <= 20000) setTimeout(waitForDeck, 100);
  })();

  window.tutorDeckFlush = function () { flush(false); };
})();
