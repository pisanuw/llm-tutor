"""Plan step 2.2 (findings A6, C22, C21): one turn at a time per student,
one grade per exam attempt, and unique-key upserts that tolerate a race.

Done when: two concurrent sends produce one turn; two concurrent exam
answers grade once.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import mastery, state
from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import LearningGoal, MasteryAttempt, Student, StudentPath, Track, TurnEvent
from tests.helpers import applied, make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Seasons", description="", course="AST101", lecture_num=3))
    s.commit()
    agent._INFLIGHT.clear()
    yield s
    agent._INFLIGHT.clear()
    s.close()


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0


def _patches(stream_round):
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
    ]


def _gated_round(release: asyncio.Event):
    async def fake(system, messages, tools):
        yield ("chunk", "Working on it...")
        await release.wait()
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = _Usage()
        final.model = None
        yield ("final", final)
    return fake


async def _drain(db, message="what causes the seasons?"):
    return [e async for e in agent.run_turn(
        student=db.get(Student, 1), db=db, user_message=message, lecture_num=None, course="AST101",
    )]


def test_two_concurrent_sends_produce_one_turn(db):
    async def scenario():
        release = asyncio.Event()
        ps = _patches(_gated_round(release))
        with applied(ps):
            first = asyncio.create_task(_drain(db))
            await asyncio.sleep(0.05)                       # first is mid-stream
            second = await _drain(db, "and another thing")
            release.set()
            return await first, second

    first, second = asyncio.run(scenario())
    assert second == [{"type": "busy", "message": agent.BUSY_MESSAGE}]
    assert any(e["type"] == "done" for e in first)
    assert db.query(TurnEvent).filter_by(kind="chat").count() == 1
    assert agent._INFLIGHT == {}                            # released afterwards


def test_the_next_send_after_a_reply_runs(db):
    release = asyncio.Event()
    release.set()
    ps = _patches(_gated_round(release))
    with applied(ps):
        asyncio.run(_drain(db))
        asyncio.run(_drain(db, "follow-up"))
    assert db.query(TurnEvent).filter_by(kind="chat").count() == 2


def test_a_stale_claim_does_not_block_forever(db):
    agent._INFLIGHT[1] = (time.monotonic() - agent.INFLIGHT_STALE_SECONDS - 1, "abandoned")
    release = asyncio.Event()
    release.set()
    ps = _patches(_gated_round(release))
    with applied(ps):
        events = asyncio.run(_drain(db))
    assert any(e["type"] == "done" for e in events)


def test_closing_the_turn_early_releases_the_claim(db):
    release = asyncio.Event()
    release.set()
    ps = _patches(_gated_round(release))

    async def stop_at_done():
        gen = agent.run_turn(student=db.get(Student, 1), db=db, user_message="hi",
                             lecture_num=None, course="AST101")
        async for e in gen:
            if e["type"] == "done":
                break
        await gen.aclose()

    with applied(ps):
        asyncio.run(stop_at_done())
    assert agent._INFLIGHT == {}


# ---------------------------------------------------------------------------
# One grade per attempt
# ---------------------------------------------------------------------------

QUESTIONS = {"q1": {"question": "a?", "rubric": "r"}, "q2": {"question": "b?", "rubric": "r"},
             "code": {"question": "c?", "rubric": "r", "starter": "", "is_conceptual": True}}


def test_record_exam_result_refuses_a_second_close(db):
    a = state.start_exam_attempt(db, 1, "G1", json.dumps(QUESTIONS))
    state.record_exam_result(db, a, True)
    with pytest.raises(state.AttemptNotPending):
        state.record_exam_result(db, a, False)
    assert a.status == "passed" and a.attempts == 1


def test_two_concurrent_exam_answers_grade_once(db):
    attempt = state.start_exam_attempt(db, 1, "G1", json.dumps(QUESTIONS))
    db.commit()
    graded = {"score": 3, "passed": True, "feedback": "Solid.", "usage": {}}

    async def grade(answer):
        return [e async for e in mastery.stream_grade_exam(attempt, answer, 1, db)]

    async def both():
        with patch.object(mastery, "_grade_with_api", return_value=graded):
            return await asyncio.gather(grade("Q1: a"), grade("Q1: a"))

    first, second = asyncio.run(both())
    texts = ["".join(e.get("text", "") for e in ev if e["type"] == "token") for ev in (first, second)]
    assert sum("Mastery confirmed" in t for t in texts) == 1
    assert sum("already graded" in t for t in texts) == 1
    exam_events = [json.loads(e.verdicts_json) for e in db.query(TurnEvent).filter_by(kind="exam").all()]
    assert exam_events == [{"passed": True}]
    assert db.query(MasteryAttempt).one().attempts == 1


# ---------------------------------------------------------------------------
# Unique-key upserts that lose a race
# ---------------------------------------------------------------------------

def test_student_path_insert_that_loses_the_race_returns_the_winner(db):
    db.add(Track(id="AST101-lecture-order", course="AST101", title="Lecture order", goal_ids_json="[]"))
    db.commit()
    winner = state.set_student_path(db, 1, "AST101", "AST101-lecture-order")
    db.commit()
    row, created = state._insert_student_path(db, 1, "AST101", "AST101-lecture-order")
    assert created is False and row.id == winner.id
    assert db.query(StudentPath).count() == 1
    # The session is still usable after the rolled-back savepoint.
    row2, created2 = state.ensure_student_path(db, 1, "AST101", "AST101-lecture-order")
    assert created2 is False and row2.id == winner.id


def test_login_insert_that_loses_the_race_returns_the_winner(db):
    from tutor.routers.auth import _insert_student

    got = _insert_student(db, Student(email="stu@example.edu", name="Dup"))
    assert got.id == 1 and got.name == "Stu"
    assert db.query(Student).filter_by(email="stu@example.edu").count() == 1
