"""Plan step 2.5 (findings A13, A12): two-stage persistence, one done.

A13: a failure after the reply streamed (bookkeeping, telemetry, a paused
database) rolled back the whole turn, so the student read an answer that was
never stored and its tokens were never ledgered; and a DB error inside the
advisory strategist left the session needing a rollback. A12: an exam pass
that replays the held-back question sent two "done" events in one turn, so
the client froze the second bubble with mismatched text.
Done when: a failure after streaming still leaves the reply, the tokens,
and the user row; the replay bubble is live and its buttons work.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tutor.agent import state
from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import Base, LearningGoal, Message, Student, TurnEvent
from tests.helpers import applied, make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Seasons", description="", course="AST101", lecture_num=3))
    s.commit()
    agent._INFLIGHT.clear()
    yield s
    s.close()


class _Usage:
    input_tokens = 400
    output_tokens = 120
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _round(text="Trace the Sun's path first."):
    async def fake(system, messages, tools):
        yield ("chunk", text)
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = _Usage()
        final.model = None
        yield ("final", final)
    return fake


def _patches(stream_round, extra=()):
    from tutor.agent.strategist import NO_MOVE

    envelope = Envelope(max_rung=5, goals=["G1"], exam_lockdown=[], must_ground=True,
                        allow_exercise=True, escalate_review=False, reason="band")
    bundle = EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)
    return [
        patch("tutor.agent.preclassify.classify", return_value=Classification(intent=Intent.QUESTION)),
        patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE),
        patch("tutor.agent.mastery.get_active_exam", return_value=None),
        patch("tutor.agent.envelope.assemble_envelope", return_value=bundle),
        patch("tutor.agent.turn_stages.actor_client.stream_round", stream_round),
        patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"),
        *extra,
    ]


def _run(db, patches, message="how do the seasons work?", lecture_num=3):
    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message=message,
            lecture_num=lecture_num, course="AST101",
        )]
    with applied(patches):
        return asyncio.run(go())


@pytest.mark.parametrize("target", [
    "tutor.agent.state.record_topic_access",           # was unguarded: lost the turn
    "tutor.agent.progress.mark_goals_seen_for_lecture",
])
def test_a_bookkeeping_failure_after_streaming_keeps_reply_tokens_and_message(db, target):
    boom = patch(target, side_effect=RuntimeError("db hiccup"))
    events = _run(db, _patches(_round(), [boom]))
    assert [e["type"] for e in events][-1] == "done"
    roles = [m.role for m in db.query(Message).order_by(Message.id).all()]
    assert roles == ["user", "assistant"]
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    assert ev.in_tokens == 400 and ev.out_tokens == 120 and ev.cost_usd > 0
    assert json.loads(ev.verdicts_json)["verdict"] == "allow"


def test_a_failure_while_saving_the_reply_still_leaves_the_message_and_the_turn(db):
    boom = patch("tutor.agent.state.finalize_turn_event", side_effect=RuntimeError("paused DB"))
    with pytest.raises(RuntimeError):
        _run(db, _patches(_round(), [boom]))
    db.rollback()
    assert [m.role for m in db.query(Message).all()] == ["user"]
    ev = db.query(TurnEvent).filter_by(kind="chat").one()     # the provisional event
    assert json.loads(ev.verdicts_json) == {"verdict": "in_progress"}


def test_a_db_error_in_the_strategist_does_not_poison_the_turn(db):
    def broken_trajectory(session, student_id):
        session.add(TurnEvent(student_id=None, kind="broken"))   # violates NOT NULL
        session.flush()

    boom = patch("tutor.agent.trajectory.compute_trajectory", side_effect=broken_trajectory)
    events = _run(db, _patches(_round(), [boom]))
    assert [e["type"] for e in events][-1] == "done"
    assert db.query(TurnEvent).filter_by(kind="chat").count() == 1
    assert db.query(TurnEvent).filter_by(kind="broken").count() == 0


# ---------------------------------------------------------------------------
# One done per turn
# ---------------------------------------------------------------------------

QUESTIONS = {"q1": {"question": "a?", "rubric": "r"}, "q2": {"question": "b?", "rubric": "r"},
             "code": {"question": "c?", "rubric": "r", "starter": "", "is_conceptual": True}}


def test_an_exam_pass_with_a_replay_sends_one_done_and_a_section(db):
    from tutor.agent import mastery

    trigger = Message(student_id=1, role="user", content="Why is June warm in the north?",
                      course="AST101", lecture_num=3)
    db.add(trigger)
    db.flush()
    attempt = state.start_exam_attempt(db, 1, "G1", json.dumps(QUESTIONS), trigger_message_id=trigger.id)
    db.commit()
    graded = {"score": 3, "passed": True, "feedback": "Solid.", "usage": {}}
    extra = [
        patch("tutor.agent.mastery.get_active_exam", return_value=attempt),
        patch("tutor.agent.mastery.classify_exam_message", return_value="ANSWER"),
        patch.object(mastery, "_grade_with_api", return_value=graded),
    ]
    events = _run(db, _patches(_round("Start from the tilt of the axis."), extra), message="Q1: a Q2: b Q3: c")
    kinds = [e["type"] for e in events]
    assert kinds.count("done") == 1 and kinds[-1] == "done"
    assert kinds.count("section") == 1
    section = next(e for e in events if e["type"] == "section")
    done = events[-1]
    saved = [m.content for m in db.query(Message).filter_by(role="assistant").order_by(Message.id).all()]
    # Each bubble's text is exactly one saved reply, so speak and feedback match.
    assert saved == [section["full_text"], done["full_text"]]
    assert "Mastery confirmed" in section["full_text"]
    assert done["full_text"] == "Start from the tilt of the axis."


def test_a_deferral_sends_a_section_then_the_answer(db):
    from tutor.agent import mastery

    attempt = state.start_exam_attempt(db, 1, "G1", json.dumps(QUESTIONS))
    db.commit()
    extra = [
        patch("tutor.agent.mastery.get_active_exam", return_value=attempt),
        patch("tutor.agent.mastery.classify_exam_message", return_value="DEFER"),
    ]
    events = _run(db, _patches(_round("Parallax is an apparent shift."), extra), message="later please")
    kinds = [e["type"] for e in events]
    assert kinds.count("done") == 1 and kinds.count("section") == 1
    section = next(e for e in events if e["type"] == "section")
    assert section["full_text"] == mastery.DEFER_ACK


def test_the_api_returns_both_replies():
    from tutor.routers import api

    src = open(api.__file__).read()
    assert 'kind in ("section", "done")' in src


# ---------------------------------------------------------------------------
# A crash after the student's message was committed
# ---------------------------------------------------------------------------

@pytest.fixture
def crash_client(monkeypatch):
    from fastapi.testclient import TestClient

    from tutor.auth.deps import get_current_student, get_db
    from tutor.main import app
    from tutor.routers import chat as chat_router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    s = Student(email="crash@example.edu", name="Crash")
    session.add(s)
    session.commit()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s
    monkeypatch.setattr(chat_router, "_turn_session", lambda: Session())
    try:
        yield TestClient(app), session, monkeypatch
    finally:
        app.dependency_overrides.clear()
        session.close()


def _crashing_turn(save_first: bool):
    async def fake_run_turn(*, student, db, user_message, course, **_):
        if save_first:
            db.add(Message(student_id=student.id, role="user", content=user_message, course=course))
            db.commit()
        yield {"type": "token", "text": "Partial "}
        raise RuntimeError("paused DB")
    return fake_run_turn


@pytest.mark.parametrize("save_first,expected", [
    (True, ["user", "assistant"]),      # the committed message gets its note
    (False, []),                        # nothing saved: nothing to pair
])
def test_a_crash_pairs_a_saved_message_with_a_note(crash_client, save_first, expected):
    import tutor.agent.turn as turn_mod

    client, session, monkeypatch = crash_client
    monkeypatch.setattr(turn_mod, "run_turn", _crashing_turn(save_first))
    resp = client.post("/chat/message", data={"message": "what is a nebula?", "course": "AST101"})
    assert resp.status_code == 200 and '"type": "error"' in resp.text
    session.expire_all()
    rows = session.query(Message).order_by(Message.id).all()
    assert [m.role for m in rows] == expected
    if save_first:
        assert rows[-1].content == agent.CRASH_NOTE == "[No reply: the tutor ran into a problem.]"
    assert session.query(TurnEvent).filter_by(kind="error").count() == 1


def test_a_crash_after_stage_one_finalizes_the_provisional_row(crash_client):
    """The provisional chat row used to stay in_progress forever beside a
    kind=error row with no turn id (finding
    F49): the crash is now written on that row, on the turn's id."""
    import tutor.agent.turn as turn_mod
    from tutor.agent import state

    client, session, monkeypatch = crash_client

    async def fake_run_turn(*, student, db, user_message, course, turn_id=None, **_):
        db.add(Message(student_id=student.id, role="user", content=user_message, course=course))
        db.commit()
        state.record_turn_event(db, student_id=student.id, kind="chat", intent="QUESTION", rung_used=3,
                                goal_ids=["AST101-L04-tides"], envelope={"max_rung": 3}, model="actor-x",
                                verdicts={"verdict": "in_progress"}, turn_id=turn_id)
        db.commit()
        yield {"type": "token", "text": "Partial "}
        raise RuntimeError("paused DB")

    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)
    resp = client.post("/chat/message", data={"message": "what is a nebula?", "course": "AST101"})
    assert resp.status_code == 200 and '"type": "error"' in resp.text
    session.expire_all()
    rows = session.query(TurnEvent).order_by(TurnEvent.id).all()
    assert [r.kind for r in rows] == ["chat"]                      # no second, error row
    verdicts = json.loads(rows[0].verdicts_json)
    assert verdicts == {"verdict": "crash", "error": "paused DB"}
    assert rows[0].turn_id and len(rows[0].turn_id) == 32
    # What stage 1 wrote survives the crash: the Supervision tab still shows
    # what was asked and which model died.
    assert (rows[0].intent, rows[0].rung_used, rows[0].model) == ("QUESTION", 3, "actor-x")
    assert json.loads(rows[0].goal_ids_json) == ["AST101-L04-tides"] and json.loads(rows[0].envelope_json) == {"max_rung": 3}
    assert session.query(Message).order_by(Message.id).all()[-1].content == agent.CRASH_NOTE
