// Activity events for a signed-in page (docs/activity-analytics-plan-2026-10.md).
//
// Loaded by base.html for a signed-in reader while ACTIVITY_EVENTS is on. It
// queues what the student does and posts batches to POST /api/events (the
// vocabulary and the limits live in tutor/activity_events.py): the page view,
// the tab going to the background and coming back, a heartbeat while the page
// is visible, the leave, and every click on a control. Page scripts add their
// own events through window.tutorTrack(name, fields).
//
// Every row carries the ids the analysis joins on: a visit id per browser tab
// (sessionStorage, so a reload keeps it), a page view id per load, and a
// sequence number per page view (a gap is a lost batch). Durations are
// measured here, where the browser knows them; the server stamps its own time
// and corrects it by this clock's offset (sent_at). Nothing in here may throw
// into the page: observability never breaks the tutor.
(function () {
  'use strict';
  if (!window.fetch || !window.JSON || !document.addEventListener) return;
  // A page inside a frame is not a page view: the chat's deck panel can show
  // the error page for a missing deck, and its own tracker would share this
  // tab's visit id and duplicate the sequence numbers (decks run
  // deck-track.js instead, on purpose).
  try { if (window.top !== window.self) return; } catch (e) { return; }

  var ENDPOINT = '/api/events';
  var FLUSH_EVERY_MS = 10000;   // a batch at most this long after its first event
  var FLUSH_AT = 20;            // ... or as soon as this many wait
  var MAX_BATCH = 100;          // the server's cap per POST
  var MAX_QUEUE = 400;          // beyond this the oldest are dropped (the server is unreachable)
  var script = document.currentScript;
  var heartbeatMs = 1000 * (parseInt(script && script.getAttribute('data-heartbeat'), 10) || 60);

  function randomId() {
    var bytes = new Uint8Array(8);
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
    else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    var out = '';
    for (var j = 0; j < bytes.length; j++) out += ('0' + bytes[j].toString(16)).slice(-2);
    return out;
  }

  function visitId() {
    // One id per browser tab: sessionStorage is per tab and survives reloads.
    try {
      var id = sessionStorage.getItem('tutor_visit');
      if (!id || !/^[0-9a-f]{16}$/.test(id)) {
        id = randomId();
        sessionStorage.setItem('tutor_visit', id);
      }
      return id;
    } catch (e) {
      return randomId();   // storage blocked: the tab still gets one id for this load
    }
  }

  var VISIT = visitId();
  var PAGE = randomId();
  var seq = 0;
  var queue = [];
  var timer = null;
  var lastTurnId = null;

  // --- on-screen time -------------------------------------------------------
  var visible = document.visibilityState !== 'hidden';
  var activeSince = visible ? Date.now() : null;
  var activeTotal = 0;

  function takeActive() {
    // Active ms since the last take; also folds them into the page total.
    if (activeSince === null) return 0;
    var now = Date.now();
    var span = Math.max(0, now - activeSince);
    activeSince = now;
    activeTotal += span;
    return span;
  }

  // --- the queue ------------------------------------------------------------
  function send(batch, leaving) {
    var body = JSON.stringify({ sent_at: Date.now(), events: batch });
    try {
      if (leaving && navigator.sendBeacon) {
        if (navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }))) return;
      }
      fetch(ENDPOINT, {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json' }, body: body
      }).catch(function () { /* lost; the sequence numbers show the gap */ });
    } catch (e) { /* never into the page */ }
  }

  function flush(leaving) {
    if (timer) { clearTimeout(timer); timer = null; }
    while (queue.length) send(queue.splice(0, MAX_BATCH), leaving);
  }

  function schedule() {
    if (!timer) timer = setTimeout(function () { flush(false); }, FLUSH_EVERY_MS);
  }

  function track(name, fields) {
    try {
      if (typeof name !== 'string' || !name) return;
      seq += 1;
      var event = {
        name: name, seq: seq, client_ts: Date.now(),
        visit_id: VISIT, page_view_id: PAGE, path: location.pathname
      };
      if (window.TUTOR_COURSE) event.course = String(window.TUTOR_COURSE);
      if (fields) {
        for (var key in fields) {
          if (Object.prototype.hasOwnProperty.call(fields, key) && fields[key] !== undefined && fields[key] !== null) {
            event[key] = fields[key];
          }
        }
      }
      if (event.turn_id) lastTurnId = event.turn_id;
      queue.push(event);
      if (queue.length > MAX_QUEUE) queue.splice(0, queue.length - MAX_QUEUE);
      if (queue.length >= FLUSH_AT) flush(false); else schedule();
    } catch (e) { /* never into the page */ }
  }

  // --- the page's own events --------------------------------------------------
  function sameOriginPath(href) {
    // The path of a same-origin URL (no query, no fragment), else undefined:
    // where a link on this site sends the student, so a 404 joins its click.
    try {
      if (!href) return undefined;
      var url = new URL(href, location.href);
      return url.origin === location.origin ? url.pathname.slice(0, 200) : undefined;
    } catch (e) { return undefined; }
  }

  // --- script failures --------------------------------------------------------
  // A JavaScript error that breaks the page for one browser used to leave no
  // trace (review step 0.2, F52). The name, the script and the line are
  // recorded; never the message, which can quote the student. A loop of
  // errors is capped per page view.
  var MAX_ERRORS = 5;
  var errorsSent = 0;
  function reportError(name, source, line) {
    if (errorsSent >= MAX_ERRORS) return;
    errorsSent += 1;
    track('client_error', {
      error: String(name || 'Error').slice(0, 40),
      source: sameOriginPath(source) || undefined,
      line: typeof line === 'number' && line >= 0 ? Math.floor(line) : 0
    });
  }
  window.addEventListener('error', function (e) {
    try { reportError(e.error && e.error.name ? e.error.name : 'Error', e.filename, e.lineno); } catch (x) { /* never */ }
  });
  window.addEventListener('unhandledrejection', function (e) {
    try {
      var r = e.reason;
      reportError(r && r.name ? r.name : 'UnhandledRejection', '', 0);
    } catch (x) { /* never */ }
  });
  window.tutorTrackError = reportError;

  function sameSiteReferrer() {
    try {
      if (!document.referrer) return '';
      var url = new URL(document.referrer);
      return url.origin === location.origin ? url.pathname : '';
    } catch (e) { return ''; }
  }

  track('page_view', {
    ref: sameSiteReferrer(),
    vw: window.innerWidth || 0,
    vh: window.innerHeight || 0
  });

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      if (!visible) return;
      visible = false;
      track('page_hide', { value_ms: takeActive() });
      activeSince = null;
      flush(true);
    } else {
      if (visible) return;
      visible = true;
      activeSince = Date.now();
      track('page_show');
    }
  });

  var left = false;
  function leave() {
    if (left) return;
    left = true;
    if (visible) takeActive();
    track('page_leave', { value_ms: activeTotal, turn_id: lastTurnId || undefined });
    flush(true);
  }
  window.addEventListener('pagehide', leave);
  // Safari before 2023 and some in-app browsers fire only beforeunload.
  window.addEventListener('beforeunload', leave);
  window.addEventListener('pageshow', function (e) {
    // Back from the back/forward cache: the page is alive again.
    if (e.persisted) { left = false; visible = document.visibilityState !== 'hidden'; activeSince = visible ? Date.now() : null; track('page_show'); }
  });

  setInterval(function () {
    if (visible && !left) track('page_heartbeat', { value_ms: takeActive() });
  }, heartbeatMs);

  // Every click on a control. The label is the control's own static text;
  // a data-track attribute names a control more usefully than its id.
  document.addEventListener('click', function (e) {
    try {
      var el = e.target && e.target.closest
        ? e.target.closest('a,button,[data-track],input[type=submit],summary') : null;
      if (!el) return;
      var label = (el.getAttribute('data-track-label') || el.getAttribute('aria-label')
                   || el.textContent || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 60);
      track('click', {
        target: el.getAttribute('data-track') || el.id || el.name || el.tagName.toLowerCase(),
        label: label || undefined,
        href: sameOriginPath(el.tagName === 'A' ? el.href : '')
      });
      // A control can also name a semantic event in its markup:
      // data-event="lecture_open" data-event-fields='{"course": "AST101", "lecture_num": 3}'.
      var named = el.getAttribute('data-event');
      if (named) {
        var fields = {};
        try { fields = JSON.parse(el.getAttribute('data-event-fields') || '{}') || {}; } catch (e2) { fields = {}; }
        track(named, fields);
      }
    } catch (err) { /* never into the page */ }
  }, true);

  window.tutorTrack = track;
  window.tutorTrackFlush = function () { flush(false); };
  window.tutorTrackIds = function () { return { visit_id: VISIT, page_view_id: PAGE }; };
})();
