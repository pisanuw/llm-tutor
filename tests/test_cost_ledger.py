"""Plan step 2.1 (findings A3, B15, A17): the cost ledger is complete.

Before: cache writes (1.25x input on every cold-cache turn) were not priced;
the rubric evaluator, the exam grader, the exam labeller, and the clarifier
never reached the ledger; and a turn served by the capacity fallback was
priced as the actor model. MONTHLY_COST_CAP_USD was not a ceiling.
Done when: month_cost_usd moves on a cold-cache turn and on an exam.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import mastery, models, state
from tutor.agent import usage as usage_mod
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import LearningGoal, Student, TurnEvent
from tests.helpers import make_session


def test_cache_writes_are_priced():
    assert usage_mod.estimate_cost_usd(models.ACTOR.id, 0, 0, 0, 1_000_000) == pytest.approx(3.75)
    assert usage_mod.estimate_cost_usd(models.SUPERVISOR.id, 0, 0, 0, 1_000_000) == pytest.approx(1.25)
    assert models.cache_write_price("some-future-model") == models.ACTOR.cache_write_price
    fields = usage_mod.cost_fields(models.SUPERVISOR.id, {"in_tokens": 1000, "cache_write_tokens": 2000})
    assert fields["cost_usd"] == pytest.approx((1000 * 1.0 + 2000 * 1.25) / 1_000_000)


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Seasons", description="", course="AST101", lecture_num=3))
    s.commit()
    yield s
    s.close()


class _Usage:
    def __init__(self, inp=0, out=0, read=0, write=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = read
        self.cache_creation_input_tokens = write


def _routine_turn(db, usage, served_model=None, dispatch=None, script_tool=False):
    from tutor.agent import turn as agent
    from tutor.agent.strategist import NO_MOVE

    envelope = Envelope(max_rung=5, goals=["G1"], exam_lockdown=[], must_ground=True,
                        allow_exercise=True, escalate_review=False, reason="band")
    bundle = EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)
    calls = {"n": 0}

    class _ToolUse:
        type = "tool_use"
        name = "evaluate_code"
        id = "tu_1"
        input = {"student_code": "int f();", "problem": "p"}

    async def rounds(system, messages, tools):
        calls["n"] += 1
        tool_round = script_tool and calls["n"] == 1
        if not tool_round:
            yield ("chunk", "Find the Sun first.")
        final = MagicMock()
        final.stop_reason = "tool_use" if tool_round else "end_turn"
        final.content = [_ToolUse()] if tool_round else []
        final.usage = usage
        final.model = served_model
        yield ("final", final)

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message="why do we have seasons?",
            lecture_num=None, course="AST101",
        )]

    with patch("tutor.agent.preclassify.classify", return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE), \
         patch("tutor.agent.mastery.get_active_exam", return_value=None), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", rounds), \
         patch("tutor.agent.tools.dispatch_tool", return_value=dispatch or {}), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        return asyncio.run(go())


def test_a_cold_cache_turn_moves_the_month_cost(db):
    before = usage_mod.month_cost_usd(db)
    _routine_turn(db, _Usage(inp=50, out=200, write=30_000))
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    assert ev.cache_write_tokens == 30_000
    expected = (50 * 3.0 + 200 * 15.0 + 30_000 * 3.75) / 1_000_000
    assert ev.cost_usd == pytest.approx(expected)
    assert usage_mod.month_cost_usd(db) - before == pytest.approx(expected)
    # Without the writes the ledger said about 0.003; the call cost about 0.116.
    assert ev.cost_usd > 30 * (50 * 3.0 + 200 * 15.0) / 1_000_000


def test_the_fallback_model_is_recorded_and_priced(db):
    _routine_turn(db, _Usage(inp=1000, out=1000), served_model=models.SUPERVISOR.id)
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    assert ev.model == models.SUPERVISOR.id
    assert ev.cost_usd == pytest.approx((1000 * 1.0 + 1000 * 5.0) / 1_000_000)


def test_the_rubric_evaluator_gets_its_own_ledger_row(db):
    evaluated = {"score": 3, "correct": True, "feedback": "Close.", "hint": "",
                 "eval_details": {"improved_code": ""}, "eval_source": "llm-opinion",
                 "_usage": {"model": models.ACTOR.id, "in_tokens": 900, "out_tokens": 400,
                            "cache_read_tokens": 0, "cache_write_tokens": 0}}
    _routine_turn(db, _Usage(inp=10, out=10), dispatch=evaluated, script_tool=True)
    ev = db.query(TurnEvent).filter_by(kind="evaluate").one()
    assert ev.model == models.ACTOR.id
    assert ev.cost_usd == pytest.approx((900 * 3.0 + 400 * 15.0) / 1_000_000)


def test_the_rubric_reports_usage_but_never_to_the_student():
    from tutor.agent import exercises

    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text='{"score": 2, "feedback": "f", "hint": "h"}')]
    resp.usage = _Usage(inp=700, out=300)
    client.messages.create.return_value = resp
    with patch("tutor.agent.clients.supervisor_client", return_value=client):
        result = exercises._evaluate_with_rubric("int f();", "p")
    assert result["_usage"]["in_tokens"] == 700 and result["_usage"]["out_tokens"] == 300
    assert "_usage" not in exercises.student_facing_eval(result)


def test_an_exam_moves_the_month_cost(db):
    before = usage_mod.month_cost_usd(db)
    questions = {"q1": {"question": "a?", "rubric": "r"}, "q2": {"question": "b?", "rubric": "r"},
                 "code": {"question": "c?", "rubric": "r", "starter": "", "is_conceptual": True}}
    attempt = state.start_exam_attempt(db, 1, "G1", json.dumps(questions))
    db.commit()
    graded = {"score": 3, "passed": True, "feedback": "Solid.",
              "usage": {"in_tokens": 1200, "out_tokens": 150, "cache_read_tokens": 0,
                        "cache_write_tokens": 0}}

    async def grade():
        with patch.object(mastery, "_grade_with_api", return_value=graded):
            return [e async for e in mastery.stream_grade_exam(attempt, "Q1: a", 1, db)]
    asyncio.run(grade())
    ev = db.query(TurnEvent).filter_by(kind="exam_grade").one()
    assert ev.cost_usd == pytest.approx((1200 * 1.0 + 150 * 5.0) / 1_000_000)
    assert usage_mod.month_cost_usd(db) > before


def test_exam_labeller_and_clarifier_are_ledgered(db):
    from tutor.agent import turn as agent

    questions = {"q1": {"question": "a?", "rubric": "r"}, "q2": {"question": "b?", "rubric": "r"},
                 "code": {"question": "c?", "rubric": "r", "starter": "", "is_conceptual": True}}
    attempt = state.start_exam_attempt(db, 1, "G1", json.dumps(questions))
    db.commit()
    counts = {"in_tokens": 400, "out_tokens": 20, "cache_read_tokens": 0, "cache_write_tokens": 0}

    def label(message, client=None, usage_sink=None):
        usage_sink.append(dict(counts))
        return "CLARIFY"

    def clarify(qs, message, client=None, usage_sink=None):
        usage_sink.append(dict(counts))
        return "Q2 asks which phase comes after the full Moon."

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message="what does Q2 mean?",
            lecture_num=3, course="AST101",
        )]

    with patch("tutor.agent.mastery.get_active_exam", return_value=attempt), \
         patch("tutor.agent.mastery.classify_exam_message", side_effect=label), \
         patch("tutor.agent.mastery.clarify_exam", side_effect=clarify), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        asyncio.run(go())
    for kind in ("exam_classify", "exam_clarify"):
        ev = db.query(TurnEvent).filter_by(kind=kind).one()
        assert ev.cost_usd == pytest.approx((400 * 1.0 + 20 * 5.0) / 1_000_000)


def test_cache_writes_lower_the_cache_share():
    from pipeline.cost_report import aggregate

    warm = aggregate([{"kind": "chat", "cost": 0.01, "in_tokens": 100, "cache_tokens": 900}], 7)
    cold = aggregate([{"kind": "chat", "cost": 0.01, "in_tokens": 100, "cache_tokens": 900,
                       "cache_write_tokens": 9000}], 7)
    assert warm.cache_share == pytest.approx(0.9)
    assert cold.cache_share < 0.1
