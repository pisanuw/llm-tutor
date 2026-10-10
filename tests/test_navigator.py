"""Table-driven tests for tutor/agent/navigator.py (Session 14)."""

from __future__ import annotations

import datetime
import json

import pytest

from tutor.db.models import (
    GoalPrerequisite,
    LearningGoal,
    Student,
    StudentProgress,
    Track,
)
from tutor.agent.navigator import (
    default_track_id,
    frontier,
    get_student_track,
    next_goals,
)
from tutor.agent.state import set_student_path
from tutor.utctime import utcnow
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    session = make_session()
    yield session
    session.close()


def _make_student(db, email="test@example.com") -> Student:
    s = Student(email=email, name="Test")
    db.add(s)
    db.flush()
    return s


def _make_goal(db, gid: str, course: str = "AST101", lecture: int = 1) -> LearningGoal:
    g = LearningGoal(
        id=gid,
        title=gid,
        description="",
        course=course,
        lecture_num=lecture,
    )
    db.add(g)
    db.flush()
    return g


def _add_edge(db, from_id: str, to_id: str) -> GoalPrerequisite:
    e = GoalPrerequisite(from_goal_id=from_id, to_goal_id=to_id)
    db.add(e)
    db.flush()
    return e


def _master(db, student_id: int, goal_id: str, stale: bool = False) -> StudentProgress:
    """A mastered goal; ``stale`` makes it due for review on the one review
    clock, the FSRS due date (plan step 5.3, finding B20)."""
    last_evidence = due = None
    if stale:
        last_evidence = utcnow() - datetime.timedelta(days=30)
        due = utcnow() - datetime.timedelta(days=2)
    row = StudentProgress(
        student_id=student_id,
        goal_id=goal_id,
        status="mastered",
        evidence_type="test",
        last_evidence_at=last_evidence,
        fsrs_due=due,
    )
    db.add(row)
    db.flush()
    return row


def _make_track(db, track_id: str, course: str, goal_ids: list[str]) -> Track:
    t = Track(
        id=track_id,
        course=course,
        title=track_id,
        description="",
        goal_ids_json=json.dumps(goal_ids),
        graph_version=1,
    )
    db.add(t)
    db.flush()
    return t


# ---------------------------------------------------------------------------
# default_track_id
# ---------------------------------------------------------------------------

def test_default_track_id_ast101():
    assert default_track_id("AST101") == "AST101-lecture-order"


def test_default_track_id_ast102():
    assert default_track_id("AST102") == "AST102-lecture-order"


# ---------------------------------------------------------------------------
# frontier — empty graph (no prerequisites)
# ---------------------------------------------------------------------------

def test_frontier_all_goals_unlocked_when_no_prereqs(db):
    s = _make_student(db)
    _make_goal(db, "g1")
    _make_goal(db, "g2")
    result = frontier(s.id, "AST101", db)
    assert set(result) == {"g1", "g2"}


def test_frontier_excludes_mastered(db):
    s = _make_student(db)
    _make_goal(db, "g1")
    _make_goal(db, "g2")
    _master(db, s.id, "g1")
    result = frontier(s.id, "AST101", db)
    assert result == ["g2"]


def test_frontier_empty_when_all_mastered(db):
    s = _make_student(db)
    _make_goal(db, "g1")
    _master(db, s.id, "g1")
    result = frontier(s.id, "AST101", db)
    assert result == []


# ---------------------------------------------------------------------------
# frontier — with prerequisites
# ---------------------------------------------------------------------------

def test_frontier_locked_when_prereq_not_mastered(db):
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    _add_edge(db, "g1", "g2")
    result = frontier(s.id, "AST101", db)
    assert "g2" not in result
    assert "g1" in result


def test_frontier_unlocks_when_prereq_mastered(db):
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    _add_edge(db, "g1", "g2")
    _master(db, s.id, "g1")
    result = frontier(s.id, "AST101", db)
    assert "g2" in result
    assert "g1" not in result  # g1 is mastered, not in frontier


def test_frontier_chain_unlocks_incrementally(db):
    """g1 -> g2 -> g3: only g1 is initially in frontier."""
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    _make_goal(db, "g3", lecture=3)
    _add_edge(db, "g1", "g2")
    _add_edge(db, "g2", "g3")

    assert frontier(s.id, "AST101", db) == ["g1"]

    _master(db, s.id, "g1")
    assert frontier(s.id, "AST101", db) == ["g2"]

    _master(db, s.id, "g2")
    assert frontier(s.id, "AST101", db) == ["g3"]


def test_frontier_multiple_prereqs_all_required(db):
    """g3 requires both g1 and g2; not unlocked until both mastered."""
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=1)
    _make_goal(db, "g3", lecture=2)
    _add_edge(db, "g1", "g3")
    _add_edge(db, "g2", "g3")

    # Only one mastered — g3 still locked
    _master(db, s.id, "g1")
    f = frontier(s.id, "AST101", db)
    assert "g3" not in f

    # Both mastered — g3 unlocked
    _master(db, s.id, "g2")
    f = frontier(s.id, "AST101", db)
    assert "g3" in f


def test_frontier_course_isolation(db):
    """AST101 and AST102 goals don't cross-contaminate."""
    s = _make_student(db)
    _make_goal(db, "AST101-g1", course="AST101")
    _make_goal(db, "AST102-g1", course="AST102")
    assert frontier(s.id, "AST101", db) == ["AST101-g1"]
    assert frontier(s.id, "AST102", db) == ["AST102-g1"]


# ---------------------------------------------------------------------------
# next_goals — track ordering
# ---------------------------------------------------------------------------

def test_next_goals_respects_track_order(db):
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    _make_goal(db, "g3", lecture=3)
    # Reverse track order
    _make_track(db, "AST101-lecture-order", "AST101", ["g3", "g2", "g1"])

    result = next_goals(s.id, "AST101", db, track_id="AST101-lecture-order", n=3)
    assert result == ["g3", "g2", "g1"]


def test_next_goals_limits_to_n(db):
    s = _make_student(db)
    for i in range(4):
        _make_goal(db, f"g{i}", lecture=i + 1)
    _make_track(db, "AST101-lecture-order", "AST101", [f"g{i}" for i in range(4)])

    result = next_goals(s.id, "AST101", db, track_id="AST101-lecture-order", n=2)
    assert len(result) == 2


def test_next_goals_empty_when_all_mastered(db):
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _master(db, s.id, "g1")
    _make_track(db, "AST101-lecture-order", "AST101", ["g1"])

    result = next_goals(s.id, "AST101", db, track_id="AST101-lecture-order")
    assert result == []


def test_next_goals_review_due_injected_at_front(db):
    """Stale-mastered goals appear before frontier goals."""
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)  # stale-mastered
    _make_goal(db, "g2", lecture=2)  # frontier
    _master(db, s.id, "g1", stale=True)
    _make_track(db, "AST101-lecture-order", "AST101", ["g1", "g2"])

    result = next_goals(s.id, "AST101", db, track_id="AST101-lecture-order", n=3)
    # g1 is review-due and should appear at the front
    assert result[0] == "g1"
    assert "g2" in result


def test_a_mastered_goal_with_no_review_schedule_is_not_due(db):
    """B20: a mastered row with no evidence date used to be review-due
    forever, so it was always first in "What's next"."""
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    _master(db, s.id, "g1")                      # no FSRS schedule, no evidence date
    _make_track(db, "AST101-lecture-order", "AST101", ["g1", "g2"])

    assert next_goals(s.id, "AST101", db, track_id="AST101-lecture-order", n=3) == ["g2"]


def test_review_due_follows_the_fsrs_due_date(db):
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    row = _master(db, s.id, "g1")
    row.last_evidence_at = utcnow() - datetime.timedelta(days=60)
    row.fsrs_due = utcnow() + datetime.timedelta(days=5)   # long ago, but not due
    db.flush()
    _make_track(db, "AST101-lecture-order", "AST101", ["g1", "g2"])
    assert next_goals(s.id, "AST101", db, track_id="AST101-lecture-order", n=3) == ["g2"]
    row.fsrs_due = utcnow() - datetime.timedelta(minutes=1)
    db.flush()
    assert next_goals(s.id, "AST101", db, track_id="AST101-lecture-order", n=3) == ["g1", "g2"]


def test_next_goals_uses_saved_track_when_no_track_arg(db):
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    _make_track(db, "AST101-lecture-order", "AST101", ["g1", "g2"])
    _make_track(db, "AST101-big-ideas-first", "AST101", ["g2", "g1"])

    set_student_path(db, s.id, "AST101", "AST101-big-ideas-first")
    result = next_goals(s.id, "AST101", db, n=2)
    assert result == ["g2", "g1"]


def test_next_goals_falls_back_to_lecture_order_when_no_track_saved(db):
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    _make_track(db, "AST101-lecture-order", "AST101", ["g1", "g2"])

    result = next_goals(s.id, "AST101", db, n=2)
    assert result == ["g1", "g2"]


def test_next_goals_falls_back_gracefully_when_track_missing(db):
    """If the track row doesn't exist, falls back to lecture order."""
    s = _make_student(db)
    _make_goal(db, "g1", lecture=1)
    _make_goal(db, "g2", lecture=2)
    # No track rows seeded — next_goals falls back to _all_goal_ids order

    result = next_goals(s.id, "AST101", db, track_id="AST101-nonexistent", n=2)
    # Should return something, not crash
    assert len(result) <= 2


# ---------------------------------------------------------------------------
# get_student_track + set_student_path
# ---------------------------------------------------------------------------

def test_get_student_track_returns_none_when_not_set(db):
    s = _make_student(db)
    assert get_student_track(s.id, "AST101", db) is None


def test_set_student_path_creates_row(db):
    s = _make_student(db)
    _make_track(db, "AST101-lecture-order", "AST101", [])
    set_student_path(db, s.id, "AST101", "AST101-lecture-order")
    assert get_student_track(s.id, "AST101", db) == "AST101-lecture-order"


def test_set_student_path_updates_existing_row(db):
    s = _make_student(db)
    _make_track(db, "AST101-lecture-order", "AST101", [])
    _make_track(db, "AST101-big-ideas-first", "AST101", [])
    set_student_path(db, s.id, "AST101", "AST101-lecture-order")
    set_student_path(db, s.id, "AST101", "AST101-big-ideas-first")
    assert get_student_track(s.id, "AST101", db) == "AST101-big-ideas-first"


def test_set_student_path_independent_per_course(db):
    s = _make_student(db)
    _make_track(db, "AST101-lecture-order", "AST101", [])
    _make_track(db, "AST102-lecture-order", "AST102", [])
    set_student_path(db, s.id, "AST101", "AST101-lecture-order")
    set_student_path(db, s.id, "AST102", "AST102-lecture-order")
    assert get_student_track(s.id, "AST101", db) == "AST101-lecture-order"
    assert get_student_track(s.id, "AST102", db) == "AST102-lecture-order"
