"""track.js: the activity events every signed-in page records
(activity analytics, step 1.4).

The script is served by content version to a signed-in reader while
ACTIVITY_EVENTS is on, and to nobody else. In headless Chromium a page view,
a click, the tab going to the background, and the leave each become a row
with one visit id, one page view id, and increasing sequence numbers.
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from tests.browser_support import ORIGIN, app_client, open_page, student_db
from tutor import config
from tutor.db.models import ActivityEvent
from tutor.templating import STATIC_DIR

_TAG = re.compile(r'<script src="(/static/track\.js\?v=([0-9a-f]{12}))" data-heartbeat="(\d+)"></script>')


@pytest.fixture
def db():
    session = student_db()
    yield session
    session.close()


def test_a_signed_in_page_loads_the_script_by_content_version(db, monkeypatch):
    monkeypatch.setenv("ACTIVITY_HEARTBEAT_S", "45")
    config.get_config.cache_clear()
    with app_client(db) as client:
        for page in ("/chat?course=AST101", "/map?course=AST101", "/lectures", "/help", "/review?course=AST101"):
            html = client.get(page).text
            tag = _TAG.search(html)
            assert tag, page
            assert tag.group(2) == hashlib.sha256((STATIC_DIR / "track.js").read_bytes()).hexdigest()[:12]
            assert tag.group(3) == "45"
        served = client.get(tag.group(1))
        assert served.status_code == 200 and "javascript" in served.headers["content-type"]
        assert "window.tutorTrack = track" in served.text


def test_nobody_else_gets_the_script(db, monkeypatch):
    from fastapi.testclient import TestClient

    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    assert "track.js" not in TestClient(app).get("/auth/login").text      # no reader
    monkeypatch.setenv("ACTIVITY_EVENTS", "off")
    config.get_config.cache_clear()
    with app_client(db) as client:
        assert "track.js" not in client.get("/help").text                   # switched off


def test_the_heartbeat_setting_has_a_floor(monkeypatch):
    monkeypatch.setenv("ACTIVITY_HEARTBEAT_S", "3")
    config.get_config.cache_clear()
    assert config.get_config()["activity_heartbeat_s"] == 10
    monkeypatch.setenv("ACTIVITY_HEARTBEAT_S", "nope")
    config.get_config.cache_clear()
    assert config.get_config()["activity_heartbeat_s"] == 60
    monkeypatch.delenv("ACTIVITY_EVENTS_DAYS", raising=False)
    config.get_config.cache_clear()
    assert config.get_config()["activity_events_days"] is None
    monkeypatch.setenv("ACTIVITY_EVENTS_DAYS", "400")
    config.get_config.cache_clear()
    assert config.get_config()["activity_events_days"] == 400


# ---------------------------------------------------------------------------
# In the browser
# ---------------------------------------------------------------------------

@pytest.fixture
def chat(browser, db):
    with app_client(db) as client:
        with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
            yield page, net, db


def _rows(db):
    db.expire_all()
    return db.query(ActivityEvent).order_by(ActivityEvent.id).all()


def test_a_page_view_a_click_a_hidden_tab_and_the_leave_become_rows(chat):
    page, net, db = chat
    page.goto(f"{ORIGIN}/help")
    page.wait_for_function("() => typeof window.tutorTrack === 'function'")
    page.click("header a[href='/chat']")              # a control with static text
    page.wait_for_url(f"{ORIGIN}/chat**")
    page.wait_for_function("() => typeof window.tutorTrack === 'function'")
    # The tab goes to the background: the page records its on-screen time.
    page.evaluate("""() => {
        Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_function("() => window.tutorTrackFlush && true")
    page.evaluate("() => window.tutorTrackFlush()")
    page.wait_for_timeout(300)

    rows = _rows(db)
    # Two page views in one visit (the same tab); batches may land in either
    # order, so read each page view's events by its sequence numbers.
    assert len({r.visit_id for r in rows}) == 1
    by_page: dict[str, list] = {}
    for r in rows:
        by_page.setdefault(r.page_view_id, []).append(r)
    assert len(by_page) == 2
    help_rows, chat_rows = sorted(by_page.values(), key=lambda rs: min(r.id for r in rs))
    for group in (help_rows, chat_rows):
        group.sort(key=lambda r: r.seq)
        assert [r.seq for r in group] == list(range(1, len(group) + 1))   # nothing lost
    help_names = [r.name for r in help_rows]
    # On navigation the browser fires the hidden visibility change before or
    # after pagehide, so the leave is found by name, not by position.
    assert help_names[:2] == ["page_view", "click"] and "page_leave" in help_names[2:]
    assert set(help_names) <= {"page_view", "click", "page_hide", "page_leave"}
    help_view, click = help_rows[0], help_rows[1]
    leave = next(r for r in help_rows if r.name == "page_leave")
    assert help_view.path == "/help" and json.loads(help_view.detail_json)["vw"] == 1280
    assert click.target == "a" and click.label               # the brand link's own text
    assert json.loads(click.detail_json)["href"] == "/chat"   # where the link went (step 0.2)
    assert leave.value_ms is not None and leave.value_ms >= 0
    # (The test's output directory has no decks, so the chat also records deck_missing.)
    assert [r.name for r in chat_rows if r.name != "deck_missing"] == ["page_view", "page_hide"]
    chat_view = chat_rows[0]
    assert chat_view.path == "/chat" and json.loads(chat_view.detail_json)["ref"] == "/help"
    assert chat_view.course == "AST101"
    assert {r.student_id for r in rows} == {1} and all(r.login_session_id for r in rows)


def test_a_script_failure_and_a_missing_deck_become_rows(browser, db, tmp_path, monkeypatch):
    """Review step 0.2 (F52, F5): an error thrown in the page and an
    unhandled rejection are client_error rows with the error's name and no
    message; the chat's slides panel records the deck it could not load."""
    from tutor import decks

    # No deck on disk, whatever has been rendered into the repository's own
    # output/: the page's deck list is empty, so the panel asks for the deck,
    # and the /slides mount answers a frame's request for a missing one with
    # its 404 JSON.
    cfg = dict(config.get_config())
    cfg["output_dir"] = tmp_path
    monkeypatch.setattr(decks, "get_config", lambda: cfg)
    decks._reset_for_tests()
    try:
        with app_client(db) as client, \
                open_page(browser, client, width=1280, height=800, clock=False, errors_ok=True) as (page, net):
            net.stubs[("GET", "/slides/AST101/lecture-03/index.html")] = lambda route, url: route.fulfill(
                status=404, content_type="application/json", body='{"detail": "Not Found"}')
            _script_failure_and_missing_deck(page, db)
    finally:
        decks._reset_for_tests()             # the empty list must not outlive the test


def _script_failure_and_missing_deck(page, db):
    page.goto(f"{ORIGIN}/help")
    page.wait_for_function("() => typeof window.tutorTrack === 'function'")
    page.evaluate("() => { setTimeout(() => { throw new TypeError('the message stays in the browser'); }, 0); }")
    page.evaluate("() => { setTimeout(() => { Promise.reject(new RangeError('nor this one')); }, 0); }")
    page.wait_for_timeout(200)
    page.evaluate("() => window.tutorTrackFlush()")
    page.wait_for_timeout(300)
    errors = [r for r in _rows(db) if r.name == "client_error"]
    details = [json.loads(r.detail_json) for r in errors]
    assert [d["error"] for d in details] == ["TypeError", "RangeError"]
    assert all("message" not in d and "line" in d for d in details)
    assert "the message stays" not in "".join(r.detail_json or "" for r in errors)

    page.goto(f"{ORIGIN}/chat?course=AST101&lecture_num=3")
    page.wait_for_selector("[data-deck-missing]", state="visible")
    page.evaluate("() => window.tutorTrackFlush()")
    page.wait_for_timeout(300)
    (missing,) = [r for r in _rows(db) if r.name == "deck_missing"]
    assert (missing.course, missing.lecture_num, missing.path) == ("AST101", 3, "/chat")


def test_a_page_script_can_add_its_own_events(chat):
    page, net, db = chat
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.wait_for_function("() => typeof window.tutorTrack === 'function'")
    page.evaluate("""() => {
        window.tutorTrack('question_sent', {chars: 12, kind: 'typed', lecture_num: 3, turn_id: null});
        window.tutorTrack('reply_done', {turn_id: 'abc123', message_id: 9, value_ms: 2500, chars: 80, outcome: 'done'});
        window.tutorTrackFlush();
    }""")
    page.wait_for_timeout(300)
    rows = _rows(db)
    by_name = {r.name: r for r in rows}
    sent, done = by_name["question_sent"], by_name["reply_done"]
    assert (sent.lecture_num, sent.course, json.loads(sent.detail_json)) == (3, "AST101", {"chars": 12, "kind": "typed"})
    assert (done.turn_id, done.message_id, done.value_ms) == ("abc123", 9, 2500)
    assert done.seq == sent.seq + 1


# ---------------------------------------------------------------------------
# The other pages (plan step 1.7)
# ---------------------------------------------------------------------------

def test_the_lecture_catalog_names_its_links_as_lecture_open(db, tmp_path, monkeypatch):
    from tutor.routers import lectures as lectures_mod

    out = tmp_path / "output"
    lec = out / "AST101" / "lecture-03"
    lec.mkdir(parents=True)
    (lec / "index.html").write_text("<html></html>")
    (lec / "content.md").write_text("## Slide 1: Seasons\n")
    cfg = dict(config.get_config())
    cfg["output_dir"], cfg["lecture_audio_dir"] = out, tmp_path / "audio"
    monkeypatch.setattr(lectures_mod, "get_config", lambda: cfg)
    with app_client(db) as client:
        html = client.get("/lectures").text
    assert 'data-track="lecture-open"' in html and 'data-event="lecture_open"' in html
    assert "data-event-fields='{\"course\": \"AST101\", \"lecture_num\": 3}'" in html


def test_a_data_event_control_adds_its_named_event(chat):
    page, net, db = chat
    page.goto(f"{ORIGIN}/help")
    page.wait_for_function("() => typeof window.tutorTrack === 'function'")
    page.evaluate("""() => {
        const a = document.createElement('button');
        a.type = 'button';
        a.textContent = 'Lecture 3';
        a.setAttribute('data-track', 'lecture-open');
        a.setAttribute('data-event', 'lecture_open');
        a.setAttribute('data-event-fields', JSON.stringify({course: 'AST101', lecture_num: 3}));
        document.body.appendChild(a);
        a.click();
        window.tutorTrackFlush();
    }""")
    page.wait_for_timeout(300)
    rows = _rows(db)
    names = [r.name for r in rows]
    assert names.index("click") + 1 == names.index("lecture_open")
    opened = rows[names.index("lecture_open")]
    assert (opened.course, opened.lecture_num, opened.path) == ("AST101", 3, "/help")
    assert rows[names.index("click")].target == "lecture-open"


def test_the_map_review_and_book_scripts_name_their_events():
    root = STATIC_DIR.parent.parent.parent
    assert "window.tutorTrack('goal_click'" in (STATIC_DIR / "map.js").read_text()
    review = (root / "tutor" / "web" / "templates" / "review.html").read_text()
    assert "this.track('review_start')" in review and "this.track('review_answer')" in review
    book = (root / "book" / "narration.js").read_text()
    assert "'book_narration_play'" in book and "'book_narration_ended'" in book
    assert "fetch('/api/events'" in book
