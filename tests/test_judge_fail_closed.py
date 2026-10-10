"""Plan step 1.2 (finding A7), end to end: when the judge fails on an exam
lockdown turn, the student gets the fallback reply, not the unjudged draft.

Before, every judge exception became an "allow" vote, so an outage (or a
timeout) on the turns that most need review let the draft through.
"""
from __future__ import annotations

import asyncio
import functools
import json
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import judge as judge_mod
from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import Message, Student, TurnEvent

pytestmark = pytest.mark.usefixtures("no_strategist")   # tests/conftest.py


class _Usage:
    input_tokens = 100
    output_tokens = 20
    cache_read_input_tokens = 0


def _one_round(text):
    async def fake(system, messages, tools):
        yield ("chunk", text)
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = _Usage()
        yield ("final", final)
    return fake


def _run(db, envelope, judge_client, draft):
    student = db.get(Student, 1)
    bundle = EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)
    real_judge = functools.partial(judge_mod.judge, client=judge_client)

    async def go():
        return [e async for e in agent.run_turn(
            student=student, db=db, user_message="walk me through it",
            lecture_num=None, course="AST101",
        )]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.mastery.get_active_exam", return_value=None), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", _one_round(draft)), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", real_judge):
        events = asyncio.run(go())
    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    return text, json.loads(ev.verdicts_json)


def _raising(n):
    c = MagicMock()
    c.messages.create.side_effect = [RuntimeError("judge timed out")] * n
    return c


def test_judge_failure_on_a_lockdown_turn_yields_the_fallback(db):
    lockdown = Envelope(
        max_rung=1, goals=["G3"], exam_lockdown=["G3"], must_ground=True,
        allow_exercise=False, escalate_review=True, reason="exam lockdown",
    )
    draft = "The distance for this exam question is 1 / 0.5 = 2 parsecs, so ..."
    text, verdicts = _run(db, lockdown, _raising(3), draft)

    assert text == judge_mod.FALLBACK_MESSAGE
    assert "parsecs" not in text
    assert verdicts["verdict"] == "block"
    assert verdicts["judged"] is True
    assert verdicts["judge_rule"] == "judge_unavailable"
    saved = db.query(Message).filter_by(role="assistant").one().content
    assert saved == judge_mod.FALLBACK_MESSAGE


def test_judge_failure_on_a_routine_judged_turn_keeps_the_draft(db):
    """Decision 4 default: outside lockdown and escalation, an outage fails open."""
    routine = Envelope(
        max_rung=3, goals=["G3"], exam_lockdown=[], must_ground=True,
        allow_exercise=True, escalate_review=False, reason="band",
    )
    draft = "Think about where the Sun is."
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.ATTEMPT, has_attempt=True)):
        text, verdicts = _run(db, routine, _raising(1), draft)
    assert text == draft
    assert verdicts["verdict"] == "allow"
