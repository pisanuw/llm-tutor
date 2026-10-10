"""Curriculum navigator — pure, table-driven, zero LLM (Session 14).

Public API
----------
frontier(student_id, course, db) -> list[str]
    Goal IDs that are unlocked (every prerequisite mastered) and not yet
    mastered. Empty list when the student has mastered everything.

next_goals(student_id, course, db, track_id=None, n=3) -> list[str]
    frontier() filtered to those in the student's chosen track, in track
    order, with review-due goals prepended. Falls back to lecture order
    when the student has no track choice.

get_student_track(student_id, course, db) -> str | None
    The track_id the student is currently following, or None.

set_student_track(student_id, course, track_id, db) -> None
    Written through state.set_student_path.

default_track_id(course) -> str
    The lecture-order track for a course (always exists after startup seeding).

Design constraints:
  - No LLM calls.
  - No access to student prose or message content.
  - All inputs are trusted DB facts; tests use in-memory fixtures.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor.db.models import (
    GoalPrerequisite,
    LearningGoal,
    StudentPath,
    StudentProgress,
    Track,
)

log = logging.getLogger("tutor.navigator")

DEFAULT_TRACK_SUFFIX = "lecture-order"

# Decision 5, default: a prerequisite in another course is advisory. It stays
# on the map, but it neither keeps a goal off the frontier nor triggers a
# prerequisite exam (mastery.check_prerequisite_gate reads this too): a student
# who took the earlier course elsewhere has no records of it here, and a later
# course's first goals often hang off the earlier course's. Set True to make
# such edges bind; mastery in any course then counts (the mastered set is not
# filtered by course).
CROSS_COURSE_PREREQS_BIND = False


def default_track_id(course: str) -> str:
    return f"{course}-{DEFAULT_TRACK_SUFFIX}"


# ---------------------------------------------------------------------------
# Internal helpers — read-only queries
# ---------------------------------------------------------------------------

def _mastered_ids(student_id: int, db: DBSession) -> frozenset[str]:
    """Goal IDs the student has mastered, in either course.

    Not filtered by course (plan step 1.8, finding B3): the old filter made a
    prerequisite from another course count as never mastered.
    """
    rows = (
        db.query(StudentProgress.goal_id)
        .filter(
            StudentProgress.student_id == student_id,
            StudentProgress.status == "mastered",
        )
        .all()
    )
    return frozenset(r[0] for r in rows)


def _prereq_map(course: str, db: DBSession) -> dict[str, list[str]]:
    """Return {to_goal_id: [from_goal_id, ...]} for the edges that gate the
    course's goals. Edges from the other course are left out while they are
    advisory (CROSS_COURSE_PREREQS_BIND)."""
    edges = (
        db.query(GoalPrerequisite.from_goal_id, GoalPrerequisite.to_goal_id)
        .join(LearningGoal, LearningGoal.id == GoalPrerequisite.to_goal_id)
        .filter(LearningGoal.course == course)
        .all()
    )
    from_course: dict[str, str] = {}
    if not CROSS_COURSE_PREREQS_BIND and edges:
        from_ids = {f for f, _ in edges}
        from_course = {
            gid: c for gid, c in
            db.query(LearningGoal.id, LearningGoal.course).filter(LearningGoal.id.in_(from_ids)).all()
        }
    result: dict[str, list[str]] = {}
    for from_id, to_id in edges:
        if not CROSS_COURSE_PREREQS_BIND and from_course.get(from_id, course) != course:
            continue
        result.setdefault(to_id, []).append(from_id)
    return result


def _all_goal_ids(course: str, db: DBSession) -> list[str]:
    """All goal IDs for a course, in lecture order."""
    rows = (
        db.query(LearningGoal.id)
        .filter_by(course=course)
        .order_by(LearningGoal.lecture_num, LearningGoal.id)
        .all()
    )
    return [r[0] for r in rows]


def _review_due_ids(student_id: int, course: str, db: DBSession) -> list[str]:
    """Mastered goals that are due for review, on the one review clock
    (state.review_due_rows: the FSRS due date, plan step 5.3, finding B20).
    A mastered goal with no FSRS schedule is not due; it used to be due
    forever when its evidence date was empty."""
    from tutor.agent import state

    return [r.goal_id for r in state.review_due_rows(db, student_id, course)
            if r.status == "mastered"]


def _track_goal_order(track_id: str, db: DBSession) -> list[str] | None:
    """Ordered goal IDs from the track, or None if the track doesn't exist.
    A malformed list reads as empty (Track.goal_ids, the safe parser)."""
    track = db.get(Track, track_id)
    if track is None:
        return None
    return track.goal_ids()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def frontier(student_id: int, course: str, db: DBSession) -> list[str]:
    """Goal IDs that are unlocked (all prerequisites mastered) and not yet mastered.

    A goal with no prerequisites is always in the frontier unless mastered.
    """
    mastered = _mastered_ids(student_id, db)
    prereqs = _prereq_map(course, db)
    all_ids = _all_goal_ids(course, db)

    result = []
    for gid in all_ids:
        if gid in mastered:
            continue
        required = prereqs.get(gid, [])
        if all(r in mastered for r in required):
            result.append(gid)
    return result


def get_student_track(student_id: int, course: str, db: DBSession) -> Optional[str]:
    """The track_id the student is currently following, or None."""
    row = (
        db.query(StudentPath.track_id)
        .filter_by(student_id=student_id, course=course)
        .first()
    )
    return row[0] if row else None


def current_course(student_id: int, db: DBSession) -> Optional[str]:
    """The course the student is currently working in, or None.

    Defined as the course of the most recently updated StudentPath: picking a
    course on /welcome and switching course in the chat header both touch that
    row (state.ensure_student_path), so this is the student's last explicit
    course choice. No separate preference column (a first-login UX decision).
    None only for a student who has never chosen a course.
    """
    row = (
        db.query(StudentPath.course)
        .filter_by(student_id=student_id)
        .order_by(StudentPath.updated_at.desc(), StudentPath.id.desc())
        .first()
    )
    return row[0] if row else None


def next_goals(
    student_id: int,
    course: str,
    db: DBSession,
    track_id: Optional[str] = None,
    n: int = 3,
) -> list[str]:
    """Top n frontier goals in track order, with review-due goals prepended.

    If track_id is None, uses the student's saved track or falls back to
    lecture order.
    """
    if track_id is None:
        track_id = get_student_track(student_id, course, db) or default_track_id(course)

    frontier_ids = set(frontier(student_id, course, db))
    if not frontier_ids:
        return []

    track_order = _track_goal_order(track_id, db)
    if track_order is None:
        log.warning("navigator: track %s not found, falling back to lecture order", track_id)
        track_order = _all_goal_ids(course, db)

    # Ordered frontier: goals that are both in frontier and in the track, in track order
    ordered = [gid for gid in track_order if gid in frontier_ids]

    # Inject review-due mastered goals at the front (spaced repetition signal).
    # These are status='mastered', and frontier() excludes mastered goals, so a
    # review goal is never already in frontier_ids — no de-dup filter needed.
    review_prefix = _review_due_ids(student_id, course, db)

    combined = review_prefix + ordered
    return combined[:n]
