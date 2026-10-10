"""Run app pages in headless Chromium for the browser tests.

The ``browser`` fixture (tests/conftest.py) starts Chromium once per module
and skips the module where Playwright, or a Chromium it can launch, is
missing; CI installs neither yet. ``open_page`` gives a page whose requests
reach the app through Playwright's router and a TestClient, so a test needs
no server, and ``Net`` lets it cut the network or stub a path:

    page, net = open_page(browser, client)
    page.goto(f"{ORIGIN}/chat?course=AST101")
"""
from __future__ import annotations

import contextlib
import json
import os
from urllib.parse import urlsplit

from tests.helpers import make_session

ORIGIN = "http://tutor.test"


def student_db(email="student@example.edu", name="Sam", courses=("AST101",)):
    """An in-memory database with one whitelisted student (id 1), a
    lecture-order track per course, and a path in each of ``courses`` (so the
    chat page opens instead of redirecting to /welcome)."""
    from tutor.db.models import Student, StudentPath, Track, WhitelistEntry

    s = make_session()
    s.add(Student(id=1, email=email, name=name))
    s.add(WhitelistEntry(email=email))
    for course in ("AST101", "AST102"):
        s.add(Track(id=f"{course}-lecture-order", course=course, title="Lecture order",
                    goal_ids_json="[]"))
    s.commit()
    for course in courses:
        s.add(StudentPath(student_id=1, course=course, track_id=f"{course}-lecture-order"))
    s.commit()
    return s


@contextlib.contextmanager
def app_client(db, student_id=1):
    """A TestClient on the app using ``db``, signed in as ``student_id``."""
    from fastapi.testclient import TestClient

    import tutor.main as main_mod
    from tutor.auth.deps import get_db
    from tutor.auth.jwt_utils import create_token
    from tutor.db.models import Session as SessionModel

    main_mod.app.dependency_overrides[get_db] = lambda: db
    token, jti, expires = create_token(student_id)
    db.add(SessionModel(student_id=student_id, jwt_jti=jti, expires_at=expires, method="magic"))
    db.commit()
    client = TestClient(main_mod.app)
    client.cookies.set("session_token", token)
    try:
        yield client
    finally:
        main_mod.app.dependency_overrides.clear()


def expect(target):
    """Playwright's auto-waiting assertions. Use them for anything an action
    changes: Alpine updates the DOM a moment after the event, so a bare
    ``is_visible`` right after a click can read the old state. Imported late,
    so a module that uses it still skips where Playwright is missing."""
    from playwright.sync_api import expect as _expect

    return _expect(target)


def sse(events) -> str:
    """A text/event-stream body carrying ``events``."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def launch(pw):
    """The Chromium this Playwright ships, else CHROMIUM_PATH, else the cloud
    container's copy (its revision can differ from the installed package)."""
    try:
        return pw.chromium.launch()
    except Exception:
        for exe in (os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"):
            if exe and os.path.exists(exe):
                return pw.chromium.launch(executable_path=exe)
        raise


class Net:
    """The page's network: every request goes to the app unless the test has
    cut the network (``go_offline``) or stubbed its method and path in
    ``stubs`` with a ``(route, url)`` callable. ``seen`` lists every request."""

    def __init__(self, client, context):
        self.client = client
        self.context = context
        self.offline = False
        self.stubs: dict = {}
        self.seen: list[str] = []

    def go_offline(self):
        # Emulation flips navigator.onLine and fires the offline event, but
        # routed requests would still reach the handler, so fail them here.
        self.offline = True
        self.context.set_offline(True)

    def go_online(self):
        self.offline = False
        self.context.set_offline(False)

    def handle(self, route):
        req = route.request
        url = urlsplit(req.url)
        self.seen.append(f"{req.method} {url.path}{'?' + url.query if url.query else ''}")
        if self.offline:
            route.abort("internetdisconnected")
            return
        stub = self.stubs.get((req.method, url.path))
        if stub is not None:
            stub(route, url)
            return
        headers = {k: v for k, v in req.headers.items() if k.lower() in ("content-type", "accept")}
        resp = self.client.request(req.method, url.path + (f"?{url.query}" if url.query else ""),
                                   content=req.post_data_buffer, headers=headers,
                                   follow_redirects=False)
        keep = {k: v for k, v in resp.headers.items()
                if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")}
        route.fulfill(status=resp.status_code, headers=keep, body=resp.content)


@contextlib.contextmanager
def open_page(browser, client, width=390, height=844, clock=True, reduced_motion=None,
              touch=False, errors_ok=False):
    """(page, net) at ``width`` x ``height``, with a fake clock installed when
    ``clock`` (time still flows; ``page.clock.fast_forward`` skips ahead).
    ``reduced_motion="reduce"`` emulates the system setting; ``touch`` a
    phone's touch screen (a coarse pointer). Page errors fail the test when
    the block exits, unless ``errors_ok`` (a test that throws on purpose)."""
    context = browser.new_context(viewport={"width": width, "height": height},
                                  reduced_motion=reduced_motion or "no-preference",
                                  has_touch=touch, is_mobile=touch)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    net = Net(client, context)
    # A predicate, not a glob: the glob form missed URLs with a query string.
    page.route(lambda url: url.startswith(ORIGIN + "/"), net.handle)
    net.stubs[("GET", "/chat/starters")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json", body='{"starters": []}')
    if clock:
        page.clock.install()
    try:
        yield page, net
    finally:
        context.close()
    assert errors_ok or errors == [], errors


# An axe-style check of what a page offers the pointer and the ear (plan step
# 3.4): every visible control needs a name that has a letter or digit in it
# (an aria-label, a <label>, or its own text, where emoji and aria-hidden
# parts do not count), and a target of at least 44 x 44 CSS px. Links inside
# a sentence or list item with other text are exempt from the size rule, as
# in WCAG 2.5.5.
_AUDIT_JS = r"""
(root) => {
  const scope = root ? document.querySelector(root) : document;
  const sel = 'a[href], button, select, textarea, input:not([type=hidden]), summary, ' +
              '[role=button], [tabindex]:not([tabindex="-1"])';
  const hasWord = (t) => /[\p{L}\p{N}]/u.test(t || '');
  const visible = (el) => {
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const ownText = (el) => {
    const c = el.cloneNode(true);
    c.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove());
    c.querySelectorAll('img[alt]').forEach((img) => img.replaceWith(img.alt));
    return c.textContent.replace(/\s+/g, ' ').trim();
  };
  const name = (el) => {
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || '')
                     .join(' ').trim();
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    if (el.labels && el.labels.length) return [...el.labels].map((l) => l.textContent).join(' ').trim();
    if (/^(A|BUTTON|SUMMARY)$/.test(el.tagName) || el.getAttribute('role') === 'button') return ownText(el);
    return '';
  };
  const inline = (el) => {
    if (el.tagName !== 'A') return false;
    const block = el.parentElement && el.parentElement.closest('p, li');
    return !!block && hasWord(block.textContent.replace(el.textContent, ''));
  };
  const out = [];
  for (const el of scope.querySelectorAll(sel)) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    const label = name(el);
    out.push({
      what: `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''} "${(label || ownText(el)).slice(0, 50)}"`,
      named: hasWord(label),
      small: !inline(el) && (r.width < 43.5 || r.height < 43.5),
      size: `${Math.round(r.width)}x${Math.round(r.height)}`,
    });
  }
  return out;
}
"""


def audit(page, root=None) -> dict:
    """``{"unnamed": [...], "small": [...], "checked": n}`` for the visible
    controls on ``page`` (or under the ``root`` selector)."""
    found = page.evaluate(_AUDIT_JS, root)
    return {
        "unnamed": [c["what"] for c in found if not c["named"]],
        "small": [f'{c["what"]} {c["size"]}' for c in found if c["small"]],
        "checked": len(found),
    }
