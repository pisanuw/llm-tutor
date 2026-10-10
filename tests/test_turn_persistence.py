"""A turn is durable before the client hears "done" (plan step 0.6, finding A1).

The JSON API used to return at the first ``done`` event. Every path in
run_turn persisted after that yield, so the request session rolled the turn
back: no reply row, no TurnEvent, no cost, and a daily cap that never counted
API turns. Each test here consumes a turn only up to ``done``, closes the
generator, and rolls the session back, exactly as that consumer did, then
checks what survived.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tutor.agent import turn as agent
from tutor.agent.judge import Verdict
from tutor.agent.policy import Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import LearningGoal, Message, Student, TurnEvent
from tests.helpers import make_session

pytestmark = pytest.mark.usefixtures("no_strategist")   # tests/conftest.py


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="The celestial sphere", description="", course="AST101", lecture_num=1))
    s.commit()
    yield s
    s.close()


def _plain_answer(text):
    async def fake(system, messages, tools):
        yield ("chunk", text)
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                                      cache_read_input_tokens=0)
        yield ("final", final)
    return fake


def _stop_at_done(db, message):
    """Consume one turn like the old API: stop at the first done, close the
    generator, and roll back what was not committed."""
    async def run():
        gen = agent.run_turn(student=db.get(Student, 1), db=db,
                             user_message=message, course="AST101")
        events = []
        async for event in gen:
            events.append(event)
            if event["type"] == "done":
                break
        await gen.aclose()
        return events
    events = asyncio.run(run())
    db.rollback()
    return events


def _replies(db):
    return [m.content for m in db.query(Message).filter_by(role="assistant").order_by(Message.id)]


def test_normal_turn_is_committed_before_done(db):
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", _plain_answer("Parallax is an apparent shift.")):
        events = _stop_at_done(db, "what is parallax?")

    reply = db.query(Message).filter_by(role="assistant").one()
    assert events[-1] == {"type": "done", "full_text": "Parallax is an apparent shift.",
                          "message_id": reply.id}
    users = [m.content for m in db.query(Message).filter_by(role="user")]
    assert users == ["what is parallax?"]
    assert _replies(db) == ["Parallax is an apparent shift."]
    chat_events = db.query(TurnEvent).filter_by(student_id=1, kind="chat").all()
    assert len(chat_events) == 1 and chat_events[0].in_tokens == 100


def test_the_callers_turn_id_names_the_ledger_row(db):
    """The chat route mints the stream's resume handle before the turn
    starts; the same id goes on the turn_events row and in the activity
    stream, so a browser event that carries it joins both
    (activity analytics, step 1.2)."""
    async def run():
        gen = agent.run_turn(student=db.get(Student, 1), db=db, user_message="what is parallax?",
                             course="AST101", turn_id="feedfacecafebeef")
        return [event async for event in gen]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", _plain_answer("An address.")), \
         patch("tutor.logging_config.activity") as activity:
        events = asyncio.run(run())

    chat_event = db.query(TurnEvent).filter_by(student_id=1, kind="chat").one()
    assert chat_event.turn_id == "feedfacecafebeef"
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["message_id"] == db.query(Message).filter_by(role="assistant").one().id
    logged = {call.kwargs.get("turn_id") for call in activity.call_args_list
              if call.args and call.args[0] == "chat_turn"}
    assert logged == {"feedfacecafebeef"}


def test_a_caller_without_a_turn_id_gets_one(db):
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.HOSTILE)), \
         patch("tutor.agent.conduct.conduct_response", return_value="Let's keep it civil."):
        events = _stop_at_done(db, "you are useless")
    assert events[-1]["message_id"] == db.query(Message).filter_by(role="assistant").one().id


def test_conduct_reply_is_committed_before_done(db):
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.HOSTILE)), \
         patch("tutor.agent.conduct.conduct_response", return_value="Let's keep it civil."):
        events = _stop_at_done(db, "you are useless")

    assert events[-1]["type"] == "done"
    assert _replies(db) == ["Let's keep it civil."]


def test_exam_clarification_is_committed_before_done(db):
    exam = SimpleNamespace(questions_json=json.dumps(["Q1: what is *p?"]))
    with patch("tutor.agent.mastery.get_active_exam", return_value=exam), \
         patch("tutor.agent.mastery.classify_exam_message", return_value="CLARIFY"), \
         patch("tutor.agent.mastery.clarify_exam", return_value="Q1 asks what *p evaluates to."):
        events = _stop_at_done(db, "what does Q1 mean?")

    assert events[-1]["type"] == "done"
    assert _replies(db) == ["Q1 asks what *p evaluates to."]


# ---------------------------------------------------------------------------
# POST /api/chat: operators only, and it reads the whole turn
# ---------------------------------------------------------------------------

@pytest.fixture
def api(db, monkeypatch):
    import tutor.config as config
    from tutor.auth.deps import get_current_student, get_db
    from tutor.main import app

    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    db.add(Student(id=2, email="prof@example.edu", name="Prof"))
    db.commit()
    who = {"id": 1}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_student] = lambda: db.get(Student, who["id"])
    try:
        yield TestClient(app), who
    finally:
        app.dependency_overrides.clear()


def test_api_chat_is_for_operators_only(api):
    client, who = api
    who["id"] = 1
    resp = client.post("/api/chat", json={"message": "hi", "course": "AST101"})
    assert resp.status_code == 403


def test_api_chat_reads_the_whole_turn(api):
    client, who = api
    who["id"] = 2
    progress = {"finished": False}

    async def fake_run_turn(**kwargs):
        yield {"type": "token", "text": "Passed."}
        yield {"type": "done", "full_text": "Passed."}
        yield {"type": "slide", "url": "/slides/AST101/lecture-04/index.html",
               "title": "Light and Telescopes", "num": 3, "lecture_num": 4}
        yield {"type": "done", "full_text": "Here is the answer."}
        progress["finished"] = True   # only reached if the API keeps reading

    with patch("tutor.agent.turn.run_turn", fake_run_turn):
        resp = client.post("/api/chat", json={"message": "q", "course": "AST101"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Passed.\n\nHere is the answer."
    assert body["slide_ref"]["lecture_num"] == 4
    assert body["error"] is None
    assert progress["finished"] is True


def test_api_chat_reports_a_failed_turn(api):
    client, who = api
    who["id"] = 2

    async def failing_run_turn(**kwargs):
        yield {"type": "error", "message": "The tutor is briefly overloaded."}
        yield {"type": "done", "full_text": ""}

    with patch("tutor.agent.turn.run_turn", failing_run_turn):
        body = client.post("/api/chat", json={"message": "q", "course": "AST101"}).json()

    assert body["text"] == "The tutor is briefly overloaded."
    assert body["error"] == "The tutor is briefly overloaded."
