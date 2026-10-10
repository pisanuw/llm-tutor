"""Student progress tracking against the learning goals graph.

Public API
----------
mark_goals_seen_for_lecture(student_id, db, course, lecture_num)
    Mark a lecture's goals as seen (discussed in chat), status unchanged.

get_student_progress(student_id, db) -> dict[str, str]
    Returns {goal_id: status} for every goal the student has touched.
    Goals with no row are implicitly 'locked' and are omitted.

progress_summary(student_id, db, course) -> str
    One-paragraph string suitable for injection into the system prompt.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor.agent import state
from tutor.db.models import LearningGoal, StudentProgress
from tutor.courses import course_label


def mark_goals_seen_for_lecture(
    student_id: int,
    db: DBSession,
    course: str,
    lecture_num: int,
) -> int:
    """Mark all goals for a lecture as seen (discussed in chat).

    Sets seen=True on their StudentProgress rows without changing mastery
    status. "Unlocked" now means prerequisites mastered (navigator frontier),
    not chat exposure. Returns the count of rows created or updated.
    """
    goals = (
        db.query(LearningGoal)
        .filter_by(course=course, lecture_num=lecture_num)
        .all()
    )
    return sum(
        1 for goal in goals
        if state.mark_goal_seen(db, student_id, goal.id)
    )


def get_student_progress(student_id: int, db: DBSession) -> dict[str, str]:
    """Returns {goal_id: status} for all non-locked goals."""
    rows = db.query(StudentProgress).filter_by(student_id=student_id).all()
    return {r.goal_id: r.status for r in rows}


def progress_summary(student_id: int, db: DBSession, course: Optional[str] = None) -> str:
    """Short text block for the system prompt.

    E.g.:
        Student progress (AST 101): 5 goals unlocked, 3 mastered out of 12 total.
        Mastered topics: celestial-sphere, daily-motion, ...
    """
    all_goals_q = db.query(LearningGoal)
    if course:
        all_goals_q = all_goals_q.filter_by(course=course)
    total = all_goals_q.count()
    if total == 0:
        return ""

    rows = db.query(StudentProgress).filter_by(student_id=student_id).all()
    progress = {r.goal_id: r.status for r in rows}
    # Evidence tiers (pedagogy item 9): mastered but not yet confirmed by a retrieval
    # on a later day. The agent should treat these as "learned once" and expect
    # the review system to re-check them, not as settled.
    unconfirmed = {r.goal_id for r in rows if r.status == "mastered" and r.confirmed_at is None}

    # Filter to goals for the relevant course
    course_goal_ids: set[str] = {g.id for g in all_goals_q.all()}
    relevant = {gid: s for gid, s in progress.items() if gid in course_goal_ids}

    unlocked = sum(1 for s in relevant.values() if s == "unlocked")
    mastered = sum(1 for s in relevant.values() if s == "mastered")

    if unlocked + mastered == 0:
        return ""

    label = course_label(course) if course else "this course"
    lines = [
        f"Student progress ({label}): "
        f"{unlocked} goals unlocked, {mastered} mastered out of {total} total."
    ]
    if mastered:
        mastered_ids = [gid for gid, s in relevant.items() if s == "mastered"]
        # Show slugs only (strip course-lecture prefix) to keep prompt short
        slugs = [gid.split("-", 3)[-1] for gid in mastered_ids[:8]]
        lines.append(f"Mastered: {', '.join(slugs)}" + (" …" if mastered > 8 else "."))
        n_unconf = sum(1 for gid in mastered_ids if gid in unconfirmed)
        if n_unconf:
            lines.append(
                f"{n_unconf} of these passed once but await a spaced review before "
                f"they count as confirmed; treat them as learned, not settled."
            )

    # Spaced review (Engram P1) on the one review clock, the FSRS due date
    # (state.review_is_due, plan step 5.3, finding B20): the tutor can gently
    # point the student at /review when the backlog is real.
    now = state._utcnow()
    review_due = [r.goal_id for r in rows
                  if r.goal_id in course_goal_ids and state.review_is_due(r.fsrs_due, now)]
    if review_due:
        n = len(review_due)
        slugs = [gid.split("-", 3)[-1] for gid in review_due[:5]]
        lines.append(
            f"Spaced review: {n} item{'s' if n != 1 else ''} due for retrieval practice: "
            + ", ".join(slugs) + (" …" if n > 5 else ".")
        )

    return "\n".join(lines)
