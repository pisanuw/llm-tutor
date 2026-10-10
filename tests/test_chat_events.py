"""The chat page's own activity events (activity analytics, step 1.5), in
headless Chromium against a stubbed tutor stream.

A question and its reply leave question_sent, reply_started, reply_first_token,
slide_cited and reply_done rows that share the stream's turn id and name the
saved reply; the lecture picker, the course picker, the phone's panel toggle,
and a thumbs-up each leave their own row.
"""
from __future__ import annotations

import json

import pytest

from tests.browser_support import ORIGIN, app_client, open_page, sse, student_db
from tutor.db.models import ActivityEvent

QUESTION = "What is a telescope for?"
REPLY = "A telescope gathers light."
SLIDE = {"type": "slide", "url": "/slides/AST101/lecture-04/index.html", "title": "Telescopes", "num": 7}


@pytest.fixture
def db():
    session = student_db()
    yield session
    session.close()


@pytest.fixture
def client(db, no_decks):
    with app_client(db) as c:
        yield c


@pytest.fixture
def no_decks(tmp_path, monkeypatch):
    """No deck on disk, whatever the working tree's output/ holds: the
    /slides mount and the page's deck list both read an empty folder."""
    from tutor import decks
    from tutor.main import app

    real = decks.get_config
    monkeypatch.setattr(decks, "get_config", lambda: {**real(), "output_dir": tmp_path})
    mount = next(r for r in app.routes if getattr(r, "name", "") == "slides")
    monkeypatch.setattr(mount.app, "all_directories", [str(tmp_path)])
    decks._reset_for_tests()
    yield tmp_path
    decks._reset_for_tests()


def _stub_turn(net, message_id=42):
    net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
        status=200, content_type="text/event-stream",
        body=sse([{"type": "turn", "id": "feedfacecafebeef"}, SLIDE,
                  {"type": "token", "text": REPLY[:9]}, {"type": "token", "text": REPLY[9:]},
                  {"type": "done", "full_text": REPLY, "message_id": message_id}]))
    net.stubs[("POST", "/chat/feedback")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"ok": True, "message_id": message_id, "rating": 1}))


def _flush(page):
    page.evaluate("() => window.tutorTrackFlush && window.tutorTrackFlush()")
    page.wait_for_timeout(300)


def _rows(db):
    db.expire_all()
    rows = db.query(ActivityEvent).order_by(ActivityEvent.seq).all()
    return [r for r in rows if r.name not in ("page_view", "page_hide", "page_show",
                                              "page_heartbeat", "page_leave", "click")]


def _by_name(rows):
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r.name, []).append(r)
    return out


def test_a_question_and_its_reply_leave_their_events_on_one_turn(browser, client, db):
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        _stub_turn(net)
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_function("() => typeof window.tutorTrack === 'function'")
        page.fill("#msg-input", QUESTION)
        page.click("[data-track=send]")
        page.wait_for_selector(f"#messages >> text={REPLY}")
        page.click("#messages button[aria-label='This was helpful']")
        page.wait_for_timeout(200)
        _flush(page)

    # (The test's output directory has no decks, so the page also records deck_missing.)
    rows = [r for r in _rows(db) if r.name != "deck_missing"]
    names = [r.name for r in rows]
    # The stub cites the slide before its first token, as a tool round does.
    assert names == ["question_sent", "reply_started", "slide_cited", "reply_first_token",
                     "reply_done", "feedback"], names
    sent, started, cited, first, done, fb = rows
    assert json.loads(sent.detail_json) == {"chars": len(QUESTION), "kind": "typed"}
    assert sent.course == "AST101" and sent.lecture_num == 1 and sent.turn_id is None
    assert {started.turn_id, first.turn_id, cited.turn_id, done.turn_id} == {"feedfacecafebeef"}
    assert started.value_ms is not None and first.value_ms >= started.value_ms
    assert (cited.lecture_num, cited.slide_num, cited.course) == (4, 7, "AST101")
    assert (done.message_id, done.value_ms >= first.value_ms) == (42, True)
    assert json.loads(done.detail_json) == {"chars": len(REPLY), "outcome": "done"}
    assert (fb.message_id, json.loads(fb.detail_json)) == (42, {"rating": 1})
    # The sequence is unbroken across the page's own events and the clicks.
    db.expire_all()
    seqs = [r.seq for r in db.query(ActivityEvent).order_by(ActivityEvent.seq)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_the_pickers_and_the_phone_panel_leave_their_events(browser, client, db):
    with open_page(browser, client, width=390, height=844, clock=False) as (page, net):
        _stub_turn(net)
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_function("() => typeof window.tutorTrack === 'function'")
        page.click("[data-track=panel-toggle]")            # slides
        page.select_option("select[aria-label=Lecture]", "4")
        page.click("[data-track=panel-toggle]")            # back to the chat
        page.select_option("select[aria-label=Course]", "AST102")
        page.wait_for_timeout(200)
        _flush(page)

    by = _by_name(_rows(db))
    assert [json.loads(r.detail_json)["to"] for r in by["panel_toggle"]] == ["slides", "chat"]
    (pick,) = by["lecture_select"]
    assert json.loads(pick.detail_json) == {"from": 1, "to": 4} and pick.lecture_num == 4
    (switch,) = by["course_switch"]
    assert json.loads(switch.detail_json) == {"from": "AST101", "to": "AST102"} and switch.course == "AST102"


def test_a_reply_that_fails_is_recorded_with_its_outcome(browser, client, db):
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "turn", "id": "abc"}, {"type": "token", "text": "Half"},
                      {"type": "error", "message": "The tutor is down."}]))
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_function("() => typeof window.tutorTrack === 'function'")
        page.fill("#msg-input", "hi")
        page.click("[data-track=send]")
        page.wait_for_selector("#messages >> text=The tutor is down.")
        _flush(page)

    by = _by_name(_rows(db))
    (done,) = by["reply_done"]
    assert (done.turn_id, json.loads(done.detail_json)) == ("abc", {"chars": 4, "outcome": "error"})
