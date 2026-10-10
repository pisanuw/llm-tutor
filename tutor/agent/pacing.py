"""Adaptive pacing: deterministic navigator facts for the strategist (Session 16).

Three pure helpers — no LLM, no student prose, no side effects:

navigator_facts(student_id, course, db) -> dict
    Top next goal and most-decayed mastered goal for the strategist's context.

warm_up_hint(student_id, course, db) -> str | None
    On the first message of a day: one line for the per_request_ctx block naming
    the most-decayed mastered goal. None if nothing is due or it isn't the first
    message today.

pacing_allowed(student_id, db, window_turns) -> bool
    True unless a ADVANCE, SUGGEST_REVIEW, or MODALITY_SWITCH move was already
    suggested in the last window_turns turns (no nagging cap).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session as DBSession
from tutor.utctime import utcnow

log = logging.getLogger("tutor.pacing")

_PACING_MOVES = {"ADVANCE", "SUGGEST_REVIEW", "MODALITY_SWITCH"}
_PACING_CAP_TURNS = 3  # block pacing moves if one occurred in this many recent turns


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _most_decayed_mastered(student_id: int, course: str, db: DBSession) -> Optional[tuple[str, str, float]]:
    """Return (goal_id, title, effective_score) of the most-decayed mastered goal.

    "Most-decayed" = smallest effective_mastery score among goals with
    status='mastered'. Returns None if no mastered goals exist.
    """
    from tutor.db.models import LearningGoal, StudentProgress
    from tutor.agent.state import effective_mastery

    rows = (
        db.query(StudentProgress, LearningGoal)
        .join(LearningGoal, LearningGoal.id == StudentProgress.goal_id)
        .filter(
            StudentProgress.student_id == student_id,
            StudentProgress.status == "mastered",
            LearningGoal.course == course,
        )
        .all()
    )
    if not rows:
        return None

    now = utcnow()
    best: Optional[tuple[str, str, float]] = None
    best_score = 1.0

    for prog, goal in rows:
        score = effective_mastery(
            prog.mastery_score,
            prog.last_evidence_at,
            now=now,
        )
        if score < best_score:
            best_score = score
            best = (goal.id, goal.title, score)

    return best


def _first_message_today(student_id: int, db: DBSession) -> bool:
    """True when the student has sent 0 messages in the current UTC day."""
    from tutor.db.models import Message

    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    count = (
        db.query(Message)
        .filter(
            Message.student_id == student_id,
            Message.role == "user",
            Message.created_at >= today_start,
        )
        .count()
    )
    return count == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def navigator_facts(student_id: int, course: str, db: DBSession) -> dict:
    """Return deterministic navigator facts for the strategist's prompt.

    Keys:
      top_goal_id      — first frontier goal in the student's track (str | None)
      top_goal_title   — title of that goal (str | "")
      decayed_goal_id  — most-decayed mastered goal (str | None)
      decayed_title    — its title (str | "")
      decayed_score    — its effective mastery score (float | None)
      pacing_ok        — True if a pacing move is permitted this turn (bool)
    """
    from tutor.agent.navigator import next_goals, get_student_track, default_track_id

    from tutor.db.guard import best_effort
    from tutor.db.models import LearningGoal

    def _top() -> tuple[Optional[str], str]:
        track_id = get_student_track(student_id, course, db) or default_track_id(course)
        top = next_goals(student_id, course, db, track_id=track_id, n=1)
        if not top:
            return None, ""
        g = db.get(LearningGoal, top[0])
        return top[0], (g.title if g else "")

    def _decayed() -> tuple[Optional[str], str, Optional[float]]:
        decayed = _most_decayed_mastered(student_id, course, db)
        return (decayed[0], decayed[1], decayed[2]) if decayed else (None, "", None)

    # Each value falls back to its own default on failure; the two computations
    # are independent, so a failure in one must not skip the other. Each runs
    # in a savepoint, so a failed query cannot poison the session (plan 5.6).
    top_goal_id, top_goal_title = best_effort(db, _top, (None, ""), "navigator facts: next goal")
    decayed_goal_id, decayed_title, decayed_score = best_effort(
        db, _decayed, (None, "", None), "navigator facts: most-decayed goal")

    return {
        "top_goal_id": top_goal_id,
        "top_goal_title": top_goal_title,
        "decayed_goal_id": decayed_goal_id,
        "decayed_title": decayed_title,
        "decayed_score": decayed_score,
        "pacing_ok": pacing_allowed(student_id, db),
    }


def warm_up_hint(student_id: int, course: str, db: DBSession) -> Optional[str]:
    """Return a one-line warm-up nudge if today is the student's first message.

    Returns None when:
    - The student has already sent a message today.
    - No mastered goal is stale (last_evidence_at > DECAY_DAYS/2 days ago).

    Trigger condition: effective score < original mastery_score, i.e. decay
    has started. Since effective_mastery floors at DECAY_FLOOR, we compare
    the effective score to the stored mastery_score rather than to the floor.
    Goals with null mastery_score (boolean-only mastery) are considered
    candidates if last_evidence_at is more than half the decay window ago.
    """
    from tutor.agent.state import DECAY_DAYS, effective_mastery as _eff

    half_window = DECAY_DAYS / 2.0

    # No blanket handler: the caller runs this in a savepoint (plan step
    # 5.6), which logs a failure and carries on without the hint.
    if not _first_message_today(student_id, db):
        return None

    from tutor.db.models import LearningGoal, StudentProgress

    rows = (
        db.query(StudentProgress, LearningGoal)
        .join(LearningGoal, LearningGoal.id == StudentProgress.goal_id)
        .filter(
            StudentProgress.student_id == student_id,
            StudentProgress.status == "mastered",
            LearningGoal.course == course,
        )
        .all()
    )
    if not rows:
        return None

    now = utcnow()
    best_title: Optional[str] = None
    best_eff: float = 1.0
    best_orig: float = 1.0

    for prog, goal in rows:
        eff = _eff(prog.mastery_score, prog.last_evidence_at, now=now)
        orig = prog.mastery_score if prog.mastery_score is not None else 1.0

        # Days since last evidence
        if prog.last_evidence_at is not None:
            days_stale = (now - prog.last_evidence_at).total_seconds() / 86400.0
        else:
            days_stale = float("inf")

        # Trigger: decay has started (past half-window) AND effective < original
        if days_stale >= half_window and eff < orig:
            if best_title is None or eff < best_eff:
                best_eff = eff
                best_orig = orig
                best_title = goal.title

    if best_title is None:
        return None

    return (
        f"\nWARM-UP SUGGESTION: '{best_title}' shows signs of forgetting "
        f"(effective mastery {best_eff:.0%}, was {best_orig:.0%}). "
        f"Open with a 2-minute review — one elicitation question, then move on."
    )


def pacing_allowed(student_id: int, db: DBSession, window_turns: int = _PACING_CAP_TURNS) -> bool:
    """True unless a pacing move was already suggested in the last window_turns turns.

    Reads TurnEvent rows of kind='strategist' to check for recent ADVANCE,
    SUGGEST_REVIEW, or MODALITY_SWITCH verdicts. Any failure defaults to True
    so the strategist is never blocked by a telemetry read failure.
    """
    from tutor.db.guard import best_effort
    from tutor.db.models import TurnEvent

    rows = best_effort(db, lambda: (
        db.query(TurnEvent)
        .filter_by(student_id=student_id, kind="strategist")
        .order_by(TurnEvent.ts.desc())
        .limit(window_turns)
        .all()
    ), [], "the pacing check")
    for row in rows:
        if not row.verdicts_json:
            continue
        try:
            v = json.loads(row.verdicts_json)
            move = (v.get("move") or "").upper()
            if move in _PACING_MOVES:
                return False
        except (ValueError, TypeError, AttributeError):
            continue
    return True
