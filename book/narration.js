// Chapter narration in the instructor's voice, for signed-in students only.
// The book is public; the clips are not. They live behind the session-gated
// /lecture-audio mount as /lecture-audio/book/<chapter>.mp3, rendered by
// `python -m pipeline render-book-audio`. A HEAD probe with the session cookie
// decides what to show: 200 -> a player, 401 -> a sign-in line, anything else
// (no clip for this page) -> nothing. No frameworks, vanilla DOM only.

(function () {
  function slugOf(pathname) {
    var m = pathname.match(/\/([^\/]+)\.html$/);
    if (m) return m[1];
    return pathname.slice(-1) === '/' ? 'index' : null;
  }

  function init() {
    var slug = slugOf(location.pathname);
    var host = document.querySelector('#quarto-document-content') || document.querySelector('main');
    if (!slug || !host) return;
    var src = '/lecture-audio/book/' + slug + '.mp3';
    // narration-index.json (public, beside the pages) names the chapters that
    // have a clip; the gate answers 401 before it looks for a file, so the
    // probe alone cannot tell a missing clip from a missing session.
    fetch('narration-index.json', { cache: 'no-cache' }).then(function (r) {
      return r.ok ? r.json() : [];
    }).then(function (slugs) {
      if (slugs.indexOf(slug) < 0) return null;
      return fetch(src, { method: 'HEAD', credentials: 'include' });
    }).then(function (r) {
      if (!r) return;
      var box = document.createElement('div');
      box.className = 'book-narration';
      if (r.status === 200) {
        var label = document.createElement('span');
        label.className = 'book-narration-label';
        label.textContent = '🔊 Listen to this chapter';
        var audio = document.createElement('audio');
        audio.controls = true;
        audio.preload = 'none';
        audio.src = src;
        box.appendChild(label);
        box.appendChild(audio);
        watch(audio, slug);
      } else if (r.status === 401) {
        var a = document.createElement('a');
        a.href = '/auth/login?next=' + encodeURIComponent(location.pathname);
        a.textContent = 'Sign in to listen to this chapter';
        box.appendChild(document.createTextNode('🔊 '));
        box.appendChild(a);
      } else {
        return;
      }
      host.insertAdjacentElement('afterbegin', box);
    }).catch(function () {});
  }

  // Activity events for the signed-in reader (docs/analytics-cookbook.md):
  // the clip played and ended, with the span played, posted to /api/events
  // like the app's pages do. The 200 on the probe above means the reader is
  // signed in; nothing is sent otherwise, and a failure is silent.
  function watch(audio, slug) {
    var seq = 0;
    var playingSince = null;
    var page = Math.random().toString(16).slice(2, 18);
    var visit;
    try {
      visit = sessionStorage.getItem('tutor_visit');
      if (!visit || !/^[0-9a-f]{16}$/.test(visit)) {
        visit = Math.random().toString(16).slice(2, 18) + '0000000000000000';
        visit = visit.slice(0, 16);
        sessionStorage.setItem('tutor_visit', visit);
      }
    } catch (e) { visit = Math.random().toString(16).slice(2, 18); }
    function post(name) {
      try {
        var now = Date.now();
        var played = playingSince ? now - playingSince : 0;
        playingSince = name === 'book_narration_play' ? now : null;
        seq += 1;
        var body = JSON.stringify({ sent_at: now, events: [{
          name: name, seq: seq, client_ts: now, visit_id: visit, page_view_id: page,
          path: location.pathname, slug: slug, value_ms: played
        }] });
        fetch('/api/events', { method: 'POST', credentials: 'same-origin', keepalive: true,
                               headers: { 'Content-Type': 'application/json' }, body: body })
          .catch(function () {});
      } catch (e) { /* never into the page */ }
    }
    audio.addEventListener('play', function () { post('book_narration_play'); });
    audio.addEventListener('pause', function () { if (!audio.ended && playingSince) post('book_narration_ended'); });
    audio.addEventListener('ended', function () { post('book_narration_ended'); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
