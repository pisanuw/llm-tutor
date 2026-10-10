"""A pending mastery exam in this course intercepts the student's message.

A narrow 3-way intent decides the path: ANSWER grades (fail-closed); CLARIFY
answers wording at H1 without consuming the attempt; DEFER pauses the exam
and falls through so the student's actual request is handled normally.
After a pass, the original pre-gate question is replayed through the normal
flow so the gate stops swallowing questions.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from sqlalchemy.orm import Session as DBSession

from tutor.agent import mastery as mastery_mod
from tutor.agent import state as state_mod
from tutor.agent import usage as usage_mod
from tutor.agent.turn_stages import persist
from tutor.agent.turn_stages.dbstep import db_step
from tutor.agent.turn_stages.request import TurnRequest


async def handle_active_exam(db: DBSession, req: TurnRequest) -> AsyncGenerator[dict, None]:
    """Route a message that arrives while an exam is pending.

    Yields the turn's events; a yielded "done" ends the turn. On DEFER, and
    after a pass that held a question back, the turn goes on with ``req``
    rewritten: the held question becomes the message, and it is not gated
    again (``req.skip_gate``). With no pending exam this yields nothing.
    """
    loop = asyncio.get_running_loop()
    # Only this course's pending exam intercepts the message (plan 1.7, B14).
    active_exam = await db_step(db, mastery_mod.get_active_exam, req.sid, db, course=req.course)
    if not active_exam:
        return
    # Both exam-mode calls report their usage and the turn ledgers it (plan
    # step 2.1, finding B15), through db_step like its other database work
    # (plan step 2.6).
    exam_usage: list = []

    def _ledger_exam_calls(kind: str) -> None:
        for counts in exam_usage:
            state_mod.record_turn_event(
                db, student_id=req.sid, kind=kind, goal_ids=[active_exam.goal_id],
                **usage_mod.cost_fields(mastery_mod._GRADER_MODEL, counts),
            )
        exam_usage.clear()

    user_message = req.user_message
    exam_action = await loop.run_in_executor(
        None, lambda: mastery_mod.classify_exam_message(user_message, usage_sink=exam_usage)
    )
    await db_step(db, _ledger_exam_calls, "exam_classify")

    if exam_action == "CLARIFY":
        questions = json.loads(active_exam.questions_json)
        reply = await loop.run_in_executor(
            None, lambda: mastery_mod.clarify_exam(questions, user_message, usage_sink=exam_usage)
        )
        await db_step(db, _ledger_exam_calls, "exam_clarify")
        mid = await persist.save_assistant_message(db, req, reply)
        yield {"type": "token", "text": reply}
        yield {"type": "done", "full_text": reply, "message_id": mid}
        return

    if exam_action == "DEFER":
        await db_step(db, state_mod.defer_exam_attempt, db, active_exam)
        mid = await persist.save_assistant_message(db, req, mastery_mod.DEFER_ACK)
        yield {"type": "token", "text": mastery_mod.DEFER_ACK}
        # One "done" per turn (plan step 2.5, finding A12): the saved
        # acknowledgement closes a section and the answer follows.
        yield {"type": "section", "full_text": mastery_mod.DEFER_ACK, "message_id": mid}
        # Fall through: handle the message normally, and do not gate it.
        # The deferred goal is also snoozed for DEFER_SNOOZE_HOURS, so the
        # next messages on this lecture are answered rather than re-gated.
        req.skip_gate = True
        # A bare "Not now" asks nothing new: answer the question the gate
        # held back, as a pass does (plan step 3.7, finding B13).
        if mastery_mod.is_bare_deferral(user_message):
            held = await db_step(db, mastery_mod.original_gate_question, req.sid, active_exam, db)
            if held is not None:
                _replay(req, held)
        return

    # ANSWER
    full_parts: list[str] = []
    exam_passed = False
    async for event in mastery_mod.stream_grade_exam(active_exam, user_message, req.sid, db):
        if event["type"] == "exam_result":
            exam_passed = bool(event.get("passed"))
            continue  # internal event, not for the client
        if event["type"] == "done":
            continue  # the turn sends its own done
        if event["type"] == "token":
            full_parts.append(event["text"])
        yield event
    replay = (
        await db_step(db, mastery_mod.original_gate_question, req.sid, active_exam, db)
        if exam_passed else None
    )
    if replay is not None:
        transition = f"\n\n---\n\nBack to your question: “{replay.content[:300]}”\n\n"
        full_parts.append(transition)
        yield {"type": "token", "text": transition}
    full_text = "".join(full_parts)
    mid = await persist.save_assistant_message(db, req, full_text)
    if replay is None:
        yield {"type": "done", "full_text": full_text, "message_id": mid}
        return
    # One "done" per turn (plan step 2.5, finding A12): the saved exam result
    # closes a section; the replayed answer is the next one. The swallowed
    # pre-gate question goes through the normal flow (it is already in
    # history; a chained gate keeps its trigger).
    yield {"type": "section", "full_text": full_text, "message_id": mid}
    _replay(req, replay)


def _replay(req: TurnRequest, held) -> None:
    """Answer the question the gate held back, and do not gate it again."""
    req.user_message = held.content
    req.lecture_num = held.lecture_num or req.lecture_num
    req.gate_trigger_id = held.id
    req.skip_gate = True
