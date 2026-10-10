"""Session 5 tests: envelope before tools, tool loop under policy (S14).

Exit gate: table-driven envelope x available-tools tests, plus the reordered
pipeline (classify -> envelope -> gate -> bounded tool loop -> generate) and
the slide-metadata refinement that can widen scope but never fire a gate.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.judge import Verdict
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.agent.tools import TOOL_DEFINITIONS, tools_for_envelope
from tutor.db.models import (
    GoalPrerequisite,
    LearningGoal,
    MasteryAttempt,
    Student,
    TurnEvent,
)
from tutor.agent.turn_stages import actor_loop
from tests.helpers import make_session, ToolUse
from tests.helpers import scripted_rounds

pytestmark = pytest.mark.usefixtures("no_strategist")   # tests/conftest.py

ALL_TOOLS = {t["name"] for t in TOOL_DEFINITIONS}


def _envelope(max_rung=4, goals=(), lockdown=(), allow_exercise=True, escalate=False):
    return Envelope(
        max_rung=max_rung, goals=list(goals), exam_lockdown=list(lockdown),
        must_ground=True, allow_exercise=allow_exercise,
        escalate_review=escalate, reason="t",
    )


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))  # non-admin
    s.add(LearningGoal(id="G3", title="Seasons", description="", course="AST101", lecture_num=3))
    s.commit()
    yield s
    s.close()


def _collect(student, db, msg, lecture_num=None):
    async def run():
        return [e async for e in agent.run_turn(
            student=student, db=db, user_message=msg,
            lecture_num=lecture_num, course="AST101",
        )]
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Table-driven: envelope x available tools
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "allow_exercise,lockdown,expected",
    [
        # Normal turn: everything available.
        (True, (), ALL_TOOLS),
        # Instructor disabled exercises: get_exercise gone, retrieval stays.
        (False, (), ALL_TOOLS - {"get_exercise"}),
        # Exam lockdown: find_topic gone (no retrieval feeding locked goals);
        # compute_envelope also suppresses exercises during a lockdown.
        (False, ("G1",), ALL_TOOLS - {"find_topic", "get_exercise"}),
        # Lockdown with exercises somehow still on: only find_topic is cut.
        (True, ("G1",), ALL_TOOLS - {"find_topic"}),
    ],
)
def test_tools_for_envelope_table(allow_exercise, lockdown, expected):
    env = _envelope(allow_exercise=allow_exercise, lockdown=lockdown)
    names = {t["name"] for t in tools_for_envelope(env)}
    assert names == expected
    # evaluate_code and get_lecture_outline survive every envelope.
    assert {"evaluate_code", "get_lecture_outline"} <= names


# ---------------------------------------------------------------------------
# run_turn ordering: envelope assembled before tools, gate before retrieval
# ---------------------------------------------------------------------------


def test_run_turn_filters_tools_from_envelope(db):
    student = db.get(Student, 1)
    bundle = EnvelopeBundle(
        envelope=_envelope(allow_exercise=False, escalate=True),
        goal_titles={}, gate_goal=None,
    )
    captured: list = []

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.SOLUTION_DEMAND)), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.turn_stages.actor_client.stream_round",
               scripted_rounds([(["a hint"], "end_turn", [])], captured)), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
        _collect(student, db, "give me the answer")

    names = {t["name"] for t in captured[0]["tools"]}
    assert "get_exercise" not in names
    assert "find_topic" in names
    # The envelope is rendered into the actor call's system prompt (uncached
    # trailing block), proving envelope-before-actor ordering.
    assert "THIS TURN: respond at hint level <= H4" in captured[0]["system"][1]["text"]


def test_run_turn_gate_fires_before_any_tool_call(db):
    from tutor.agent import state

    student = db.get(Student, 1)
    goal = db.get(LearningGoal, "G3")
    questions = {
        "q1": {"question": "a?", "rubric": "r"},
        "q2": {"question": "b?", "rubric": "r"},
        "code": {"question": "c?", "rubric": "r", "starter": "", "is_conceptual": False},
    }
    attempt = state.start_exam_attempt(db, 1, "G3", json.dumps(questions))
    bundle = EnvelopeBundle(envelope=_envelope(), goal_titles={}, gate_goal=goal)
    actor = MagicMock()

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.mastery.get_active_exam", return_value=None), \
         patch("tutor.agent.mastery.start_exam", return_value=attempt), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", actor), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        events = _collect(student, db, "explain the seasons", lecture_num=3)

    actor.assert_not_called()   # gated turn never reaches the actor or tools
    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "Mastery Check" in text


# ---------------------------------------------------------------------------
# Slide-metadata refinement: scope may grow, the gate never re-fires
# ---------------------------------------------------------------------------

def test_slide_refinement_widens_scope_but_never_gates(db):
    student = db.get(Student, 1)
    # G_pre gates G3, and G_pre has exam questions: if the gate were computed
    # from slide metadata, this turn would start an exam.
    db.add(LearningGoal(id="G_pre", title="Daily motion", description="", course="AST101", lecture_num=1))
    db.flush()
    db.add(GoalPrerequisite(from_goal_id="G_pre", to_goal_id="G3"))
    db.commit()
    pool = {"G_pre": {"questions": [
        {"id": f"q{i}", "kind": "concept", "question": f"x{i}?", "rubric": "r"}
        for i in range(8)
    ]}}
    slide_ref = {"lecture_num": 3, "slide_url": "/slides/AST101/lecture-03/index.html",
                 "lecture_title": "Seasons", "slide_number": 5}
    captured: list = []
    script = [
        ([], "tool_use", [ToolUse("find_topic", "tu_1", query="seasons")]),
        (["a hint"], "end_turn", []),
    ]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.SOLUTION_DEMAND, goal_ids=[])), \
         patch("tutor.agent.mastery._load_exam_bank", return_value=pool), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script, captured)), \
         patch("tutor.agent.tools.dispatch_tool",
               return_value={"results": [], "slide_ref": slide_ref}), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
        # No lecture_num on the request and no classifier goals: scope starts
        # empty, the retrieved slide's lecture widens it mid-loop.
        events = _collect(student, db, "how does this work?")

    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    env = json.loads(ev.envelope_json)
    assert "G3" in env["goals"]                       # scope grew from the slide
    assert db.query(MasteryAttempt).count() == 0      # but no gate fired
    assert json.loads(ev.verdicts_json)["tool_rounds"] == 1
    # The continuation round saw the refined envelope in its system prompt.
    assert "Seasons" in captured[1]["system"][1]["text"]
    # The slide event was yielded mid-stream.
    assert any(e["type"] == "slide" for e in events)


def test_no_refinement_when_classifier_set_scope(db):
    student = db.get(Student, 1)
    db.add(LearningGoal(id="G9", title="Spectra", description="", course="AST101", lecture_num=4))
    db.commit()
    slide_ref = {"lecture_num": 3, "slide_url": "/slides/AST101/lecture-03/index.html",
                 "lecture_title": "Seasons", "slide_number": 5}
    script = [
        ([], "tool_use", [ToolUse("find_topic", "tu_1", query="spectra")]),
        (["a hint"], "end_turn", []),
    ]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.SOLUTION_DEMAND, goal_ids=["G9"])), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script)), \
         patch("tutor.agent.tools.dispatch_tool",
               return_value={"results": [], "slide_ref": slide_ref}), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
        _collect(student, db, "explain spectra")

    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    env = json.loads(ev.envelope_json)
    assert env["goals"] == ["G9"]   # classifier scope wins; slide ignored


def test_single_call_strips_eval_details_from_tool_results(db):
    """P1 on the single-call path: the continuation stream's tool_result for
    evaluate_code must carry only student-facing keys, never the solution."""
    student = db.get(Student, 1)
    eval_full = {
        "score": 1, "correct": False,
        "feedback": "The zero-distance check is missing.",
        "hint": "What should happen when the distance is zero?",
        "eval_details": {"improved_code": "if (d <= 0) return 0;"},
    }
    captured: list = []
    script = [
        ([], "tool_use", [ToolUse("evaluate_code", "tu_1",
                                   student_code="int main(){}", problem="p")]),
        (["feedback prose"], "end_turn", []),
    ]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION, has_attempt=True)), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script, captured)), \
         patch("tutor.agent.tools.dispatch_tool", return_value=eval_full), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
        events = _collect(student, db, "grade my code please")

    tool_result_msg = captured[1]["messages"][-1]
    payload = tool_result_msg["content"][0]["content"]
    assert "improved_code" not in payload and "eval_details" not in payload
    assert "The zero-distance check is missing." in payload
    assert any(e["type"] == "eval" for e in events)   # badge yielded mid-stream


# ---------------------------------------------------------------------------
# The actor's native tool loop: bounded, and chains tools with real results
# (these ran against the removed two-call tool phase until plan step 5.1)
# ---------------------------------------------------------------------------

def _escalated_bundle():
    return EnvelopeBundle(envelope=_envelope(escalate=True), goal_titles={}, gate_goal=None)


def test_tool_loop_is_bounded_at_max_rounds(db):
    """A model that asks for a tool on every round gets MAX_TOOL_ROUNDS tool
    rounds, then one last stream call, and the turn still ends."""
    student = db.get(Student, 1)
    captured: list = []
    script = [([], "tool_use", [ToolUse("find_topic", "tu_x", query="q")])]   # repeats

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=_escalated_bundle()), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script, captured)), \
         patch("tutor.agent.tools.dispatch_tool", return_value={"results": []}) as dispatched, \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
        events = _collect(student, db, "tell me about nebulae")

    assert dispatched.call_count == actor_loop.MAX_TOOL_ROUNDS
    assert len(captured) == actor_loop.MAX_TOOL_ROUNDS + 1
    assert events[-1]["type"] == "done"
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    assert json.loads(ev.verdicts_json)["tool_rounds"] == actor_loop.MAX_TOOL_ROUNDS


def test_tool_loop_chains_find_topic_into_get_exercise(db):
    """Each continuation round sees the previous round's result as a proper
    tool_result block, so find_topic can feed get_exercise."""
    student = db.get(Student, 1)
    captured: list = []
    script = [
        ([], "tool_use", [ToolUse("find_topic", "tu_1", query="seasons")]),
        ([], "tool_use", [ToolUse("get_exercise", "tu_2", topic="seasons")]),
        (["Try this one."], "end_turn", []),
    ]
    dispatched: list = []

    def fake_dispatch(name, tool_input):
        dispatched.append(name)
        return {"results": []} if name == "find_topic" else {"problem": "write daylight(h)"}

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=_escalated_bundle()), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script, captured)), \
         patch("tutor.agent.tools.dispatch_tool", side_effect=fake_dispatch), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
        events = _collect(student, db, "give me practice on the seasons")

    assert dispatched == ["find_topic", "get_exercise"]
    assert len(captured) == 3
    round2 = captured[1]["messages"]
    assert [m["role"] for m in round2[-2:]] == ["assistant", "user"]
    first = round2[-1]["content"][0]
    assert first["type"] == "tool_result" and first["tool_use_id"] == "tu_1"
    second = captured[2]["messages"][-1]["content"][0]
    assert second["tool_use_id"] == "tu_2" and "write daylight(h)" in second["content"]
    assert "Try this one." in "".join(e.get("text", "") for e in events if e["type"] == "token")
