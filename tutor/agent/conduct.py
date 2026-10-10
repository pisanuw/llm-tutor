"""Conduct handling for HOSTILE / ABUSIVE intents.

Locked decision (the instructor's, 2026-06-10): never auto-ban enrolled students. The
ladder is warn, then restrict to a brief boundary reply and notify the
instructor. "Notify the instructor" is a recorded conduct TurnEvent that the
admin page surfaces (Task 7); no message is ever sent off-platform from here.

Conduct events are recorded with kind 'conduct' so they do NOT count against
the daily chat-exchange cap.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from tutor.agent.policy import Intent

WARN_MESSAGE = (
    "Let's keep it respectful so I can actually help you. "
    "What is the CS question you are working on?"
)
RESTRICT_MESSAGE = (
    "I have noted this for your instructor. I am still here to help with the "
    "course, but let's reset the tone. What would you like to work on?"
)

_CONDUCT_INTENTS = (Intent.HOSTILE, Intent.ABUSIVE)


def _prior_conduct_today(db: DBSession, student_id: int) -> int:
    from tutor.db.models import TurnEvent

    from tutor import quotas

    start, _reset = quotas.window_bounds("day")   # the local day the caps use, not UTC
    return int(
        db.query(func.count())
        .select_from(TurnEvent)
        .filter(
            TurnEvent.student_id == student_id,
            TurnEvent.kind == "conduct",
            TurnEvent.ts >= start,
        )
        .scalar()
        or 0
    )


def conduct_response(db: DBSession, student_id: int, intent: Intent) -> Optional[str]:
    """If the intent is a conduct issue, record it and return a boundary reply.

    First offence today: warn. Repeat: restrict (still helps, flags for the
    instructor). Returns None for non-conduct intents so the turn proceeds
    normally.
    """
    if intent not in _CONDUCT_INTENTS:
        return None

    from tutor.agent import state

    repeat = _prior_conduct_today(db, student_id) > 0
    action = "restrict" if repeat else "warn"
    state.record_turn_event(
        db,
        student_id=student_id,
        kind="conduct",
        intent=intent.value,
        verdicts={"action": action},
    )
    if repeat:
        # The restriction now has teeth (S24): the envelope assembler reads
        # the flag and caps every later turn at H1 until the instructor
        # clears it (scripts/clear_restriction.py; admin button in Phase F).
        state.set_student_restricted(db, student_id, True)
    return RESTRICT_MESSAGE if repeat else WARN_MESSAGE
