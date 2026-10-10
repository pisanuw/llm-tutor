"""Task 1 safety tests: no solution reveal, prompt-cache block structure.

All tests are network-free and env-independent (the Anthropic client and the
persona prompt are patched), so they run in CI with no API key.

Covers:
- student_facing_eval        strips eval_details / improved_code  (exercises.py)
- run_turn                   never hands a solution to the actor,
                             not even in the revise call's context (turn.py)
- _system_blocks             cached persona prefix + uncached ctx  (turn.py)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.exercises import student_facing_eval
from tutor.agent.judge import Verdict
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import Student
from tutor.agent.turn_stages import actor_client
from tests.helpers import ToolUse


# ---------------------------------------------------------------------------
# student_facing_eval (pure)
# ---------------------------------------------------------------------------

def test_student_facing_eval_drops_eval_details_and_improved_code():
    full = {
        "score": 1,
        "correct": False,
        "feedback": "Off-by-one in the loop bound.",
        "hint": "What index does the loop stop at?",
        "eval_details": {"improved_code": "for (int i = 0; i < n; ++i) {}"},
        "improved_code": "for (int i = 0; i < n; ++i) {}",  # stray, must also drop
    }
    safe = student_facing_eval(full)
    assert set(safe.keys()) == {"score", "correct", "feedback", "hint"}
    assert "eval_details" not in safe
    assert "improved_code" not in safe
    # The original dict is untouched (pure function).
    assert "eval_details" in full


def test_student_facing_eval_tolerates_missing_keys():
    safe = student_facing_eval({"score": 4})
    assert safe == {"score": 4}


# ---------------------------------------------------------------------------
# run_turn: the actor must never receive improved_code, not even on a revise
# ---------------------------------------------------------------------------

SOLUTION = "if (d <= 0) return 0;"


def test_the_solution_never_reaches_the_actor_even_on_a_revise(db):
    """The continuation stream and the tools-free revise call both see only
    the student-facing keys; the server keeps the full evaluation."""
    from tutor.agent.strategist import NO_MOVE

    eval_full = {
        "score": 1,
        "correct": False,
        "feedback": "The zero-distance check is missing.",
        "hint": "What should happen when the distance is zero?",
        "eval_details": {"improved_code": SOLUTION},
    }
    script = iter([
        ([], "tool_use", [ToolUse("evaluate_code", "tu_1", student_code="int main(){}", problem="p")]),
        (["Close: look at where it stops."], "end_turn", []),
    ])
    streamed: list = []
    revised: list = []

    async def rounds(system, messages, tools):
        streamed.append(messages)
        chunks, stop, content = next(script)
        for c in chunks:
            yield ("chunk", c)
        yield ("final", MagicMock(stop_reason=stop, content=list(content), usage=None, model=None))

    def generate_once(system, messages):
        revised.append(messages)
        return "Which input should return right away?", None

    envelope = Envelope(max_rung=4, goals=[], exam_lockdown=[], must_ground=True,
                        allow_exercise=True, escalate_review=True, reason="t")

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message="grade this",
            lecture_num=None, course="AST101",
        )]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.ATTEMPT, has_attempt=True)), \
         patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE), \
         patch("tutor.agent.mastery.get_active_exam", return_value=None), \
         patch("tutor.agent.envelope.assemble_envelope",
               return_value=EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", rounds), \
         patch("tutor.agent.tools.dispatch_tool", return_value=eval_full), \
         patch("tutor.agent.turn_stages.actor_client.generate_once", side_effect=generate_once), \
         patch("tutor.agent.turn_stages.persist.log_eval_details") as logged, \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("revise", rule="r", reason="too direct")):
        events = asyncio.run(go())

    # The continuation stream's tool_result: student-facing keys only.
    payload = streamed[1][-1]["content"][0]["content"]
    assert "The zero-distance check is missing." in payload
    assert "improved_code" not in payload and "eval_details" not in payload and SOLUTION not in payload

    # The revise call is tools-free, so the exchange is flattened to text:
    # still no solution.
    assert len(revised) == 1
    context = [m["content"] for m in revised[0]
               if isinstance(m["content"], str) and m["content"].startswith("[RETRIEVED COURSE CONTEXT")]
    assert len(context) == 1 and "The zero-distance check is missing." in context[0]
    assert "improved_code" not in context[0] and SOLUTION not in context[0]

    # The student sees the revised reply; the server keeps the full eval.
    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "Which input should return right away?" in text and SOLUTION not in text
    assert logged.call_args.args[1]["eval_details"]["improved_code"] == SOLUTION


# ---------------------------------------------------------------------------
# _system_blocks: cache breakpoint on the stable prefix, ctx last & uncached
# ---------------------------------------------------------------------------

def test_system_blocks_caches_prefix_and_appends_uncached_context():
    with patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA PREFIX"):
        blocks = actor_client.system_blocks("STUDENT CONTEXT: ...")

    assert len(blocks) == 2
    # First block is the cached persona prefix.
    assert blocks[0]["text"] == "PERSONA PREFIX"
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Second block is the per-request context, and it is NOT cached.
    assert blocks[1]["text"] == "STUDENT CONTEXT: ..."
    assert "cache_control" not in blocks[1]


def test_system_blocks_no_context_is_single_cached_block():
    with patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA PREFIX"):
        blocks = actor_client.system_blocks()
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
