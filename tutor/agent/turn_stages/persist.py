"""Persistence and telemetry for a turn, in two stages (plan step 2.5, A13).

Stage 1 commits the student's message and a provisional "chat" TurnEvent
before the actor runs, so the turn is on the ledger and the daily cap even if
a later step fails. Stage 2a commits the reply and the finished event before
"done"; stage 2b commits learning-state bookkeeping on its own, so a failure
there cannot take the student's answer or its cost down with it.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

from sqlalchemy.orm import Session as DBSession

from tutor import logging_config
from tutor.agent import exercises
from tutor.agent import memory as memory_mod
from tutor.agent import progress
from tutor.agent import state as state_mod
from tutor.agent import usage as usage_mod
from tutor.agent.turn_stages import actor_client
from tutor.agent.turn_stages.dbstep import db_step
from tutor.agent.turn_stages.request import TurnRequest
from tutor.agent.turn_stages.supervision import sources_from_slide
from tutor.db.models import Message

if TYPE_CHECKING:
    from tutor.agent.envelope import EnvelopeBundle
    from tutor.agent.preclassify import Classification
    from tutor.agent.turn_stages.actor_loop import ActorRun
    from tutor.agent.turn_stages.supervision import Reply
    from tutor.db.models import TurnEvent


async def save_user_message(db: DBSession, req: TurnRequest) -> Optional[int]:
    """Commit the student's message; its id becomes the gate trigger if a
    mastery exam starts this turn. If the turn fails later, the chat router
    pairs it with a note so history never ends on an unanswered message."""
    row = Message(
        student_id=req.sid,
        role="user",
        content=req.user_message,
        lecture_num=req.lecture_num,
        slide_num=req.slide_num,
        course=req.course,
        turn_id=req.turn_id,
    )
    db.add(row)
    await db_step(db)
    return row.id


async def save_assistant_message(db: DBSession, req: TurnRequest, content: str) -> int:
    """A reply that is a whole section of the turn (an exam step, a conduct
    boundary, a prerequisite check), committed at once. Returns its row id,
    which the "done" or "section" event carries to the client."""
    row = Message(
        student_id=req.sid, role="assistant", content=content,
        lecture_num=req.lecture_num, course=req.course, turn_id=req.turn_id,
    )
    db.add(row)
    await db_step(db)
    return row.id


async def record_provisional_event(
    db: DBSession, req: TurnRequest, bundle: "EnvelopeBundle", classification: "Classification",
) -> "TurnEvent":
    """Stage 1: the provisional chat event, committed before the actor runs.
    Stage 2a fills it in."""
    def _stage1():
        return state_mod.record_turn_event(
            db,
            student_id=req.sid,
            kind="chat",
            goal_ids=bundle.envelope.goals or None,
            envelope=bundle.envelope,
            intent=classification.intent.value,
            rung_used=bundle.envelope.max_rung,
            verdicts={"verdict": "in_progress"},
            model=actor_client.actor_model(),
            turn_id=req.turn_id,
        )

    return await db_step(db, _stage1)


def verdicts_for(run: "ActorRun", reply: "Reply") -> dict:
    """The supervision record on the turn's event: what the student got and
    why, for the supervision tab, the auditor, and the sim."""
    verdicts: dict = {
        "verdict": reply.verdict_label,
        "detector_cut": reply.detector_cut,
        "judged": reply.judged,
        "tool_rounds": run.tool_rounds,
    }
    if run.supervision_tightened:
        verdicts["supervision_tightened"] = True
    if run.stop_reasons:
        verdicts["stop_reason"] = run.stop_reasons[-1]
        if "max_tokens" in run.stop_reasons:
            verdicts["truncated"] = True   # a round hit the output cap (plan 2.4)
    if reply.judge_rule:
        verdicts["judge_rule"] = reply.judge_rule
    if reply.judge_rule == "judge_unavailable":
        # A block because the judge produced no verdict, not because the
        # draft was wrong: told apart on the Supervision tab and counted as
        # a failure by the digest (review step 0.8, F51).
        verdicts["judge_unavailable"] = True
    if getattr(run.classification, "degraded", False):
        verdicts["classifier_unavailable"] = True
    if reply.judge_reason:
        verdicts["judge_reason"] = reply.judge_reason[:300]
    if run.stream_error:
        verdicts["stream_error"] = True
        verdicts["stream_error_kind"] = run.stream_error_kind
    return verdicts


async def save_reply(
    db: DBSession, req: TurnRequest, run: "ActorRun", reply: "Reply", provisional_event: "TurnEvent",
) -> int:
    """Stage 2a: the reply and the turn's ledger rows, committed on their own.

    Per-turn telemetry: envelope, intent, rung, verdict, tokens, and the
    estimated cost (P6, and the ledger the spend caps read). Token counts are
    summed across every actor call in the turn (tool rounds and the revise).
    Returns the reply's message id.
    """
    verdicts = verdicts_for(run, reply)
    totals = run.ledger.totals
    latency_ms = req.elapsed_ms()
    if req.timings:
        verdicts["timings"] = dict(req.timings)
    envelope = run.bundle.envelope

    row = Message(
        student_id=req.sid,
        role="assistant",
        content=reply.full_text,
        lecture_num=req.lecture_num,
        slide_num=req.slide_num,
        course=req.course,
        # Compact memory of what the actor showed (S16): rendered back
        # into history so it can reference the slide turns later.
        tool_summary=json.dumps(run.tool_notes) if run.tool_notes else None,
        turn_id=req.turn_id,
    )

    def _stage2a():
        db.add(row)
        # The rubric evaluator's own call (actor model, 1,024 tokens): its
        # own ledger row, so the chat row keeps one model's tokens.
        for call in run.side_calls:
            state_mod.record_turn_event(
                db, student_id=req.sid, kind="evaluate",
                **usage_mod.cost_fields(str(call.get("model") or actor_client.actor_model()), call),
            )
        state_mod.finalize_turn_event(
            db,
            provisional_event,
            goal_ids=envelope.goals or None,
            envelope=envelope,
            intent=run.classification.intent.value,
            rung_used=envelope.max_rung,
            verdicts=verdicts,
            grounding_hit=bool(run.slide_ref),
            sources=sources_from_slide(run.slide_ref),
            model=run.ledger.model,
            in_tokens=totals["in"],
            out_tokens=totals["out"],
            cache_read_tokens=totals["cache"],
            cache_write_tokens=totals["cache_write"],
            cost_usd=totals["cost"],
            latency_ms=latency_ms,
        )

    await db_step(db, _stage2a)
    return row.id


def log_eval_details(student_id: Optional[int], eval_result: Optional[dict]) -> None:
    """Record the server-side-only eval_details (improved_code) for instructors.

    The student never sees this. Task 1 interim home is the log; Task 3 routes
    eval_details into the TurnEvent telemetry column for proper review.
    """
    if not eval_result:
        return
    details = eval_result.get("eval_details")
    if details:
        logging.getLogger("tutor.eval_details").info(
            "student=%s eval_details=%s", student_id, json.dumps(details, default=str)
        )


async def record_learning(db: DBSession, req: TurnRequest, run: "ActorRun", reply: "Reply") -> None:
    """Stage 2b: learning-state bookkeeping, committed on its own. A failure is
    logged and rolled back; the reply and its ledger row stay."""
    def _stage2b():
        eval_result = run.eval_result
        # Exercise outcome (ExerciseAttempt + graded events + exercise event)
        # goes through the single writer.
        if eval_result:
            log_eval_details(req.sid, eval_result)
            # Use the exercise_id from the tool result when available so graded
            # events are attributed to the right goal (Session 17). Fall back to
            # the old chat-based synthetic id for unstructured code submissions.
            eid = eval_result.get("exercise_id") or f"chat-{req.sid}-{req.lecture_num or 0}"
            ex_goal_ids = exercises.goals_for_exercise(eid)
            # Plan step 1.5 (B4): a bank exercise credits its own goals; an
            # unstructured submission credits only the goals the classifier
            # found in this turn, never every goal of the lecture.
            credit_goal_ids = ex_goal_ids or list(run.classification.goal_ids or [])
            state_mod.record_exercise_outcome(
                db,
                student_id=req.sid,
                exercise_id=eid,
                lecture_num=req.lecture_num,
                submission=req.user_message,
                score=float(eval_result.get("score", 0)),
                passed=bool(eval_result.get("correct", False)),
                feedback=reply.full_text,
                course=req.course,
                goal_ids=credit_goal_ids or None,
                # S29: tag whether the score came from deterministic tests or
                # the LLM rubric, so mastery from tests is the strongest class.
                evidence_source=eval_result.get("eval_source", "llm-opinion"),
            )

        # Mark goals as "seen" (chat exposure): does NOT set unlocked status.
        # "Unlocked" means prerequisites mastered (navigator frontier, S17).
        seen_lec = run.found_lecture_num or req.lecture_num
        if seen_lec:
            progress.mark_goals_seen_for_lecture(req.sid, db, req.course, seen_lec)
        if req.lecture_num:
            state_mod.record_topic_access(db, req.sid, req.lecture_num, req.slide_num, req.user_message)

    try:
        await db_step(db, _stage2b)       # rolled back on failure
    except Exception:
        logging.getLogger("tutor").exception(
            "turn %s: progress bookkeeping failed; the reply and its ledger row were saved",
            req.turn_id)


def log_turn(req: TurnRequest, run: "ActorRun", reply: "Reply") -> None:
    """The activity stream (a tailable mirror of the DB; content gated by
    env): the student's turn, the supervisor decision, and the reply, on one
    JSON line."""
    envelope = run.bundle.envelope
    latency_ms = req.elapsed_ms()
    logging_config.activity(
        "chat_turn",
        turn_id=req.turn_id,
        student_id=req.sid, course=req.course,
        lecture_num=req.lecture_num,
        intent=run.classification.intent.value,
        rung_used=envelope.max_rung,
        move=(envelope.move.value if envelope.move else None),
        verdict=reply.verdict_label, detector_cut=reply.detector_cut,
        grounding_hit=bool(run.slide_ref), tool_rounds=run.tool_rounds,
        cost_usd=round(run.ledger.totals["cost"], 6),
        latency_ms=latency_ms,
        content={"user_message": req.user_message, "reply": reply.full_text},
    )
    # A completed-but-slow turn is the softer cousin of the "thinking forever"
    # hang: surface it at the minimal tier and as a WARNING so it shows even at a
    # coarse LOG_LEVEL and can be correlated by turn_id.
    if latency_ms >= logging_config.slow_turn_ms():
        logging_config.activity("slow_turn", level="minimal", turn_id=req.turn_id,
                                student_id=req.sid, course=req.course,
                                latency_ms=latency_ms, tool_rounds=run.tool_rounds,
                                timings=dict(req.timings))
        logging.getLogger("tutor").warning(
            "slow turn %s for student %s: %d ms (%d tool rounds) %s",
            req.turn_id, req.sid, latency_ms, run.tool_rounds,
            " ".join(f"{k}={v}" for k, v in sorted(req.timings.items())))


async def summarize(db: DBSession, req: TurnRequest) -> None:
    """Fold older messages into the rolling per-course summary when enough
    have accumulated (one Haiku call after the reply; fail-soft, S16)."""
    try:
        await memory_mod.maybe_summarize_async(
            db, req.sid, req.course, db_step=lambda fn, *args: db_step(db, fn, *args),
        )
    except Exception:
        # The verbatim window still covers the newest messages; the failure
        # is logged, not hidden (plan step 5.6). db_step rolled it back.
        logging.getLogger("tutor").warning(
            "turn %s: the rolling summary was not updated", req.turn_id, exc_info=True)
