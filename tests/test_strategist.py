"""Session 9 tests: the strategist proposes, the policy core disposes.

Exit gate: strategist tests with a mocked client; policy tests proving a
move can never exceed the ceiling or override lockdown; telemetry records
the move chosen.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import strategist
from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.judge import Verdict
from tutor.agent.policy import Envelope, Intent, Move, merge_move
from tutor.agent.preclassify import Classification
from tutor.agent.prompt_builder import build_system_prompt, render_envelope
from tutor.agent.trajectory import GoalTrajectory, Trajectory
from tutor.db.models import Student, TurnEvent
from tests.helpers import make_session


def _envelope(max_rung=4, lockdown=(), goals=("G1",), escalate=False):
    return Envelope(
        max_rung=max_rung, goals=list(goals), exam_lockdown=list(lockdown),
        must_ground=True, allow_exercise=True, escalate_review=escalate, reason="t",
    )


def _trajectory(stuck=False):
    return Trajectory(
        session_turns=4, attempts=1, questions=3, passive=0,
        minutes_since_last_attempt=12.0,
        goals=(GoalTrajectory(
            goal_id="G1", turns=4, rungs_used=(4, 4, 5, 5), attempts=1,
            questions=3, passive=0, repeated_error=stuck,
            exam_passes=0, exam_fails=1, graded_scores=(0.25,),
        ),),
    )


def _client(text):
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock(content=[block])
    resp.usage = MagicMock(input_tokens=400, output_tokens=30, cache_read_input_tokens=0)
    client.messages.create.return_value = resp
    return client


# ---------------------------------------------------------------------------
# The strategist call (mocked client)
# ---------------------------------------------------------------------------

def test_valid_move_is_returned_with_reason():
    client = _client('{"move": "WORKED_ANALOGY", "reason": "Stuck on the same goal."}')
    choice = strategist.choose_move(_envelope(), _trajectory(stuck=True), Intent.QUESTION, client=client)
    assert choice.move == Move.WORKED_ANALOGY
    assert "Stuck" in choice.reason
    assert choice.in_tokens == 400   # usage surfaced for the ledger


def test_strategist_sees_facts_never_prose():
    client = _client('{"move": null, "reason": ""}')
    strategist.choose_move(_envelope(), _trajectory(stuck=True), Intent.QUESTION, client=client)
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "stuck=True" in sent
    assert "help ceiling: H4" in sent
    assert "intent: QUESTION" in sent
    # The signature cannot carry the student message; the rendered facts are
    # built purely from the dataclasses.
    assert "<student" not in sent


@pytest.mark.parametrize("raw", [
    '{"move": "INVENT_NEW_MOVE", "reason": "x"}',   # unknown move
    "garbage not json",
    '{"reason": "no move key"}',
    '{"move": null, "reason": "nothing stands out"}',
])
def test_invalid_or_null_output_means_no_move(raw):
    choice = strategist.choose_move(_envelope(), _trajectory(), Intent.QUESTION, client=_client(raw))
    assert choice.move is None


def test_api_failure_means_no_move():
    broken = MagicMock()
    broken.messages.create.side_effect = RuntimeError("down")
    assert strategist.choose_move(_envelope(), _trajectory(), Intent.QUESTION, client=broken).move is None


def test_lockdown_skips_the_call_entirely():
    client = MagicMock()
    choice = strategist.choose_move(_envelope(lockdown=("G1",)), _trajectory(), Intent.QUESTION, client=client)
    assert choice.move is None
    client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Policy supremacy: merge_move
# ---------------------------------------------------------------------------

def test_move_never_raises_the_ceiling():
    env = _envelope(max_rung=3)
    merged = merge_move(env, Move.ENCOURAGE_ATTEMPT, "try it")
    assert merged.max_rung == 3
    assert merged.move == Move.ENCOURAGE_ATTEMPT


def test_content_bearing_move_above_ceiling_is_dropped():
    env = _envelope(max_rung=4)
    assert merge_move(env, Move.WORKED_ANALOGY, "x").move is None   # needs H5
    assert merge_move(_envelope(max_rung=5), Move.WORKED_ANALOGY, "x").move == Move.WORKED_ANALOGY
    assert merge_move(_envelope(max_rung=1), Move.LOCATED_OBSERVATION, "x").move is None  # needs H2


def test_move_never_overrides_exam_lockdown():
    env = _envelope(lockdown=("G1",))
    merged = merge_move(env, Move.ENCOURAGE_ATTEMPT, "x")
    assert merged.move is None
    assert merged == env


def test_no_move_is_a_no_op():
    env = _envelope()
    assert merge_move(env, None) == env


# ---------------------------------------------------------------------------
# Rendering: per-turn line + cached vocabulary
# ---------------------------------------------------------------------------

def test_render_envelope_names_the_move():
    env = merge_move(_envelope(), Move.ELICIT_PREDICTION, "new goal, no history")
    block = render_envelope(env)
    assert "THIS TURN'S MOVE" in block
    assert "predict" in block
    assert "new goal, no history" in block
    # No move: no line.
    assert "THIS TURN'S MOVE" not in render_envelope(_envelope())


def test_move_vocabulary_lives_in_the_cached_prefix():
    prompt = build_system_prompt()
    assert "Instructional moves" in prompt
    for m in Move:
        assert m.value in prompt


# ---------------------------------------------------------------------------
# run_turn wiring: the chosen move reaches the actor and the ledger
# ---------------------------------------------------------------------------

class _Usage:
    input_tokens = 100
    output_tokens = 20
    cache_read_input_tokens = 30000


def test_run_turn_records_move_and_renders_it(tmp_path):
    db = make_session()
    try:
        db.add(Student(id=1, email="stu@example.edu", name="Stu"))
        db.commit()
        student = db.get(Student, 1)

        captured: list = []

        async def fake_round(system, messages, tools):
            captured.append(system)
            final = MagicMock()
            final.stop_reason = "end_turn"
            final.content = []
            final.usage = _Usage()
            yield ("chunk", "Before I explain: what do you think happens?")
            yield ("final", final)

        bundle = EnvelopeBundle(envelope=_envelope(escalate=True), goal_titles={}, gate_goal=None)
        choice = strategist.MoveChoice(
            move=Move.ELICIT_PREDICTION, reason="passive streak",
            in_tokens=400, out_tokens=25, latency_ms=900,
        )

        def run():
            async def go():
                return [e async for e in agent.run_turn(
                    student=student, db=db, user_message="explain eclipses", course="AST101",
                )]
            return asyncio.run(go())

        with patch("tutor.agent.preclassify.classify",
                   return_value=Classification(intent=Intent.QUESTION)), \
             patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
             patch("tutor.agent.strategist.choose_move", return_value=choice), \
             patch("tutor.agent.turn_stages.actor_client.stream_round", fake_round), \
             patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
             patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
            run()

        # The actor's system prompt carried the move line.
        assert len(captured[0]) > 1, f"Expected >=2 system blocks, got {len(captured[0])}"
        assert "THIS TURN'S MOVE" in captured[0][1]["text"]
        # Telemetry: the chat event's envelope records the move; the strategist
        # call itself is ledgered with cost.
        chat = db.query(TurnEvent).filter_by(kind="chat").one()
        env = json.loads(chat.envelope_json)
        assert env["move"] == "ELICIT_PREDICTION"
        strat = db.query(TurnEvent).filter_by(kind="strategist").one()
        assert json.loads(strat.verdicts_json)["move"] == "ELICIT_PREDICTION"
        assert strat.cost_usd > 0
    finally:
        db.close()
