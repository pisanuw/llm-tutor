"""A large paste is taken or refused in words, and typed text is never lost
(bulletproofing Stage 3).

R22: a paste of any size was sent whole, billed on every round and resent
with every later turn, and over 1 MB the page said "try again in a minute".
R23: the graders read the first 1,500 characters, so the code asked for last
in a prerequisite check was cut. R24: Evaluate and a starter replaced a
question the student had typed.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tutor.db.models import LearningGoal, Message, Student, WhitelistEntry
from tutor.routers.chat import MAX_MESSAGE_CHARS
from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tests.helpers import login_cookie, make_session


# ---------------------------------------------------------------------------
# R22: the limit on the server, and older pastes clipped in history
# ---------------------------------------------------------------------------

def test_the_server_refuses_a_message_over_the_limit_in_words(monkeypatch):
    import tutor.agent.turn as turn_mod
    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session(Student(id=1, email="ada@example.edu", name="Ada"), WhitelistEntry(email="@example.edu", added_by="t"))

    async def fake_run_turn(**kw):
        yield {"type": "done", "full_text": "ok"}

    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.cookies.set("session_token", login_cookie(db, 1))
        resp = client.post("/chat/message", data={"message": "x" * (MAX_MESSAGE_CHARS + 1), "course": "AST101"})
        assert resp.status_code == 413
        assert resp.json()["error"].startswith("That is 30,001 characters, and the tutor takes up to 30,000")
        assert client.post("/chat/message", data={"message": "x" * MAX_MESSAGE_CHARS, "course": "AST101"}).status_code == 200
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_history_resends_an_older_paste_clipped_and_the_latest_whole():
    from tutor.agent import memory

    db = make_session(Student(id=1, email="ada@example.edu", name="Ada"))
    old = "OLD-HEAD " + "a" * 20_000 + " OLD-END"
    latest = "NEW-HEAD " + "b" * 20_000 + " NEW-END"
    db.add_all([Message(student_id=1, role="user", content=old, course="AST101"),
                Message(student_id=1, role="assistant", content="Looked at it.", course="AST101"),
                Message(student_id=1, role="user", content=latest, course="AST101"),
                Message(student_id=1, role="assistant", content="And this one.", course="AST101")])
    db.commit()
    history = memory.load_history(db, 1, "AST101")
    first, newest = history[0]["content"], history[2]["content"]
    assert len(first) < 7_000 and first.startswith("OLD-HEAD") and first.endswith("OLD-END")
    assert "characters omitted" in first
    assert newest == latest


# ---------------------------------------------------------------------------
# R23: the graders read the end of a long answer
# ---------------------------------------------------------------------------

def _capture(text='{"score": 2, "feedback": "f"}'):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text=text)]
    resp.usage = None
    client.messages.create.return_value = resp
    return client


def _sent(client) -> str:
    return client.messages.create.call_args.kwargs["messages"][0]["content"]


def test_the_graders_read_the_code_at_the_end_of_a_long_answer():
    from tutor.agent import mastery

    goal = LearningGoal(id="G1", title="t", description="d", course="AST101", lecture_num=1)
    answer = "Q1: " + "because " * 400 + "\nQ2: " + "so " * 300 + "\nCODE: int main() { return CODE_AT_END; }"
    questions = {"q1": {"question": "a?", "rubric": "r"}, "q2": {"question": "b?", "rubric": "r"},
                 "code": {"question": "c?", "rubric": "r", "starter": "", "is_conceptual": False}}
    client = _capture('{"passed": true, "score": 3, "feedback": "f"}')
    mastery._grade_with_api(goal, questions, answer, client=client)
    assert "CODE_AT_END" in _sent(client)
    client = _capture()
    mastery.grade_probe(goal, "q", "r", "x" * 9_000 + " PROBE_END", client=client)
    sent = _sent(client)
    assert "PROBE_END" in sent and "characters omitted" in sent


# ---------------------------------------------------------------------------
# In the browser: R22 and R24
# ---------------------------------------------------------------------------

@pytest.fixture
def chat():
    s = student_db()
    s.add(Message(student_id=1, course="AST101", role="assistant", content="Welcome back."))
    s.commit()
    with app_client(s) as c:
        yield c
    s.close()


def _turns(net):
    posted = []

    def turn(route, url):
        posted.append(route.request.post_data_buffer)
        route.fulfill(status=200, content_type="text/event-stream",
                      body=sse([{"type": "turn", "id": "t1"}, {"type": "token", "text": "Sure."},
                                {"type": "done", "full_text": "Sure.", "message_id": 9}]))

    net.stubs[("POST", "/chat/message")] = turn
    return posted


def _open(page):
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.wait_for_selector("#messages >> text=Welcome back.")


def test_a_paste_over_the_limit_stays_in_the_box_with_its_size(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        posted = _turns(net)
        _open(page)
        page.fill("#msg-input", "y" * 31_000)
        page.click("button:has-text('Send')")
        expect(page.locator("[data-input-note]")).to_contain_text("That is 31,000 characters")
        assert page.input_value("#msg-input") == "y" * 31_000 and posted == []
        page.fill("#msg-input", "short now")                       # editing clears the note
        expect(page.locator("[data-input-note]")).to_be_hidden()


def test_evaluate_keeps_the_typed_question_in_front_of_the_code(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        posted = _turns(net)
        _open(page)
        page.fill("#msg-input", "Why does this loop forever?")
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", "while (true) {}")
        page.click("button:has-text('Evaluate')")
        page.click("[data-predict] button:has-text('skip')")
        expect(page.locator("#messages")).to_contain_text("Sure.")
        body = posted[0].decode("utf-8", "replace")
        assert body.index("Why does this loop forever?") < body.index("Please evaluate my C++ code")
        assert page.input_value("#msg-input") == ""


def test_a_starter_does_not_replace_the_typed_question(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        net.stubs[("GET", "/chat/starters")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body='{"course": "AST101", "starters": [{"label": "Quiz me", "message": "Quiz me on moon phases"}]}')
        posted = _turns(net)
        _open(page)
        page.fill("#msg-input", "My half-typed question")
        page.click("button[aria-label='Ideas for what to ask']")
        page.click("button[data-track=starter]:has-text('Quiz me')")
        expect(page.locator("#messages")).to_contain_text("Sure.")
        assert b"Quiz me on moon phases" in posted[0]
        assert page.input_value("#msg-input") == "My half-typed question"
