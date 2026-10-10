"""Plan step 5.2 (findings A15, B28): each stage of the turn, on its own.

run_turn was an 827-line async generator with closures mutating nonlocal
state; it is now an orchestrator over tutor/agent/turn_stages/. These tests
drive every stage directly, without the API and without run_turn: the actor
is faked at actor_client.stream_round and generate_once, the supervisors at
their modules. The end-to-end turn stays covered by the run_turn tests.
Done when: run_turn under 200 lines (test_single_actor) and every stage
unit-tested without the API (here).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tutor.agent import mastery, models, state
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.judge import FALLBACK_MESSAGE, Verdict
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.agent.tools import TOOL_DEFINITIONS
from tutor.agent.turn_stages import (
    actor_client, actor_loop, cli_turn, context, errors, exam_intercept, persist, preflight,
    supervision,
)
from tutor.agent.turn_stages.dbstep import db_step
from tutor.agent.turn_stages.request import TurnRequest
from tutor.db.models import LearningGoal, MasteryAttempt, Message, Student, TurnEvent
from tests.helpers import make_session

QUESTIONS = {
    "q1": {"question": "What does axial tilt do?", "rubric": "r"},
    "q2": {"question": "Why are summer days longer?", "rubric": "r"},
    "code": {"question": "Write daylight(h).", "rubric": "r", "starter": "", "is_conceptual": False},
}


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Seasons", description="", course="AST101", lecture_num=3))
    s.commit()
    yield s
    s.close()


def _req(db, message="how do the seasons work?", **kw) -> TurnRequest:
    return TurnRequest(student=db.get(Student, 1), user_message=message, course="AST101", **kw)


def _collect(agen) -> list[dict]:
    async def go():
        return [e async for e in agen]
    return asyncio.run(go())


def _envelope(max_rung=4, goals=(), lockdown=(), escalate=False, allow_exercise=True) -> Envelope:
    return Envelope(max_rung=max_rung, goals=list(goals), exam_lockdown=list(lockdown),
                    must_ground=True, allow_exercise=allow_exercise, escalate_review=escalate,
                    reason="t")


def _bundle(**kw) -> EnvelopeBundle:
    return EnvelopeBundle(envelope=_envelope(**kw), goal_titles={}, gate_goal=None)


class _Usage:
    def __init__(self, inp=100, out=20, read=0, write=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = read
        self.cache_creation_input_tokens = write


# ---------------------------------------------------------------------------
# request, dbstep, errors
# ---------------------------------------------------------------------------

def test_the_request_reads_the_student_id_once(db):
    req = _req(db)
    assert req.sid == 1 and len(req.turn_id) == 8 and req.elapsed_ms() >= 0
    assert req.skip_gate is False and req.gate_trigger_id is None


def test_a_db_step_commits_or_rolls_back_as_one_transaction(db):
    def good():
        db.add(Message(student_id=1, role="user", content="kept", course="AST101"))

    def bad():
        db.add(Message(student_id=1, role="user", content="dropped", course="AST101"))
        raise RuntimeError("boom")

    async def go():
        await db_step(db, good)
        with pytest.raises(RuntimeError):
            await db_step(db, bad)

    asyncio.run(go())
    assert [m.content for m in db.query(Message).all()] == ["kept"]


def test_errors_name_the_cause_and_never_leak_it():
    auth = MagicMock(status_code=401)
    assert errors.error_kind(auth) == "unavailable"
    assert errors.error_kind(Exception("Error code: 529 overloaded")) == "overloaded"
    assert errors.error_kind(httpx.ConnectError("down")) == "connection"
    assert errors.error_kind(ValueError("boom")) == "other"
    assert errors.stream_error_note("overloaded", partial=True).startswith("[Reply incomplete:")
    assert errors.stream_error_note("nonsense", partial=False) == errors.CRASH_NOTE
    message = errors.friendly_error(RuntimeError("secret internals sk-123"))
    assert "sk-123" not in message and "try again" in message


# ---------------------------------------------------------------------------
# actor_client
# ---------------------------------------------------------------------------

def test_the_ledger_prices_each_call_with_the_model_that_served_it():
    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value=models.ACTOR.id):
        ledger = actor_client.UsageLedger()
        ledger.add(_Usage(inp=1000, out=100))                                   # the actor
        ledger.add(actor_client.Served(_Usage(inp=1000, out=100, write=4000),
                                       models.SUPERVISOR.id))                   # a fallback
        ledger.add(None)
    from tutor.agent.usage import estimate_cost_usd

    assert ledger.totals["in"] == 2000 and ledger.totals["out"] == 200
    assert ledger.totals["cache_write"] == 4000
    assert ledger.totals["cost"] == pytest.approx(
        estimate_cost_usd(models.ACTOR.id, 1000, 100, 0, 0)
        + estimate_cost_usd(models.SUPERVISOR.id, 1000, 100, 0, 4000))
    assert ledger.model == models.SUPERVISOR.id          # the last call's model names the turn


def test_a_call_that_must_not_start_a_tool_round_still_declares_every_tool():
    assert actor_client.TOOLS_OFF["tools"] is TOOL_DEFINITIONS
    assert actor_client.TOOLS_OFF["tool_choice"] == {"type": "none"}


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

def test_the_context_sends_the_answered_message_once(db):
    db.add_all([Message(student_id=1, role="user", content="what is a solstice?", course="AST101"),
                Message(student_id=1, role="assistant", content="The longest day...", course="AST101")])
    db.commit()
    req = _req(db, "and an equinox?")
    req.gate_trigger_id = asyncio.run(persist.save_user_message(db, req))
    with patch("tutor.agent.memory.session_summary", return_value=""):
        ctx, messages = asyncio.run(context.read(db, req))
    assert "STUDENT CONTEXT" in ctx and "Course: AST 101" in ctx
    assert [m["content"] for m in messages] == ["what is a solstice?", "The longest day...", "and an equinox?"]


# ---------------------------------------------------------------------------
# exam_intercept
# ---------------------------------------------------------------------------

def _pending_exam(db, held_question="how do I find the Sun's noon altitude?"):
    held = Message(student_id=1, role="user", content=held_question, course="AST101", lecture_num=2)
    db.add(held)
    db.commit()
    attempt = state.start_exam_attempt(db, 1, "G1", json.dumps(QUESTIONS), trigger_message_id=held.id)
    db.commit()
    return held, attempt


def test_no_pending_exam_leaves_the_turn_alone(db):
    req = _req(db)
    assert _collect(exam_intercept.handle_active_exam(db, req)) == []
    assert req.user_message == "how do the seasons work?" and not req.skip_gate


def test_a_clarify_answers_without_using_the_attempt(db):
    _held, attempt = _pending_exam(db)
    req = _req(db, "what does Q2 mean?")
    with patch("tutor.agent.mastery.classify_exam_message", return_value="CLARIFY"), \
         patch("tutor.agent.mastery.clarify_exam", return_value="Q2 asks why the days get longer."):
        events = _collect(exam_intercept.handle_active_exam(db, req))
    assert [e["type"] for e in events] == ["token", "done"]
    assert db.query(Message).filter_by(role="assistant").one().content.startswith("Q2 asks")
    db.refresh(attempt)
    assert attempt.status == "pending" and attempt.attempts == 0


def test_a_bare_not_now_answers_the_held_question_ungated(db):
    held, attempt = _pending_exam(db)
    req = _req(db, "Not now")
    with patch("tutor.agent.mastery.classify_exam_message", return_value="DEFER"):
        events = _collect(exam_intercept.handle_active_exam(db, req))
    assert [e["type"] for e in events] == ["token", "section"]
    assert events[0]["text"] == mastery.DEFER_ACK
    assert (req.user_message, req.lecture_num, req.gate_trigger_id, req.skip_gate) == \
        (held.content, 2, held.id, True)
    db.refresh(attempt)
    assert attempt.status == "deferred"


def test_a_pass_replays_the_held_question_after_the_result(db):
    held, _attempt = _pending_exam(db)
    req = _req(db, "Q1: it tips the axis. Q2: the Sun is higher. Code: ...")

    async def graded(attempt, answer, student_id, session):
        yield {"type": "token", "text": "Passed, 3 of 3."}
        yield {"type": "exam_result", "passed": True}
        yield {"type": "done", "full_text": "Passed, 3 of 3."}

    with patch("tutor.agent.mastery.classify_exam_message", return_value="ANSWER"), \
         patch("tutor.agent.mastery.stream_grade_exam", graded):
        events = _collect(exam_intercept.handle_active_exam(db, req))
    assert [e["type"] for e in events] == ["token", "token", "section"]
    assert "Back to your question" in events[1]["text"]
    assert req.user_message == held.content and req.skip_gate and req.gate_trigger_id == held.id


def test_a_failed_answer_ends_the_turn(db):
    _pending_exam(db)
    req = _req(db, "no idea")

    async def graded(attempt, answer, student_id, session):
        yield {"type": "token", "text": "Not yet: review axial tilt."}
        yield {"type": "exam_result", "passed": False}
        yield {"type": "done", "full_text": "Not yet: review axial tilt."}

    with patch("tutor.agent.mastery.classify_exam_message", return_value="ANSWER"), \
         patch("tutor.agent.mastery.stream_grade_exam", graded):
        events = _collect(exam_intercept.handle_active_exam(db, req))
    assert [e["type"] for e in events] == ["token", "done"]
    assert req.user_message == "no idea" and not req.skip_gate


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def test_a_conduct_reply_is_saved_and_ends_the_turn(db):
    req = _req(db)
    with patch("tutor.agent.conduct.conduct_response", return_value="Let's keep it respectful."):
        reply = asyncio.run(preflight.conduct_reply(db, req, Classification(intent=Intent.QUESTION)))
    saved = db.query(Message).filter_by(role="assistant").one()
    # The text and its row id: the "done" event names the reply to the client.
    assert reply == ("Let's keep it respectful.", saved.id)
    assert saved.content == reply[0]


@pytest.mark.parametrize("code_adjacent,skip_gate,eligible", [
    (True, False, True), (True, True, False), (False, False, False),
])
def test_only_an_unskipped_code_adjacent_turn_may_be_gated(db, code_adjacent, skip_gate, eligible):
    req = _req(db, skip_gate=skip_gate)
    with patch("tutor.agent.envelope.assemble_envelope", return_value=_bundle()) as assemble:
        asyncio.run(preflight.assemble_envelope(
            db, req, Classification(intent=Intent.ATTEMPT), code_adjacent))
    assert assemble.call_args.kwargs["gate_eligible"] is eligible


def test_the_gate_starts_an_exam_with_a_not_now_button(db):
    req = _req(db)
    bundle = EnvelopeBundle(envelope=_envelope(), goal_titles={}, gate_goal=db.get(LearningGoal, "G1"))
    attempt = MagicMock(questions_json=json.dumps(QUESTIONS))
    with patch("tutor.agent.mastery.start_exam", return_value=attempt) as started:
        events = asyncio.run(preflight.start_gate(db, req, bundle))
    assert [e["type"] for e in events] == ["exam", "token", "done"]
    assert events[0] == {"type": "exam", "goal": "Seasons", "not_now": mastery.NOT_NOW}
    assert started.call_args.kwargs["trigger_message_id"] is None
    assert "Quick Mastery Check" in db.query(Message).filter_by(role="assistant").one().content


def test_admins_are_never_gated(db):
    req = _req(db)
    bundle = EnvelopeBundle(envelope=_envelope(), goal_titles={}, gate_goal=db.get(LearningGoal, "G1"))
    with patch("tutor.auth.access.is_admin", return_value=True), \
         patch("tutor.agent.mastery.start_exam") as started:
        assert asyncio.run(preflight.start_gate(db, req, bundle)) == []
    started.assert_not_called()


def test_a_failed_strategist_leaves_the_envelope_as_it_was(db):
    req = _req(db)
    bundle = _bundle()
    before = bundle.envelope
    with patch("tutor.agent.trajectory.compute_trajectory", side_effect=RuntimeError("db hiccup")):
        asyncio.run(preflight.consult_strategist(db, req, bundle, Classification(intent=Intent.QUESTION)))
    assert bundle.envelope is before


# ---------------------------------------------------------------------------
# actor_loop
# ---------------------------------------------------------------------------

def _prepared(bundle=None, intent=Intent.QUESTION, code_adjacent=False):
    with patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        return actor_loop.prepare(bundle or _bundle(), Classification(intent=intent), code_adjacent,
                                  "CTX", [{"role": "user", "content": "q"}])


def test_prepare_settles_supervision_before_the_first_token():
    routine = _prepared(_bundle(max_rung=6))
    assert not routine.risky and not routine.strict_detect and not routine.gate.strict
    assert routine.system[0]["text"] == "PERSONA" and routine.system[1]["text"].startswith("CTX")
    locked = _prepared(_bundle(max_rung=1, lockdown=["G1"], escalate=True))
    assert locked.risky and locked.strict_detect and locked.gate.strict
    assert "find_topic" not in {t["name"] for t in locked.turn_tools}


def test_a_failed_stream_is_recorded_not_raised(db):
    run = _prepared()

    async def broken(system, messages, tools):
        raise RuntimeError("Error code: 529 overloaded")
        yield   # pragma: no cover  (makes this an async generator)

    with patch("tutor.agent.turn_stages.actor_client.stream_round", broken):
        assert _collect(actor_loop.run_rounds(db, _req(db), run)) == []
    assert run.stream_error and run.stream_error_kind == "overloaded"
    assert "overloaded" in run.stream_error_message


def test_a_retrieved_slide_widens_the_scope_but_keeps_the_ceiling(db):
    run = _prepared(_bundle(max_rung=5))
    run.slide_ref = {"lecture_num": 3}
    refined = EnvelopeBundle(envelope=_envelope(max_rung=2, goals=["G1"]),
                             goal_titles={"G1": "Seasons"}, gate_goal=None)
    with patch("tutor.agent.envelope.assemble_envelope", return_value=refined) as assemble:
        actor_loop.refine_from_slide(db, _req(db), run)
    assert assemble.call_args.kwargs["gate_eligible"] is False    # a slide never gates
    assert run.bundle.envelope.goals == ["G1"]
    assert run.bundle.envelope.max_rung == 5 and "kept H5" in run.bundle.envelope.reason
    assert "Seasons" in run.env_block


def test_a_lockdown_from_the_slide_does_lower_the_ceiling(db):
    run = _prepared(_bundle(max_rung=5))
    run.slide_ref = {"lecture_num": 3}
    refined = EnvelopeBundle(envelope=_envelope(max_rung=1, goals=["G1"], lockdown=["G1"]),
                             goal_titles={}, gate_goal=None)
    with patch("tutor.agent.envelope.assemble_envelope", return_value=refined):
        actor_loop.refine_from_slide(db, _req(db), run)
    assert run.bundle.envelope.max_rung == 1
    assert "find_topic" not in {t["name"] for t in run.turn_tools}


# ---------------------------------------------------------------------------
# supervision
# ---------------------------------------------------------------------------

def test_a_routine_reply_ends_with_the_error_after_what_streamed(db):
    run = _prepared(_bundle(max_rung=6))
    run.out_parts = ["Start with the tilt."]
    run.stream_error, run.stream_error_message = True, "The tutor is briefly overloaded."
    reply = supervision.Reply()
    events = _collect(supervision.supervise(db, _req(db), run, reply, []))
    assert events == [{"type": "error", "message": "The tutor is briefly overloaded."}]
    assert reply.full_text == "Start with the tilt." and not reply.judged


def test_a_blocked_draft_becomes_the_fallback(db):
    run = _prepared(_bundle(escalate=True))
    run.draft_parts = ["Here is the whole solution in words."]
    reply = supervision.Reply()
    with patch("tutor.agent.judge.judge", return_value=Verdict("block", rule="solution", reason="gives it away")):
        events = _collect(supervision.supervise(db, _req(db), run, reply, []))
    assert "".join(e["text"] for e in events) == FALLBACK_MESSAGE == reply.full_text
    assert (reply.verdict_label, reply.judge_rule, reply.judged) == ("block", "solution", True)


def test_a_revise_that_still_carries_code_becomes_the_fallback(db):
    run = _prepared(_bundle(escalate=True))
    run.draft_parts = ["Try this approach."]
    run.executed = [("find_topic", '{"results": []}')]
    reply = supervision.Reply()
    leaky = "```cpp\nint area(int w, int h) { return w > 0 && h > 0 ? w * h : 0; }\n```"
    with patch("tutor.agent.judge.judge", return_value=Verdict("revise", rule="r", reason="too direct")), \
         patch("tutor.agent.turn_stages.actor_client.generate_once", return_value=(leaky, None)) as revise:
        _collect(supervision.supervise(db, _req(db), run, reply, []))
    sent = revise.call_args.args[1]
    assert sent[-1]["content"].startswith("[SUPERVISOR]")
    assert any(isinstance(m["content"], str) and m["content"].startswith("[RETRIEVED COURSE CONTEXT")
               for m in sent)
    assert reply.full_text == FALLBACK_MESSAGE and reply.detector_cut and reply.verdict_label == "revise"


def test_settle_never_saves_a_blank_or_silently_cut_reply():
    run = _prepared()
    run.stream_error, run.stream_error_kind = True, "connection"
    partial = supervision.Reply(full_text="Half an answer")
    assert supervision.settle(run, partial) == []
    assert partial.full_text.endswith("[Reply incomplete: the tutor lost its connection to the AI service.]")
    assert partial.verdict_label == "stream_error"
    empty = supervision.Reply()
    supervision.settle(run, empty)
    assert empty.full_text.startswith("[No reply:")
    run.stream_error = False
    blank = supervision.Reply(full_text="  ")
    assert supervision.settle(run, blank) == [{"type": "error", "message": errors.EMPTY_REPLY_MESSAGE}]
    assert (blank.full_text, blank.verdict_label) == (errors.EMPTY_REPLY_NOTE, "empty_reply")


# ---------------------------------------------------------------------------
# persist
# ---------------------------------------------------------------------------

def test_verdicts_record_what_happened_and_why():
    run = _prepared()
    run.tool_rounds, run.stop_reasons, run.supervision_tightened = 2, ["tool_use", "max_tokens"], True
    run.stream_error, run.stream_error_kind = True, "overloaded"
    reply = supervision.Reply(verdict_label="revise", judge_rule="r1", judge_reason="x" * 400,
                              detector_cut=True, judged=True)
    v = persist.verdicts_for(run, reply)
    assert v["verdict"] == "revise" and v["tool_rounds"] == 2 and v["supervision_tightened"]
    assert v["stop_reason"] == "max_tokens" and v["truncated"] is True
    assert v["judge_rule"] == "r1" and len(v["judge_reason"]) == 300
    assert v["stream_error"] and v["stream_error_kind"] == "overloaded"


def test_the_reply_and_its_ledger_are_saved_together(db):
    req = _req(db)
    bundle = _bundle()
    classification = Classification(intent=Intent.QUESTION)
    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value=models.ACTOR.id):
        provisional = asyncio.run(persist.record_provisional_event(db, req, bundle, classification))
        run = _prepared(bundle)
    assert json.loads(provisional.verdicts_json) == {"verdict": "in_progress"}
    run.ledger.add(_Usage(inp=400, out=120))
    run.tool_notes = [{"tool": "find_topic", "query": "seasons"}]
    run.side_calls = [{"model": models.ACTOR.id, "in_tokens": 50, "out_tokens": 5}]
    run.slide_ref = {"lecture_title": "Seasons", "slide_number": 4}
    reply = supervision.Reply(full_text="Start with the tilt.")
    asyncio.run(persist.save_reply(db, req, run, reply, provisional))
    saved = db.query(Message).filter_by(role="assistant").one()
    assert saved.content == "Start with the tilt." and "find_topic" in saved.tool_summary
    chat = db.query(TurnEvent).filter_by(kind="chat").one()
    assert json.loads(chat.verdicts_json)["verdict"] == "allow"
    assert (chat.in_tokens, chat.out_tokens, chat.model) == (400, 120, models.ACTOR.id)
    assert chat.cost_usd > 0 and json.loads(chat.sources_json) == ["Seasons slide 4"]
    assert db.query(TurnEvent).filter_by(kind="evaluate").count() == 1


def test_a_learning_failure_is_logged_and_the_reply_stays(db, tutor_log):
    req = _req(db, lecture_num=3)
    run = _prepared()
    with patch("tutor.agent.state.record_topic_access", side_effect=RuntimeError("db hiccup")):
        asyncio.run(persist.record_learning(db, req, run, supervision.Reply(full_text="ok")))
    assert "progress bookkeeping failed" in tutor_log.text


def test_a_slow_turn_is_flagged(db):
    req = _req(db)
    run = _prepared()
    with patch("tutor.logging_config.slow_turn_ms", return_value=0), \
         patch("tutor.logging_config.activity") as activity:
        persist.log_turn(req, run, supervision.Reply(full_text="ok"))
    assert [c.args[0] for c in activity.call_args_list] == ["chat_turn", "slow_turn"]


# ---------------------------------------------------------------------------
# cli_turn
# ---------------------------------------------------------------------------

def test_the_cli_done_waits_for_the_saved_reply(db):
    req = _req(db, lecture_num=3)
    seen: dict = {}

    async def fake_cli(system_prompt, messages, course):
        seen["system"] = system_prompt
        yield {"type": "token", "text": "Through the CLI."}
        yield {"type": "done", "full_text": "Through the CLI."}

    async def go():
        events = []
        async for event in cli_turn.run(db, req, "CTX", [{"role": "user", "content": "q"}]):
            if event["type"] == "done":
                seen["saved"] = db.query(Message).filter_by(role="assistant").count()
            events.append(event)
        return events

    with patch("tutor.agent.cli_backend.stream_cli", fake_cli), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="SYS"), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=_bundle()):
        events = asyncio.run(go())
    assert [e["type"] for e in events] == ["token", "done"]
    assert seen["saved"] == 1 and seen["system"].startswith("SYSCTX")
    assert db.query(MasteryAttempt).count() == 0
