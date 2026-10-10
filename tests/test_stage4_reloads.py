"""Reloads, restarts and double submits (bulletproofing Stage 4).

R30: a reload mid-reply showed the question with no reply and no sign one
was coming, and asking again said "busy". R31: a restart mid-reply left the
question unanswered with no word and the turn in progress in the ledger.
R32: a double tap on Sign in or Continue failed after the first had worked.
R33: on an iPhone the speaker failed the first time, though the clip was
fetched.
"""
from __future__ import annotations

import asyncio
import datetime
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from tutor import streambuf
from tutor.agent import state
from tutor.db.models import MagicLink, Message, Student, TurnEvent, WhitelistEntry
from tutor.utctime import utcnow
from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tests.helpers import login_cookie, make_session


@pytest.fixture
def site(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers import chat as chat_mod

    db = make_session(Student(id=1, email="ada@example.edu", name="Ada"), WhitelistEntry(email="@example.edu", added_by="t"))
    monkeypatch.setattr(chat_mod, "_turn_session", lambda: sessionmaker(bind=db.get_bind())())
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session_token", login_cookie(db, 1))
    streambuf._reset_for_tests()
    try:
        yield app, client, db
    finally:
        app.dependency_overrides.clear()
        streambuf._reset_for_tests()
        db.close()


def _events(text: str) -> list[dict]:
    return [json.loads(p.strip()[6:]) for p in text.split("\n\n") if p.strip().startswith("data: ")]


# ---------------------------------------------------------------------------
# R30: the page can find the reply that is still coming
# ---------------------------------------------------------------------------

def test_the_running_reply_is_found_by_student_and_course(site):
    _, client, _ = site
    streambuf.create("t-other-student", student_id=2, course="AST101")
    finished = streambuf.create("t-finished", student_id=1, course="AST101")
    asyncio.run(finished.finish())
    assert client.get("/chat/running?course=AST101").json() == {"turn_id": None}
    streambuf.create("t-live", student_id=1, course="AST101")
    assert client.get("/chat/running?course=AST101").json() == {"turn_id": "t-live"}
    assert client.get("/chat/running?course=AST102").json() == {"turn_id": None}


def test_a_busy_answer_names_the_running_reply(site, monkeypatch):
    import tutor.agent.turn as turn_mod

    _, client, _ = site

    async def busy(**kw):
        yield {"type": "busy", "message": turn_mod.BUSY_MESSAGE}

    monkeypatch.setattr(turn_mod, "run_turn", busy)
    streambuf.create("t-live", student_id=1, course="AST101")
    events = _events(client.post("/chat/message", data={"message": "again?", "course": "AST101"}).text)
    (busy_event,) = [e for e in events if e["type"] == "busy"]
    assert busy_event["turn_id"] == "t-live"


# ---------------------------------------------------------------------------
# R31: a restart closes the turn and answers the question with a note
# ---------------------------------------------------------------------------

def test_a_turn_cancelled_by_a_restart_is_closed_and_noted(site, monkeypatch):
    import tutor.agent.turn as turn_mod

    app, _, db = site
    started = asyncio.Event()

    async def long_turn(**kw):
        tdb, turn_id = kw["db"], kw["turn_id"]
        tdb.add(Message(student_id=1, role="user", content="Explain eclipses", course="AST101", turn_id=turn_id))
        state.record_turn_event(tdb, student_id=1, kind="chat", verdicts={"verdict": "in_progress"}, turn_id=turn_id)
        tdb.commit()
        yield {"type": "token", "text": "An eclipse is "}
        started.set()
        await asyncio.sleep(3600)                 # the process goes first
        yield {"type": "done", "full_text": "never"}

    monkeypatch.setattr(turn_mod, "run_turn", long_turn)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_token": login_cookie(db, 1)}) as client:
            post = asyncio.create_task(client.post("/chat/message", data={"message": "Explain eclipses", "course": "AST101"}))
            await asyncio.wait_for(started.wait(), 5)
            buf = streambuf.running_for(1)
            buf.task.cancel()                     # what the loop's shutdown does
            with pytest.raises(asyncio.CancelledError):
                await buf.task
            await asyncio.wait_for(post, 5)       # the stream ends with the buffer

    asyncio.run(scenario())
    db.expire_all()
    note = db.query(Message).filter_by(role="assistant").one()
    assert note.content == state.INTERRUPTED_NOTE and note.turn_id
    assert json.loads(db.query(TurnEvent).filter_by(kind="chat").one().verdicts_json) == {"verdict": "interrupted"}


def test_boot_closes_turns_a_kill_left_in_progress(site):
    _, _, db = site
    old, fresh = utcnow() - datetime.timedelta(minutes=30), utcnow() - datetime.timedelta(minutes=2)
    for turn_id, ts in (("t-old", old), ("t-fresh", fresh)):
        db.add(Message(student_id=1, role="user", content=f"q {turn_id}", course="AST101", turn_id=turn_id))
        db.add(TurnEvent(student_id=1, kind="chat", verdicts_json='{"verdict": "in_progress"}', turn_id=turn_id, ts=ts))
    db.commit()
    assert state.close_interrupted_turns(db) == 1          # the fresh one may still be running elsewhere
    verdicts = {e.turn_id: json.loads(e.verdicts_json)["verdict"] for e in db.query(TurnEvent).all()}
    assert verdicts == {"t-old": "interrupted", "t-fresh": "in_progress"}
    # The conversation went on past the cut question: a note now would read
    # as the newest reply, so only the ledger row closes.
    assert db.query(Message).filter_by(role="assistant").count() == 0
    assert state.close_interrupted_turns(db) == 0


def test_a_swept_note_sits_under_its_question(site):
    _, _, db = site
    asked = utcnow() - datetime.timedelta(days=2)
    db.add(Message(student_id=1, role="user", content="Explain eclipses", course="AST101", turn_id="t-cut", created_at=asked))
    db.add(TurnEvent(student_id=1, kind="chat", verdicts_json='{"verdict": "in_progress"}', turn_id="t-cut", ts=asked))
    db.add(TurnEvent(student_id=1, kind="chat", verdicts_json='{"verdict": "in_progress"}', turn_id=None, ts=asked))
    db.commit()
    assert state.close_interrupted_turns(db) == 1          # a row with no turn id never fills the batch
    note = db.query(Message).filter_by(role="assistant").one()
    assert note.content == state.INTERRUPTED_NOTE and note.created_at == asked


def test_the_page_knows_the_notes_opening_words():
    """chat.js gives a question back when the history ends on the note;
    rewording one without the other would silently turn that off."""
    import pathlib
    import re

    js = (pathlib.Path(__file__).resolve().parent.parent / "tutor/web/static/chat.js").read_text()
    prefix = re.search(r"const INTERRUPTED_PREFIX = '([^']+)'", js).group(1)
    assert state.INTERRUPTED_NOTE.startswith(prefix)


# ---------------------------------------------------------------------------
# R32: a second press of a sign-in that already worked goes on
# ---------------------------------------------------------------------------

def test_a_second_press_of_a_used_link_or_code_goes_on(site):
    _, client, db = site
    db.add(MagicLink(email="ada@example.edu", token="tok-used", code="123456", used=True, student_id=1,
                     expires_at=utcnow() + datetime.timedelta(minutes=10)))
    db.commit()
    r = client.post("/auth/magic/tok-used?next=%2Freview%3Fcourse%3DAST101", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review?course=AST101"
    r = client.get("/auth/magic/tok-used", follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/auth/magic-code", data={"email": "ada@example.edu", "code": "123456", "next": "/map"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/map"
    # Without the session, the same press is the failure it always was.
    client.cookies.clear()
    assert client.post("/auth/magic/tok-used", follow_redirects=False).status_code == 400


# ---------------------------------------------------------------------------
# In the browser
# ---------------------------------------------------------------------------

@pytest.fixture
def chat():
    s = student_db()
    s.add(Message(student_id=1, course="AST101", role="user", content="Explain eclipses"))
    s.commit()
    with app_client(s) as c:
        yield c
    s.close()


def _running(net, turn_id="t-live", text="An eclipse needs the Sun, Earth and Moon in a line."):
    net.stubs[("GET", "/chat/running")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"turn_id": turn_id}))
    net.stubs[("GET", f"/chat/message/{turn_id}")] = lambda route, url: route.fulfill(
        status=200, content_type="text/event-stream",
        body=sse([{"type": "token", "text": text}, {"type": "done", "full_text": text, "message_id": 77}]))


def test_a_reload_mid_reply_shows_the_reply_coming(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        _running(net)
        page.goto(f"{ORIGIN}/chat?course=AST101")
        expect(page.locator("#messages")).to_contain_text("An eclipse needs the Sun, Earth and Moon in a line.")
        assert page.locator("[data-turn='t-live']").count() == 1


def test_a_busy_answer_shows_the_running_reply(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        net.stubs[("GET", "/chat/running")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body='{"turn_id": null}')
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=Explain eclipses")
        _running(net)
        net.stubs[("GET", "/chat/running")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body='{"turn_id": null}')
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "turn", "id": "t-new"},
                      {"type": "busy", "message": "One question at a time.", "turn_id": "t-live"}]))
        page.fill("#msg-input", "And the seasons?")
        page.click("button:has-text('Send')")
        expect(page.locator("#messages")).to_contain_text("An eclipse needs the Sun, Earth and Moon in a line.")
        assert page.input_value("#msg-input") == "And the seasons?"


def test_a_reply_cut_by_a_restart_gives_the_question_back(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        net.stubs[("GET", "/chat/running")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body='{"turn_id": null}')
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=Explain eclipses")
        # The stream drops; the new process does not know the turn; the
        # saved conversation ends on the question.
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "turn", "id": "t-cut"}, {"type": "token", "text": "The seasons come "}]))
        net.stubs[("GET", "/chat/message/t-cut")] = lambda route, url: route.fulfill(
            status=404, content_type="application/json", body='{"error": "unknown turn"}')
        net.stubs[("GET", "/chat/history")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps([{"id": 1, "role": "user", "content": "Explain eclipses", "lecture_num": None},
                             {"id": 2, "role": "user", "content": "What causes the seasons?", "lecture_num": None}]))
        page.fill("#msg-input", "What causes the seasons?")
        page.click("button:has-text('Send')")
        expect(page.locator("[data-interrupted]")).to_contain_text("cut off")
        expect(page.locator("#msg-input")).to_have_value("What causes the seasons?")


def test_a_double_tap_on_sign_in_sends_once(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        posts = []
        net.stubs[("POST", "/auth/magic-link")] = lambda route, url: (posts.append(1), route.fulfill(
            status=200, content_type="text/html", body="<p>Check your email</p>"))
        page.goto(f"{ORIGIN}/auth/login")
        page.fill("input[name=email]", "ada@example.edu")
        page.evaluate("""() => { const f = document.querySelector('form[action="/auth/magic-link"]');
                                 f.requestSubmit(); f.requestSubmit(); }""")
        page.wait_for_selector("text=Check your email")
        assert posts == [1]


def test_a_blocked_first_play_on_an_iphone_becomes_tap_to_play(browser, chat, monkeypatch):

    from tutor.config import get_config

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    get_config.cache_clear()
    with open_page(browser, chat, clock=False) as (page, net):
        page.add_init_script("""(() => {
            let first = true;
            const real = HTMLMediaElement.prototype.play;
            HTMLMediaElement.prototype.play = function () {
              if (first) { first = false; return Promise.reject(new DOMException('gesture', 'NotAllowedError')); }
              Object.defineProperty(this, 'paused', {value: false, configurable: true});
              return Promise.resolve();
            };
        })()""")
        net.stubs[("POST", "/chat/tts")] = lambda route, url: route.fulfill(
            status=200, content_type="audio/mpeg", body=b"ID3")
        net.stubs[("GET", "/chat/running")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body='{"turn_id": null}')
        net.stubs[("GET", "/chat/history")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps([{"id": 5, "role": "assistant", "content": "A saved reply.", "lecture_num": None}]))
        page.goto(f"{ORIGIN}/chat?course=AST101")
        speaker = page.locator("[id^=hist-] .tts-btn").first
        speaker.click()
        expect(page.locator("[id^=hist-] .tts-note").first).to_have_text("Ready: tap to play.")
        speaker.click()
        expect(speaker).to_have_attribute("aria-label", "Pause")
