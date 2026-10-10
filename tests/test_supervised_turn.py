"""The supervised turn (run_turn) end to end: hostile messages, the detector,
and the judge's verdicts. Named test_pipeline.py until plan step 6.2
(finding E22): it tests the tutor's turn pipeline, not the content pipeline.

Network-free: the API-calling stages (classify, tool phase, actor, judge) are
patched; the deterministic detector and the telemetry run for real. Async
generators are driven with asyncio.run.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import turn as agent
from tutor.agent.conduct import WARN_MESSAGE
from tutor.agent.detector import SUBSTITUTION_LINE
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.judge import Verdict
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import Student, TurnEvent

pytestmark = pytest.mark.usefixtures("no_strategist")   # tests/conftest.py


def _collect(student, db, msg):
    async def run():
        return [e async for e in agent.run_turn(student=student, db=db, user_message=msg, course="AST101")]
    return asyncio.run(run())


class _Usage:
    input_tokens = 100
    output_tokens = 20
    cache_read_input_tokens = 30000


def _envelope(escalate=True, max_rung=3):
    return Envelope(
        max_rung=max_rung, goals=[], exam_lockdown=[], must_ground=True,
        allow_exercise=True, escalate_review=escalate, reason="t",
    )


def _fake_round(text, stop="end_turn"):
    """Fake the _stream_round seam (Session 6 single-call actor): yields the
    text as one chunk, then a final message with the given stop_reason."""
    async def fake(system, messages, tools):
        if text:
            yield ("chunk", text)
        final = MagicMock()
        final.stop_reason = stop
        final.content = []
        final.usage = _Usage()
        yield ("final", final)
    return fake


_VIOLATING_DRAFT = (
    "Sure, here is the full solution:\n\n```cpp\n"
    "double brightness(double luminosity, double d) {\n"
    "    if (d <= 0) {\n"
    "        return 0;\n"
    "    }\n"
    "    return luminosity / (d * d);\n"
    "}\n"
    "```\n\nHope that helps."
)


# ---------------------------------------------------------------------------
# Conduct short-circuit
# ---------------------------------------------------------------------------

def test_hostile_message_short_circuits_with_warning(db):
    student = db.get(Student, 1)
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.HOSTILE)):
        events = _collect(student, db, "you are useless garbage")

    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert text == WARN_MESSAGE
    # A conduct event was recorded (the instructor-facing flag), and no actor ran.
    assert db.query(TurnEvent).filter_by(kind="conduct").count() == 1
    assert db.query(TurnEvent).filter_by(kind="chat").count() == 0


# ---------------------------------------------------------------------------
# Risky turn: actor leaks code, detector redacts it before the student sees it
# ---------------------------------------------------------------------------

def test_risky_turn_detector_redacts_leaked_code(db):
    student = db.get(Student, 1)
    bundle = EnvelopeBundle(envelope=_envelope(escalate=True), goal_titles={}, gate_goal=None)

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.SOLUTION_DEMAND, has_attempt=True)), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", _fake_round(_VIOLATING_DRAFT)), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")):
        events = _collect(student, db, "just give me the brightness function")

    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert SUBSTITUTION_LINE in text
    assert "luminosity / (d * d)" not in text   # the leaked solution was cut
    # Telemetry recorded the cut and the classified intent.
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    assert ev.intent == "SOLUTION_DEMAND"
    import json
    assert json.loads(ev.verdicts_json)["verdict"] == "detector_cut"


def test_risky_turn_block_verdict_uses_fallback(db):
    from tutor.agent.judge import FALLBACK_MESSAGE
    student = db.get(Student, 1)
    bundle = EnvelopeBundle(envelope=_envelope(escalate=True), goal_titles={}, gate_goal=None)
    clean_draft = "Here is a hint: think about what happens when the distance doubles."

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.EXAM_LEAK)), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", _fake_round(clean_draft)), \
         patch("tutor.agent.judge.judge", return_value=Verdict("block", "exam_leak", "revealed answer")):
        events = _collect(student, db, "what are the exam answers")

    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert text == FALLBACK_MESSAGE
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    import json
    assert json.loads(ev.verdicts_json)["verdict"] == "block"
