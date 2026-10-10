"""Error pages students hit are recorded, shown as pages, and reported
(tutor/request_errors.py; review step 0.1).

F1: a dead link showed raw JSON with no header. F2: nothing recorded a 4xx.
Done when: a navigation to a 404 renders the page with the shared header and
a reference, the row names the student and the same-site referrer, a dead
link from a page emails the operator once, a bot does not, a fetch keeps its
JSON, "Tell <instructor>" writes a report row and emails, and the digest and
the Today panel show the day's errors.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest
from fastapi.testclient import TestClient

from tutor import alerts, request_errors
from tutor.courses import IDENTITY
from tutor.db.models import RequestError, Student, WhitelistEntry
from tutor.utctime import utcnow
from tests.helpers import login_cookie, make_session

HTML = {"accept": "text/html,application/xhtml+xml"}
CHROME = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"}
# The error page's button names the configured instructor (courses.yaml).
TELL = f"Tell {IDENTITY.instructor_short}"


@pytest.fixture
def setup(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app

    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import tutor.config as config

    config.get_config.cache_clear()
    db = make_session()
    db.add(Student(id=1, email="ada@example.edu", name="Ada"))
    db.add(Student(id=2, email="prof@example.edu", name="Prof"))
    db.add(WhitelistEntry(email="ada@example.edu", added_by="test"))      # the whitelist is on by default
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: sent.append((key, subject, body)) or "queued")
    client = TestClient(app)
    try:
        yield client, db, sent
    finally:
        app.dependency_overrides.clear()
        db.close()


def _signed_in(client, db, student_id=1):
    client.cookies.set("session_token", login_cookie(db, student_id))
    return client


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_the_path_is_stored_without_query_or_token():
    assert request_errors.scrub_path("/auth/magic/abc123DEF?next=%2Fchat") == "/auth/magic/[token]"
    assert request_errors.scrub_path("/slides/AST101/lecture-09/index.html?v=abc") == "/slides/AST101/lecture-09/index.html"
    assert len(request_errors.scrub_path("/x" * 300)) == request_errors.PATH_MAX


def test_agents_and_kinds():
    assert request_errors.classify_agent(CHROME["user-agent"]) == "browser"
    assert request_errors.classify_agent("Mozilla/5.0 (compatible; Googlebot/2.1)") == "bot"
    assert request_errors.classify_agent("Mozilla/5.0 Proofpoint URLDefense") == "scanner"
    assert request_errors.classify_agent("") == "bot"
    assert request_errors.kind_for("/slides/AST101/lecture-09/index.html", True) == "deck"
    assert request_errors.kind_for("/slides/AST101/lecture-02/audio/slide-03.mp3", False) == "audio"
    assert request_errors.kind_for("/lecture-audio/book/intro.mp3", False) == "audio"
    assert request_errors.kind_for("/book/99-missing.html", True) == "book"
    assert request_errors.kind_for("/explorables/x.html", True) == "explorable"
    assert request_errors.kind_for("/chat/feedback", False) == "fetch"
    assert request_errors.kind_for("/no-such-page", True) == "page"
    assert request_errors.family("/slides/AST101/lecture-09/index.html") == "/slides/AST101"
    assert request_errors.family("/no-such-page") == "/no-such-page"


def test_only_a_same_site_referrer_is_kept():
    assert request_errors.same_site_referrer("https://tutor.example.edu/map?course=AST101", "tutor.example.edu") == "/map"
    assert request_errors.same_site_referrer("https://canvas.example.edu/courses/1", "tutor.example.edu") is None
    assert request_errors.same_site_referrer("/chat", "tutor.example.edu") == "/chat"
    assert request_errors.same_site_referrer(None, "tutor.example.edu") is None


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------

def test_a_signed_in_navigation_to_a_dead_link_gets_the_page_and_a_row(setup):
    client, db, sent = setup
    _signed_in(client, db)
    resp = client.get("/no-such-page", headers={**HTML, **CHROME, "referer": "http://testserver/map"})
    assert resp.status_code == 404 and resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert html.count("<header") == 1 and 'aria-label="Main"' in html      # the shared header
    assert "That page is not here" in html and "Back to the chat" in html
    assert 'name="ref"' in html and TELL in html
    (row,) = db.query(RequestError).all()
    assert (row.status, row.method, row.path, row.referrer_path) == (404, "GET", "/no-such-page", "/map")
    assert (row.student_id, row.kind, row.agent) == (1, "page", "browser")
    assert row.ref in html and len(row.ref) == 6
    # A dead link on this site, followed by a student: the operator hears now.
    assert [(k, s) for k, s, _ in sent] == [("404:/no-such-page", "Dead link: /no-such-page (from /map)")]
    assert "Ada <ada@example.edu>" in sent[0][2] and row.ref in sent[0][2]


def test_a_missing_deck_offers_the_lecture_list(setup, tmp_path, monkeypatch):
    client, db, sent = setup
    _signed_in(client, db)
    resp = client.get("/slides/AST101/lecture-09/index.html", headers={**HTML, **CHROME, "referer": "http://testserver/chat"})
    assert resp.status_code == 404
    assert "Those slides are not here" in resp.text and 'href="/lectures"' in resp.text
    (row,) = db.query(RequestError).all()
    assert (row.kind, row.referrer_path) == ("deck", "/chat")
    assert sent[0][0] == "404:/slides/AST101"


def test_the_deck_iframe_keeps_the_json_its_script_detects(setup):
    """chat.js relayoutDeck shows its own sentence when the frame's document
    is not HTML; a page with scripts inside the panel would break that (and
    record a page view from inside the frame)."""
    client, db, sent = setup
    _signed_in(client, db)
    resp = client.get("/slides/AST101/lecture-09/index.html",
                      headers={**HTML, **CHROME, "sec-fetch-dest": "iframe", "referer": "http://testserver/chat"})
    assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}
    (row,) = db.query(RequestError).all()
    assert (row.kind, row.referrer_path, row.student_id) == ("deck", "/chat", 1)
    assert sent[0][0] == "404:/slides/AST101"                                         # still a dead link


def test_a_stale_bookmark_a_bot_and_a_fetch_are_recorded_but_never_alert(setup):
    client, db, sent = setup
    _signed_in(client, db)
    client.get("/no-such-page", headers={**HTML, **CHROME})                       # no referrer: a bookmark
    client.get("/no-such-page", headers={**HTML, "user-agent": "Googlebot/2.1", "referer": "http://testserver/map"})
    fetched = client.get("/chat/history?course=999", headers={**CHROME, "referer": "http://testserver/chat"})
    assert fetched.status_code == 422 and fetched.json()["detail"]                 # the JSON its script expects
    rows = db.query(RequestError).order_by(RequestError.id).all()
    assert [(r.kind, r.agent, r.referrer_path) for r in rows] == [
        ("page", "browser", None), ("page", "bot", "/map"), ("fetch", "browser", "/chat")]
    assert sent == []


def test_signed_out_gets_the_page_with_a_login_link_and_no_student(setup):
    client, db, sent = setup
    resp = client.get("/no-such-page", headers={**HTML, **CHROME})
    assert resp.status_code == 404
    assert 'href="/auth/login"' in resp.text and TELL not in resp.text
    (row,) = db.query(RequestError).all()
    assert row.student_id is None and sent == []


def test_a_browsers_own_requests_are_never_recorded(setup):
    """Every browser asks for /favicon.ico on its own; with no route it was
    a 404 counted against the student on every page view (the review of
    step 0.9). The icon is served now, and the automatic paths are skipped."""
    client, db, sent = setup
    _signed_in(client, db)
    resp = client.get("/favicon.ico", headers={**CHROME, "accept": "image/*", "sec-fetch-dest": "image",
                                               "referer": "http://testserver/chat"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    for path in ("/apple-touch-icon-precomposed.png", "/.well-known/security.txt"):
        resp = client.get(path, headers={**CHROME, "accept": "image/*", "referer": "http://testserver/chat"})
        assert resp.status_code == 404
    assert db.query(RequestError).count() == 0 and sent == []


def test_the_health_probe_is_never_recorded(setup):
    client, db, _sent = setup
    from tutor.routers import status

    status._reset_for_tests()
    for _ in range(status.PER_IP_PER_MINUTE + 2):
        client.get("/healthz")
    assert db.query(RequestError).count() == 0


def test_an_expired_session_is_still_the_login_redirect(setup):
    client, db, _sent = setup
    resp = client.get("/chat?course=AST102", headers=HTML, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/login?next=")
    assert db.query(RequestError).count() == 0                                     # the login trail's business


def test_tell_the_instructor_writes_a_report_and_emails_once_an_hour(setup):
    client, db, sent = setup
    _signed_in(client, db)
    page = client.get("/no-such-page", headers={**HTML, **CHROME})
    ref = db.query(RequestError).one().ref
    assert ref in page.text
    resp = client.post("/errors/report", data={"ref": ref}, headers={**HTML, **CHROME})
    assert resp.status_code == 200 and "That has been reported" in resp.text
    report = db.query(RequestError).filter_by(kind="report").one()
    assert (report.student_id, report.path, report.status, report.detail) == (1, "/no-such-page", 404, ref)
    assert [(k, s) for k, s, _ in sent] == [("report:1", "A student reported a problem: Ada")]
    assert "/no-such-page answered 404" in sent[0][2] and f"reference {ref}" in sent[0][2]


def test_a_client_that_left_mid_request_is_not_a_500(setup):
    """A disconnect while the body is read reaches the unhandled handler on
    any route: no alert, no reference, no row; the response nobody reads
    is a 400, not a 500 (production, 2026-10-07)."""
    from starlette.requests import ClientDisconnect, Request

    from tutor import main

    client, db, sent = setup
    _signed_in(client, db)
    scope = {"type": "http", "method": "POST", "path": "/api/events", "root_path": "", "scheme": "http",
             "query_string": b"", "server": ("testserver", 80), "client": ("10.0.0.9", 4), "app": main.app,
             "headers": [(b"host", b"testserver"), (b"user-agent", CHROME["user-agent"].encode()),
                         (b"accept", b"*/*"), (b"cookie", f"session_token={client.cookies['session_token']}".encode())]}
    before = main._last_alert_job
    resp = asyncio.run(main._alert_on_unhandled(Request(scope), ClientDisconnect()))
    assert resp.status_code == 400 and "reference" not in resp.body.decode()
    assert main._last_alert_job is before                  # nothing was scheduled
    assert db.query(RequestError).count() == 0 and sent == []


def test_a_500_page_names_its_reference_and_the_alert_and_row_carry_it(setup):
    import tutor.main as main
    from starlette.requests import Request

    client, db, sent = setup
    scope = {"type": "http", "method": "GET", "path": "/x", "query_string": b"", "app": main.app,
             "headers": [(b"accept", b"text/html"), (b"user-agent", CHROME["user-agent"].encode())]}
    resp = asyncio.run(main._alert_on_unhandled(Request(scope), RuntimeError("boom")))
    assert resp.status_code == 500 and resp.headers["x-content-type-options"] == "nosniff"
    body = resp.body.decode()
    assert "Something broke on our side" in body
    asyncio.run(_wait(main))
    (row,) = db.query(RequestError).all()
    assert (row.status, row.path, row.kind, row.detail) == (500, "/x", "page", "RuntimeError")
    assert row.ref in body
    assert sent[0][0] == "exc:RuntimeError:/x" and f"reference {row.ref}" in sent[0][2]


async def _wait(main):
    await main._last_alert_job


# ---------------------------------------------------------------------------
# What the instructor reads
# ---------------------------------------------------------------------------

def _row(db, path, *, status=404, student_id=1, agent="browser", kind="page", referrer="/map", minutes=5, **kw):
    r = RequestError(ts=utcnow() - datetime.timedelta(minutes=minutes), status=status, method="GET", path=path,
                     referrer_path=referrer, student_id=student_id, kind=kind, agent=agent,
                     ref=request_errors.new_ref(), **kw)
    db.add(r)
    db.commit()
    return r


def test_the_summary_and_the_digest_section(setup):
    client, db, _sent = setup
    _row(db, "/slides/AST101/lecture-09/index.html", kind="deck")
    _row(db, "/slides/AST101/lecture-09/index.html", kind="deck", student_id=2, referrer="/chat")
    _row(db, "/no-such-page", referrer=None)
    _row(db, "/no-such-page", agent="bot", student_id=None)
    _row(db, "/no-such-page", status=0, kind="report", referrer=None, detail="ABC123")
    s = request_errors.summary(db, utcnow() - datetime.timedelta(hours=24))
    assert (s.total, s.bots, s.students) == (3, 1, 2)
    assert s.by_kind == {"deck": 2, "page": 1}
    assert [(t.path, t.hits, t.students, t.referrers) for t in s.top] == [
        ("/slides/AST101/lecture-09/index.html", 2, 2, ["/chat", "/map"]), ("/no-such-page", 1, 1, [])]
    assert [(who, path) for _, who, path in s.reports] == [("Ada", "/no-such-page")]

    from tutor.reporting import render_errors_markdown

    md = render_errors_markdown(s)
    assert "## Dead links and errors" in md
    assert "**From students' browsers:** 3 (2 deck, 1 page), 2 students; 1 from bots or scanners" in md
    assert "| `/slides/AST101/lecture-09/index.html` | 404 | 2 | 2 | `/chat`, `/map` |" in md
    assert f"**Ada** pressed {TELL} on `/no-such-page`" in md
    assert render_errors_markdown(request_errors.ErrorSummary()) == ""

    from scripts.daily_report import is_quiet_day

    assert is_quiet_day([], None, s) is False


def test_the_today_panel_lists_the_errors(setup, monkeypatch):
    client, db, _sent = setup
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    import tutor.config as config

    config.get_config.cache_clear()
    _row(db, "/slides/AST101/lecture-09/index.html", kind="deck")
    _row(db, "/no-such-page", status=0, kind="report", referrer=None, detail="ABC123")
    from tutor.routers.admin import issue_admin_session

    client.cookies.set("admin_session", issue_admin_session())
    html = client.get("/admin").text
    assert "Errors students hit, last 24 hours (1)" in html
    assert "/slides/AST101/lecture-09/index.html" in html and "from /map" in html
    assert f"Ada pressed {TELL}" in html


# ---------------------------------------------------------------------------
# On a phone
# ---------------------------------------------------------------------------

def test_the_error_page_fits_a_phone_and_names_its_controls(browser, setup):
    """The 390px rule (CLAUDE.md): no horizontal scroll, every control named
    and at least 44px, signed in and signed out."""
    from tests.browser_support import ORIGIN, audit, open_page

    client, db, _sent = setup
    _signed_in(client, db)
    with open_page(browser, client, clock=False) as (page, net):
        page.goto(f"{ORIGIN}/no-such-page")
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        found = audit(page)
        assert found["checked"] >= 3 and found["unnamed"] == [] and found["small"] == []
        assert page.locator(f"button:has-text('{TELL}')").is_visible()
        page.click(f"button:has-text('{TELL}')")
        page.wait_for_selector("text=that has been reported")
    client.cookies.clear()
    with open_page(browser, client, clock=False) as (page, net):
        # (A gated URL answers a redirect, which the test router cannot follow.)
        page.goto(f"{ORIGIN}/no-such-page")
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        found = audit(page)
        assert found["unnamed"] == [] and found["small"] == []
        assert page.locator("a:has-text('Log in')").first.is_visible()
