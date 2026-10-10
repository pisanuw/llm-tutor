"""Everything the policy decides before the actor runs: the classifier (with
the conduct short-circuit), the per-turn envelope, the prerequisite gate, and
the strategist's move."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.orm import Session as DBSession

from tutor import logging_config
from tutor.agent import conduct as conduct_mod
from tutor.agent import envelope as envelope_mod
from tutor.agent import llm_response
from tutor.agent import mastery as mastery_mod
from tutor.agent import pacing
from tutor.agent import policy
from tutor.agent import preclassify
from tutor.agent import state as state_mod
from tutor.agent import strategist as strategist_mod
from tutor.agent import trajectory as trajectory_mod
from tutor.agent import usage as usage_mod
from tutor.agent.models import SUPERVISOR
from tutor.agent.policy import Intent
from tutor.agent.turn_stages import persist
from tutor.agent.turn_stages.dbstep import db_step
from tutor.agent.turn_stages.request import TurnRequest
from tutor.auth import access

if TYPE_CHECKING:
    from tutor.agent.envelope import EnvelopeBundle
    from tutor.agent.preclassify import Classification


@dataclass
class StrategistFacts:
    """What the strategist reads from the ledger, fetched while the classifier
    runs (B2): neither depends on the other, so
    the turn no longer waits for one and then the other. ``error`` is the
    read's failure, handled where the strategist runs (no move this turn)."""

    trajectory: Any = None
    nav_facts: Any = None
    error: Optional[BaseException] = None


async def classify(
    db: DBSession, req: TurnRequest, supervisor_calls: list,
) -> tuple["Classification", StrategistFacts]:
    """Pre-classify the student message (intent + goals). One Haiku call; the
    message is framed as data (injection-resistant). The classifier never
    touches the session: it reports its usage and the turn writes the ledger
    rows (plan step 2.6, A19). The strategist's ledger reads run on the
    session while the classifier's model call is in flight; the session is
    still used by one step at a time."""
    loop = asyncio.get_running_loop()
    goal_catalog = await db_step(db, preclassify.load_goal_catalog, req.course, db)
    user_message = req.user_message
    started = time.monotonic()
    call = loop.run_in_executor(
        None, lambda: preclassify.classify(user_message, req.course, goal_catalog=goal_catalog,
                                           usage_sink=supervisor_calls)
    )
    facts = StrategistFacts()
    try:
        facts.trajectory, facts.nav_facts = await db_step(
            db, lambda: (trajectory_mod.compute_trajectory(db, req.sid),
                         pacing.navigator_facts(req.sid, req.course, db)),
        )
    except Exception as exc:   # the strategist is advisory: no move, logged there
        facts.error = exc
    classification = await call
    req.add_ms("classify", started)
    await db_step(db, llm_response.record_usage_calls, db, req.sid, supervisor_calls)
    return classification, facts


async def conduct_reply(
    db: DBSession, req: TurnRequest, classification: "Classification",
) -> Optional[tuple[str, int]]:
    """HOSTILE/ABUSIVE conduct short-circuits with a boundary reply (never an
    auto-ban). Returns the saved reply and its message id, or None to go on."""
    reply = await db_step(db, conduct_mod.conduct_response, db, req.sid, classification.intent)
    if not reply:
        return None
    return reply, await persist.save_assistant_message(db, req, reply)


def is_code_adjacent(classification: "Classification") -> bool:
    return classification.has_attempt or classification.intent in (
        Intent.ATTEMPT, Intent.SOLUTION_DEMAND,
    )


async def assemble_envelope(
    db: DBSession, req: TurnRequest, classification: "Classification", code_adjacent: bool,
) -> "EnvelopeBundle":
    """The per-turn envelope, assembled BEFORE any tool call (S14): scope from
    the classifier's goals, the lecture from the request.

    Retrieved slides carry the goal-graph lecture via
    curriculum/source-lecture-map.json (the indexed decks are the old
    week-numbered pptx files); they never drive the gate, only the
    post-retrieval scope refinement in the actor loop.
    """
    # Prerequisite exams interrupt attempts and solution demands only. A
    # conceptual QUESTION is answered (the envelope still caps the rung); the
    # gate fires when the student tries the exercise.
    gate_eligible = code_adjacent and not req.skip_gate
    return await db_step(
        db,
        envelope_mod.assemble_envelope,
        req.sid, req.course, db,
        lecture_num=req.lecture_num,
        scope_goal_ids=classification.goal_ids or None,
        intent=classification.intent,
        code_adjacent=code_adjacent,
        gate_eligible=gate_eligible,
    )


async def start_gate(db: DBSession, req: TurnRequest, bundle: "EnvelopeBundle") -> list[dict]:
    """The prerequisite gate (skipped for admins), driven by the envelope's
    gate goal before any retrieval: a gated turn becomes a mastery exam and
    spends no tool calls. Returns the turn's closing events, or [] to go on."""
    if access.is_admin(req.student.email) or bundle.gate_goal is None:
        return []
    attempt = await db_step(
        db,
        mastery_mod.start_exam,
        req.sid, bundle.gate_goal, db, trigger_message_id=req.gate_trigger_id,
    )
    if not attempt:
        return []
    questions = json.loads(attempt.questions_json)
    exam_text = mastery_mod.format_exam_intro(bundle.gate_goal, questions, course=req.course)
    gate_goal_id, gate_goal_title = bundle.gate_goal.id, bundle.gate_goal.title
    mid = await persist.save_assistant_message(db, req, exam_text)
    logging_config.activity("gate", level="minimal", turn_id=req.turn_id,
                            student_id=req.sid,
                            course=req.course, gate_goal=gate_goal_id, lecture_num=req.lecture_num,
                            content={"user_message": req.user_message})
    return [
        # Tells the chat to offer a "Not now" button under this reply.
        {"type": "exam", "goal": gate_goal_title, "not_now": mastery_mod.NOT_NOW},
        {"type": "token", "text": exam_text},
        {"type": "done", "full_text": exam_text, "message_id": mid},
    ]


async def consult_strategist(
    db: DBSession, req: TurnRequest, bundle: "EnvelopeBundle", classification: "Classification",
    facts: Optional[StrategistFacts] = None,
) -> None:
    """Supervisor 2: the strategist proposes one instructional move from the
    trajectory (trusted ledger state, never prose); the policy core disposes.
    merge_move can attach a move but never raise the rung ceiling or override
    a lockdown. Any failure means no move.

    Each strategist database step is its own transaction, rolled back on
    failure (plan steps 2.5 and 2.6, A13): a DB error inside the advisory
    strategist used to leave the session needing a rollback, which then
    failed the whole turn later on.
    """
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    try:
        if facts is None:
            trajectory, nav_facts = await db_step(
                db, lambda: (trajectory_mod.compute_trajectory(db, req.sid),
                             pacing.navigator_facts(req.sid, req.course, db)),
            )
        elif facts.error is not None:
            raise facts.error
        else:
            trajectory, nav_facts = facts.trajectory, facts.nav_facts
        choice = await loop.run_in_executor(
            None,
            lambda: strategist_mod.choose_move(
                bundle.envelope, trajectory, classification.intent,
                nav_facts=nav_facts,
            ),
        )
        bundle.envelope = policy.merge_move(bundle.envelope, choice.move, choice.reason)
        if choice.in_tokens or choice.out_tokens:
            await db_step(
                db,
                state_mod.record_turn_event,
                db,
                student_id=req.sid,
                kind="strategist",
                verdicts={"move": choice.move.value if choice.move else None},
                model=SUPERVISOR.id,
                in_tokens=choice.in_tokens,
                out_tokens=choice.out_tokens,
                cache_read_tokens=choice.cache_read_tokens,
                cost_usd=usage_mod.estimate_cost_usd(
                    SUPERVISOR.id, choice.in_tokens, choice.out_tokens, choice.cache_read_tokens,
                ),
                latency_ms=choice.latency_ms,
            )
    except Exception:
        # Advisory: the failed step was rolled back and the turn goes on with
        # no move, but the failure is logged (plan step 5.6).
        logging.getLogger("tutor").warning(
            "turn %s: the strategist failed; no move this turn", req.turn_id, exc_info=True)
    finally:
        req.add_ms("strategist", started)
