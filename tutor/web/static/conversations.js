// The recent-conversations list (docs/conversations-list-plan-2026-10.md):
// a row expands in place. Clicking a row, or its ▾ button, toggles the
// thread under it; the first time, the messages are fetched from the URL
// in the row's data-thread-url (an HTML fragment the server rendered and
// escaped) and inserted. Links inside the row keep working as links.
// Nothing in here may throw into the page.
(function () {
  'use strict';
  if (!window.fetch || !document.querySelectorAll) return;

  function threadBox(row) {
    // A table row keeps its thread in the hidden row after it; a card holds it inside.
    var inside = row.querySelector('[data-thread]');
    if (inside) return { holder: inside, box: inside };
    var next = row.nextElementSibling;
    if (next && next.hasAttribute('data-thread-row')) {
      return { holder: next, box: next.querySelector('[data-thread]') };
    }
    return null;
  }

  function setOpen(row, parts, open) {
    parts.holder.hidden = !open;
    var button = row.querySelector('[data-expand]');
    if (button) {
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      button.textContent = open ? '▴' : '▾';
    }
    row.setAttribute('data-open', open ? '1' : '0');
  }

  function load(row, parts) {
    if (parts.box.getAttribute('data-loaded')) return;
    parts.box.setAttribute('data-loaded', '1');
    parts.box.textContent = 'Loading…';
    fetch(row.getAttribute('data-thread-url'), { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (html) { parts.box.innerHTML = html; })
      .catch(function () {
        parts.box.removeAttribute('data-loaded');
        parts.box.textContent = 'Could not load the messages. Click again to retry.';
      });
  }

  function toggle(row) {
    var parts = threadBox(row);
    if (!parts) return;
    var open = row.getAttribute('data-open') !== '1';
    setOpen(row, parts, open);
    if (open) load(row, parts);
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!(target instanceof Element)) return;
    var row = target.closest('[data-conversation]');
    if (!row) return;
    var control = target.closest('a, button, input, select, label');
    if (control && !control.hasAttribute('data-expand')) return;   // a real link or control
    if (target.closest('[data-thread]')) return;                   // inside the opened thread
    event.preventDefault();
    toggle(row);
  });
})();
