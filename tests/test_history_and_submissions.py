"""Plan step 2.4 (findings A4, A5): history and long submissions.

A4: the current message was flushed before the history was read, so the
actor saw it twice. A5: a round that echoed a long submission into
evaluate_code hit the output cap, no evaluation ran, and an empty reply was
saved. Done when: the A4 probe shows one copy; a 300-line submission is
evaluated.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch


from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import Message, Student, TurnEvent
from tutor.agent.turn_stages import actor_loop
from tutor.agent.turn_stages import errors as turn_errors
from tests.helpers import ToolUse

LONG_CODE = "\n".join(
    [f"int step{i}(int x) {{ return x + {i}; }}" for i in range(299)] + ["int main() { return 0; }"]
)
SUBMISSION = f"Here is my whole file, why does it crash?\n\n```cpp\n{LONG_CODE}\n```"


def _turn(db, message, script, dispatch_result=None):
    from tutor.agent.strategist import NO_MOVE

    captured: list = []
    calls = {"n": 0}

    async def rounds(system, messages, tools):
        captured.append({"messages": messages})
        chunks, stop, content = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for c in chunks:
            yield ("chunk", c)
        final = MagicMock()
        final.stop_reason = stop
        final.content = list(content)
        final.usage = None
        final.model = None
        yield ("final", final)

    envelope = Envelope(max_rung=5, goals=["G1"], exam_lockdown=[], must_ground=True,
                        allow_exercise=True, escalate_review=False, reason="band")
    bundle = EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message=message,
            lecture_num=None, course="AST101",
        )]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE), \
         patch("tutor.agent.mastery.get_active_exam", return_value=None), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", rounds), \
         patch("tutor.agent.tools.dispatch_tool",
               return_value=dispatch_result or {"error": "not used"}) as dispatched, \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        events = asyncio.run(go())
    return events, captured, dispatched


def test_the_current_message_reaches_the_actor_once(db):
    """The A4 probe."""
    for role, text in [("user", "what is a parsec?"), ("assistant", "A distance unit from parallax...")]:
        db.add(Message(student_id=1, role=role, content=text, course="AST101"))
    db.commit()
    _, captured, _ = _turn(db, "and how is parallax measured?", [(["Six months apart."], "end_turn", [])])
    sent = captured[0]["messages"]
    copies = sum(1 for m in sent if "how is parallax measured" in str(m["content"]))
    assert copies == 1
    assert sent[-1]["role"] == "user" and sent[-1]["content"].endswith("how is parallax measured?")
    assert [m["content"] for m in sent[:2]] == ["what is a parsec?", "A distance unit from parallax..."]


def test_a_300_line_submission_is_evaluated_in_full(db):
    script = [
        ([], "tool_use", [ToolUse("evaluate_code", "tu_1", student_code="", problem="Find the crash.")]),
        (["Look at main's return path."], "end_turn", []),
    ]
    evaluated = {"score": 2, "correct": False, "feedback": "f", "hint": "h",
                 "eval_details": {"improved_code": ""}, "eval_source": "llm-opinion"}
    _, _, dispatched = _turn(db, SUBMISSION, script, dispatch_result=evaluated)
    name, tool_input = dispatched.call_args.args
    assert name == "evaluate_code"
    assert tool_input["student_code"].strip() == LONG_CODE
    assert len(tool_input["student_code"].splitlines()) == 300


def test_a_truncated_echo_is_replaced_but_a_full_one_is_kept():
    head = "\n".join(LONG_CODE.splitlines()[:10])
    got = actor_loop.attach_submitted_code("evaluate_code", {"student_code": head, "problem": "p"}, SUBMISSION)
    assert got["student_code"].strip() == LONG_CODE
    own = LONG_CODE + "\n// reformatted by the model"
    kept = actor_loop.attach_submitted_code("evaluate_code", {"student_code": own, "problem": "p"}, SUBMISSION)
    assert kept["student_code"] == own
    other = actor_loop.attach_submitted_code("find_topic", {"query": "parallax"}, SUBMISSION)
    assert other == {"query": "parallax"}
    assert actor_loop.attach_submitted_code("evaluate_code", {"student_code": ""}, "no code here") == {"student_code": ""}


def test_an_empty_reply_is_reported_and_never_saved_as_an_answer(db):
    # The round ran out of output budget mid tool call: no text, no tool.
    events, _, _ = _turn(db, SUBMISSION, [([], "max_tokens", [])])
    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["message"] == turn_errors.EMPTY_REPLY_MESSAGE
    saved = db.query(Message).filter_by(role="assistant").one().content
    assert saved == turn_errors.EMPTY_REPLY_NOTE and saved.startswith("[No reply:")
    verdicts = json.loads(db.query(TurnEvent).filter_by(kind="chat").one().verdicts_json)
    assert verdicts["verdict"] == "empty_reply"
    assert verdicts["stop_reason"] == "max_tokens" and verdicts["truncated"] is True


def test_longest_fenced_code_ignores_indented_blocks():
    text = "a\n```\nshort\n```\nb\n```cpp\nlonger one\nhere\n```\n\n    indented and much much longer text\n"
    assert actor_loop.longest_fenced_code(text).strip() == "longer one\nhere"
