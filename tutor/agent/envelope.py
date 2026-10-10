"""Envelope assembler: build the policy core's inputs from trusted state.

This is the impure boundary between the database and the pure
:mod:`tutor.agent.policy`. It reads trusted learner state (progress, pending
exams), trusted content metadata (the lecture in view), and instructor toggles
(the settings table), packages them into the policy dataclasses, and calls
:func:`compute_envelope`. It never reads the student's message: intent comes in
as an already-classified enum (the Task 4 pre-classifier), defaulting to
QUESTION until that lands.

Returned bundle also carries the gate goal object (if any) so the caller can
start a mastery exam, and a goal-id to title map for rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor.agent import mastery as mastery_mod
from tutor.agent import state
from tutor.agent.policy import (
    DeterministicFacts,
    Envelope,
    GoalState,
    Intent,
    StudentState,
    Toggles,
    compute_envelope,
)
from tutor.db.models import LearningGoal, MasteryAttempt, Misconception, Setting, StudentProgress

# Interim status -> mastery mapping. Task 5 replaces this with a real
# windowed mastery_score float stored on StudentProgress.
_STATUS_MASTERY: dict[str, float] = {
    "mastered": 0.9,
    "unlocked": 0.5,
    "locked": 0.0,
}

EXPLAIN_MODE_SETTING = "explain_mode"   # a course id | "all" | "" (off)


@dataclass
class EnvelopeBundle:
    envelope: Envelope
    goal_titles: dict[str, str]
    gate_goal: Optional[LearningGoal]
    # A one-line, content-only hint for the actor naming an in-scope goal the
    # student holds an unresolved misconception about (Engram P3, populated from
    # the high-confidence-wrong signal of P2). Never changes the rung ceiling
    # (does not touch P2/P3); empty when there is nothing to flag.
    calibration_note: str = ""


def _misconception_note(
    student_id: int,
    scope_goal_ids: set[str],
    titles: dict[str, str],
    db: DBSession,
) -> str:
    """Build the misconception contract note for the in-scope goals (Engram P3).

    Reads unresolved :class:`Misconception` rows for goals in scope and returns a
    single content hint naming the specific wrong model(s), or "" when there is
    nothing to flag. Pure read; no writes, no LLM.
    """
    if not scope_goal_ids:
        return ""
    rows = (
        db.query(Misconception)
        .filter(
            Misconception.student_id == student_id,
            Misconception.goal_id.in_(scope_goal_ids),
            Misconception.resolved_at.is_(None),
        )
        .order_by(Misconception.created_at.desc())
        .limit(2)
        .all()
    )
    # Cohort library (pedagogy item 12): approved goal-level misconceptions for the
    # goals in scope. Candidates never reach the prompt.
    from tutor.agent.misconception_mining import approved_for_goals
    library = approved_for_goals(db, scope_goal_ids, limit=4)
    lib_note = ""
    if library:
        lib_parts = [
            f"{titles.get(gid, gid)} — {desc}" for gid, desc in library
        ]
        lib_note = (
            " COMMON MISCONCEPTIONS for these goals (instructor-approved library): "
            + "; ".join(lib_parts)
            + ". Listen for these specifically; if the student's reasoning matches one, "
            "surface and test that idea rather than assuming understanding."
        )
    if not rows:
        return lib_note.strip()
    parts = []
    for m in rows:
        title = titles.get(m.goal_id, m.goal_id)
        desc = (m.description or "").strip()
        parts.append(f"{title} — {desc}" if desc else title)
    joined = "; ".join(parts)
    return (
        f"MISCONCEPTION on record ({joined}). The student earlier answered this "
        "confidently but wrong. If it comes up, gently invite them to re-derive or "
        "double-check that idea rather than re-explaining it as new; do not treat "
        "it as a fresh gap."
    ) + lib_note


def _explain_mode_on(db: DBSession, course: str) -> bool:
    """Read the instructor explain-mode toggle from the settings table.

    The setting value is a course id, "all", or empty/off.
    The admin UI for this lands in Task 7; here we only read it.
    """
    row = db.get(Setting, EXPLAIN_MODE_SETTING)
    if not row or not row.value:
        return False
    val = row.value.strip().lower()
    return val == "all" or val == str(course).lower()


def assemble_envelope(
    student_id: int,
    course: str,
    db: DBSession,
    lecture_num: Optional[int] = None,
    scope_goal_ids: Optional[list[str]] = None,
    intent: Intent = Intent.QUESTION,
    code_adjacent: bool = False,
    on_live_exercise: bool = False,
    gate_eligible: bool = True,
) -> EnvelopeBundle:
    """Assemble trusted inputs and compute the per-turn envelope.

    ``gate_eligible`` decides whether this turn may become a prerequisite
    exam at all. The caller (turn.py) passes False for plain questions, for
    the replay of a question that a just-passed exam swallowed, and right
    after a deferral, so the gate only interrupts attempts and solution
    demands (observed live 2026-09-02: a first-week "what does * mean?"
    turned into two prerequisite exams and never got an answer).
    """
    # Scope: classified goals if provided (Task 4), else the lecture's goals as
    # the slide-metadata fallback.
    if scope_goal_ids:
        scope = list(scope_goal_ids)
    elif lecture_num is not None:
        scope = [
            g.id
            for g in db.query(LearningGoal)
            .filter_by(course=course, lecture_num=lecture_num)
            .all()
        ]
    else:
        scope = []

    # Prerequisite gate from trusted progress (None if disabled / all clear).
    gate_goal: Optional[LearningGoal] = None
    if gate_eligible and lecture_num is not None:
        gate_goal = mastery_mod.check_prerequisite_gate(student_id, course, lecture_num, db)

    # Goals we need state + titles for: scope plus any gate goal.
    needed_ids = set(scope)
    if gate_goal is not None:
        needed_ids.add(gate_goal.id)

    # Trusted progress rows (status + graded mastery_score).
    rows_by_goal = {
        r.goal_id: r
        for r in db.query(StudentProgress).filter_by(student_id=student_id).all()
    }

    # Goals under an active (pending) exam.
    pending_exam_ids = {
        a.goal_id
        for a in db.query(MasteryAttempt)
        .filter_by(student_id=student_id, status="pending")
        .all()
    }

    now = state._utcnow()

    def _goal_state(gid: str) -> GoalState:
        row = rows_by_goal.get(gid)
        if row is None:
            mastery, status = 0.0, "locked"
        elif row.mastery_score is not None:
            # Decayed graded score is the authoritative signal (Task 5), on
            # the same clock as every other reader (state.current_mastery:
            # FSRS predicted forgetting, or the flat decay for pre-FSRS rows).
            mastery = state.current_mastery(row, now)
            status = row.status
        else:
            mastery, status = _STATUS_MASTERY.get(row.status, 0.0), row.status
        return GoalState(
            goal_id=gid,
            mastery=mastery,
            status=status,
            exam_active=gid in pending_exam_ids,
        )

    goal_states = tuple(_goal_state(gid) for gid in needed_ids)

    # Conduct restriction (S24): set by the conduct ladder, cleared only by
    # the instructor. Trusted state; caps the ceiling at H1 in the core.
    from tutor.db.models import Student
    student_row = db.get(Student, student_id)
    restricted = bool(student_row.restricted) if student_row is not None else False

    facts = DeterministicFacts(
        scope_goal_ids=tuple(scope),
        gate_goal_id=gate_goal.id if gate_goal else None,
        code_adjacent=code_adjacent,
        on_live_exercise=on_live_exercise,
        restricted=restricted,
    )
    toggles = Toggles(
        explain_mode=_explain_mode_on(db, course),
        allow_exercise=True,
    )

    envelope = compute_envelope(StudentState(goal_states), intent, facts, toggles)

    # Titles for rendering (scope + gate).
    titles: dict[str, str] = {}
    if needed_ids:
        for g in db.query(LearningGoal).filter(LearningGoal.id.in_(needed_ids)).all():
            titles[g.id] = g.title

    # Misconception contract note (Engram P3): content hint only.
    calibration_note = _misconception_note(student_id, set(scope), titles, db)

    return EnvelopeBundle(
        envelope=envelope,
        goal_titles=titles,
        gate_goal=gate_goal,
        calibration_note=calibration_note,
    )
