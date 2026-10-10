"""What the adversarial review of bulletproofing Stage 4 found
(its section 5): a reattached reply shown
twice or not at all, the restart path depending on luck, and the edges of
the sign-in guard, the voice note and the code panel.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tutor import streambuf
from tutor.db.models import MagicLink, Message, PathEvent, Student, WhitelistEntry
from tutor.utctime import utcnow
from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tests.helpers import login_cookie, make_session


@pytest.fixture
def web():
    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session(Student(id=1, email="ada@example.edu", name="Ada"), WhitelistEntry(email="@example.edu", added_by="t"))
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session_token", login_cookie(db, 1))
    streambuf._reset_for_tests()
    try:
        yield client, db
    finally:
        app.dependency_overrides.clear()
        streambuf._reset_for_tests()
        db.close()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def test_the_running_reply_is_never_a_busy_or_finished_buffer(web):
    client, _ = web
    writing = streambuf.create("t-writing", student_id=1, course="AST101")
    busy = streambuf.create("t-busy", student_id=1, course="AST101")
    asyncio.run(busy.append({"type": "busy", "message": "One at a time."}))
    assert client.get("/chat/running?course=AST101").json() == {"turn_id": "t-writing"}
    asyncio.run(writing.append({"type": "done", "full_text": "saved", "message_id": 9}))
    assert client.get("/chat/running?course=AST101").json() == {"turn_id": None}     # saved: the history shows it
    # A course that is not one is AST101, not "any course".
    streambuf.create("t-AST102", student_id=1, course="AST102")
    assert client.get("/chat/running?course=999").json() == {"turn_id": None}


def test_a_cancelled_database_step_finishes_before_the_turn_closes():
    """Cancelling a turn while its step ran in a thread let the thread commit
    the reply after the turn wrote its "cut off" note."""
    from tutor.agent.turn_stages.dbstep import db_step

    finished = threading.Event()

    class FakeDB:
        def commit(self):
            pass

        def rollback(self):
            pass

    def slow_step():
        time.sleep(0.4)
        finished.set()

    async def scenario():
        task = asyncio.create_task(db_step(FakeDB(), slow_step))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return finished.is_set()

    assert asyncio.run(scenario()) is True


def test_an_empty_goal_on_a_path_event_is_no_goal(web):
    client, db = web
    r = client.post("/path/event", json={"course": "AST101", "goal_id": "", "modality": "teach", "source": "student"})
    assert r.status_code == 200
    assert db.query(PathEvent).one().goal_id is None


def test_a_signed_in_browser_goes_on_from_a_pruned_link(web):
    client, _ = web
    r = client.get("/auth/magic/long-gone-token", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/chat"
    client.cookies.clear()
    assert client.get("/auth/magic/long-gone-token", follow_redirects=False).status_code == 400


def test_a_spent_code_says_so(web):
    client, db = web
    db.add(MagicLink(email="ada@example.edu", token="tok", code="123456", used=True, student_id=1,
                     expires_at=utcnow().replace(year=utcnow().year + 1)))
    db.commit()
    client.cookies.clear()
    r = client.post("/auth/magic-code", data={"email": "ada@example.edu", "code": "123456"}, follow_redirects=False)
    assert r.status_code == 400 and "already used" in r.text and "did not match" not in r.text


def test_the_running_route_is_in_the_sweep():
    from tests.test_input_sweep import QUERIES

    assert ("/chat/running", {"course": "AST101"}) in QUERIES


# ---------------------------------------------------------------------------
# In the browser
# ---------------------------------------------------------------------------

@pytest.fixture
def chat():
    s = student_db(courses=("AST101", "AST102"))
    s.add(Message(student_id=1, course="AST101", role="user", content="Not now"))
    s.add(Message(id=3, student_id=1, course="AST101", role="assistant", content="No problem: the check waits a day."))
    s.commit()
    with app_client(s) as c:
        yield c
    s.close()


def _no_running(net):
    net.stubs[("GET", "/chat/running")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json", body='{"turn_id": null}')


def test_a_reattached_two_part_reply_shows_its_first_part_once(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        net.stubs[("GET", "/chat/running")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body='{"turn_id": "t-two"}')
        net.stubs[("GET", "/chat/message/t-two")] = lambda route, url: route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "token", "text": "No problem: the check waits a day."},
                      {"type": "section", "full_text": "No problem: the check waits a day.", "message_id": 3},
                      {"type": "token", "text": "Now, about eclipses."},
                      {"type": "done", "full_text": "Now, about eclipses.", "message_id": 4}]))
        page.goto(f"{ORIGIN}/chat?course=AST101")
        expect(page.locator("#messages")).to_contain_text("Now, about eclipses.")
        assert page.locator("#messages >> text=No problem: the check waits a day.").count() == 1


def test_a_reply_saved_between_history_and_running_still_shows(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        _no_running(net)
        calls = []

        def history(route, url):
            calls.append(1)
            rows = [{"id": 1, "role": "user", "content": "Explain eclipses", "lecture_num": None}]
            if len(calls) > 1:
                rows.append({"id": 2, "role": "assistant", "content": "An eclipse is a shadow.", "lecture_num": None})
            route.fulfill(status=200, content_type="application/json", body=json.dumps(rows))

        net.stubs[("GET", "/chat/history")] = history
        page.goto(f"{ORIGIN}/chat?course=AST101")
        expect(page.locator("#messages")).to_contain_text("An eclipse is a shadow.")


def test_a_course_switch_and_back_keeps_the_reply_coming(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        _no_running(net)
        held = []
        net.stubs[("POST", "/chat/message")] = lambda route, url: held.append(route)
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=Not now")
        page.fill("#msg-input", "What causes the seasons?")
        page.click("button:has-text('Send')")
        page.wait_for_function("document.querySelectorAll('[id^=bubble-]').length > 0")
        page.select_option("select[aria-label=Course]", "AST102")
        page.wait_for_timeout(300)
        page.select_option("select[aria-label=Course]", "AST101")
        page.wait_for_timeout(300)
        held[0].fulfill(status=200, content_type="text/event-stream",
                        body=sse([{"type": "turn", "id": "t1"}, {"type": "token", "text": "Last in, first out."},
                                  {"type": "done", "full_text": "Last in, first out.", "message_id": 12}]))
        expect(page.locator("#messages")).to_contain_text("Last in, first out.")


def test_a_cut_evaluate_gives_back_only_the_typed_words(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        _no_running(net)
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "turn", "id": "t-cut"}, {"type": "token", "text": "Looking at "}]))
        net.stubs[("GET", "/chat/message/t-cut")] = lambda route, url: route.fulfill(
            status=404, content_type="application/json", body='{"error": "unknown turn"}')
        code_msg = "Please evaluate my C++ code:\n\n```cpp\nint CODE_MARK = 1;\n```"
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=Not now")
        net.stubs[("GET", "/chat/history")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps([{"id": 1, "role": "user", "content": code_msg, "lecture_num": None}]))
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", "int CODE_MARK = 1;")
        page.click("button:has-text('Evaluate')")
        page.click("[data-predict] button:has-text('skip')")
        expect(page.locator("[data-interrupted]")).to_be_visible()
        assert page.input_value("#msg-input") == ""
        expect(page.locator("#code-input")).to_be_visible()


def test_the_tap_to_play_note_goes_with_the_tap(browser, chat, monkeypatch):
    from tutor.config import get_config

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    get_config.cache_clear()
    with open_page(browser, chat, clock=False) as (page, net):
        page.add_init_script("""(() => {
            let first = true;
            HTMLMediaElement.prototype.play = function () {
              if (first) { first = false; return Promise.reject(new DOMException('gesture', 'NotAllowedError')); }
              Object.defineProperty(this, 'paused', {value: false, configurable: true});
              return Promise.resolve();
            };
        })()""")
        _no_running(net)
        net.stubs[("POST", "/chat/tts")] = lambda route, url: route.fulfill(
            status=200, content_type="audio/mpeg", body=b"ID3")
        page.goto(f"{ORIGIN}/chat?course=AST101")
        speaker = page.locator("[id^=hist-] .tts-btn").first
        speaker.click()
        expect(page.locator(".tts-note").first).to_have_text("Ready: tap to play.")
        speaker.click()
        expect(page.locator(".tts-note")).to_have_count(0)


def test_a_stalled_sign_in_comes_back_after_the_guard(browser, chat):
    with open_page(browser, chat) as (page, net):
        # The submit dies on the way (Stop, Esc, a dropped network): the page stays.
        net.stubs[("POST", "/auth/magic-link")] = lambda route, url: route.abort("aborted")
        page.goto(f"{ORIGIN}/auth/login")
        page.fill("input[name=email]", "ada@example.edu")
        button = page.locator("form[action='/auth/magic-link'] button[type=submit]")
        page.evaluate("document.querySelector('form[action=\"/auth/magic-link\"]').requestSubmit()")
        expect(button).to_be_disabled()
        page.clock.fast_forward(6000)
        expect(button).to_be_enabled()


def test_evaluate_waits_on_a_used_up_quota(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        _no_running(net)
        net.stubs[("GET", "/chat/quota")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"windows": [], "note": "Used up.", "blocked": "Used up."}))
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", "int x;")
        expect(page.locator("button:has-text('Evaluate')")).to_be_disabled()


def test_a_refused_question_outlives_more_than_one_load(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        _no_running(net)
        notice = "That is today's tutor budget used up. It comes back at midnight PDT."
        hit = []

        def capped(route, url):
            hit.append(1)
            route.fulfill(status=200, content_type="text/event-stream",
                          body=sse([{"type": "turn", "id": "t1"}, {"type": "token", "text": notice},
                                    {"type": "done", "full_text": notice, "limited": True}]))

        net.stubs[("POST", "/chat/message")] = capped
        net.stubs[("GET", "/chat/quota")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"windows": [], "note": notice if hit else None, "blocked": notice if hit else None}))
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.fill("#msg-input", "What is amortized analysis?")
        page.click("button:has-text('Send')")
        expect(page.locator("button:has-text('Send')")).to_be_disabled()
        for _ in range(3):
            page.reload()
            expect(page.locator("#msg-input")).to_have_value("What is amortized analysis?")
