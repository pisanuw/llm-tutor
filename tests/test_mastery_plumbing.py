"""Session 17: per-goal mastery plumbing tests.

Covers:
- graded events land on the right goal (exercise_id attribution)
- seen flag is distinct from unlocked/mastered status
- mark_goals_seen_for_lecture sets seen without changing status
- seen display_status logic (map.py helper)
"""
from __future__ import annotations

import pytest

from tutor.agent import state
from tutor.agent.progress import mark_goals_seen_for_lecture
from tutor.db.models import LearningGoal, Student, StudentProgress
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    session = make_session()
    yield session
    session.close()


def _student(db, email="s@example.edu") -> Student:
    s = Student(email=email, name="Test")
    db.add(s)
    db.flush()
    return s


def _goal(db, gid: str, course: str = "AST101", lecture: int = 1) -> LearningGoal:
    g = LearningGoal(id=gid, title=gid, description="", course=course, lecture_num=lecture)
    db.add(g)
    db.flush()
    return g


# ---------------------------------------------------------------------------
# seen flag: mark_goal_seen
# ---------------------------------------------------------------------------

def test_mark_goal_seen_creates_row_if_absent(db):
    s = _student(db)
    _goal(db, "G1")
    result = state.mark_goal_seen(db, s.id, "G1")
    assert result is True
    row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G1").one()
    assert row.seen is True
    assert row.status == "locked"


def test_mark_goal_seen_does_not_change_status_unlocked(db):
    s = _student(db)
    _goal(db, "G1")
    # Unlock the goal first via a graded event
    state.record_graded_event(db, s.id, "G1", 0.7)
    row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G1").one()
    assert row.status == "unlocked"
    # Now mark seen
    state.mark_goal_seen(db, s.id, "G1")
    row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G1").one()
    assert row.seen is True
    assert row.status == "unlocked"  # must not regress


def test_mark_goal_seen_does_not_change_status_mastered(db):
    s = _student(db)
    _goal(db, "G1")
    for _ in range(4):
        state.record_graded_event(db, s.id, "G1", 1.0)
    row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G1").one()
    assert row.status == "mastered"
    state.mark_goal_seen(db, s.id, "G1")
    row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G1").one()
    assert row.seen is True
    assert row.status == "mastered"


def test_mark_goal_seen_idempotent(db):
    s = _student(db)
    _goal(db, "G1")
    state.mark_goal_seen(db, s.id, "G1")
    state.mark_goal_seen(db, s.id, "G1")
    rows = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G1").all()
    assert len(rows) == 1
    assert rows[0].seen is True


# ---------------------------------------------------------------------------
# mark_goals_seen_for_lecture
# ---------------------------------------------------------------------------

def test_mark_goals_seen_for_lecture_marks_all_goals(db):
    s = _student(db)
    for gid in ["G1", "G2", "G3"]:
        _goal(db, gid, lecture=3)
    _goal(db, "G_other", lecture=4)

    count = mark_goals_seen_for_lecture(s.id, db, "AST101", 3)
    assert count == 3

    rows = db.query(StudentProgress).filter_by(student_id=s.id).all()
    seen_gids = {r.goal_id for r in rows if r.seen}
    assert seen_gids == {"G1", "G2", "G3"}
    unseen_rows = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G_other").all()
    assert len(unseen_rows) == 0


def test_mark_goals_seen_for_lecture_skips_other_course(db):
    s = _student(db)
    _goal(db, "AST101-G1", course="AST101", lecture=3)
    _goal(db, "AST102-G1", course="AST102", lecture=3)

    count = mark_goals_seen_for_lecture(s.id, db, "AST101", 3)
    assert count == 1

    row_ast101 = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="AST101-G1").first()
    assert row_ast101 is not None and row_ast101.seen is True
    row_ast102 = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="AST102-G1").first()
    assert row_ast102 is None


# ---------------------------------------------------------------------------
# exercise_id attribution: graded event lands on correct goal
# ---------------------------------------------------------------------------

def test_record_graded_event_creates_progress_row(db):
    s = _student(db)
    _goal(db, "AST101-L01-phases")
    state.record_graded_event(db, s.id, "AST101-L01-phases", 0.8)
    row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="AST101-L01-phases").one()
    assert row.mastery_score > 0


def test_graded_event_on_specific_goal_does_not_affect_other_goal(db):
    s = _student(db)
    _goal(db, "G-target")
    _goal(db, "G-other")
    for _ in range(4):
        state.record_graded_event(db, s.id, "G-target", 1.0)
    target_row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G-target").one()
    assert target_row.status == "mastered"
    other_row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G-other").first()
    assert other_row is None


# ---------------------------------------------------------------------------
# seen vs unlocked: display_status semantics
# ---------------------------------------------------------------------------

def _display_status(progress: dict[str, str], seen_ids: set[str], goal_id: str) -> str:
    """Mirror the helper in map.py."""
    s = progress.get(goal_id, "locked")
    if s in ("mastered", "unlocked"):
        return s
    if goal_id in seen_ids:
        return "seen"
    return "locked"


def test_display_status_locked_no_seen():
    assert _display_status({}, set(), "G1") == "locked"


def test_display_status_seen_takes_priority_over_locked():
    assert _display_status({}, {"G1"}, "G1") == "seen"


def test_display_status_unlocked_beats_seen():
    assert _display_status({"G1": "unlocked"}, {"G1"}, "G1") == "unlocked"


def test_display_status_mastered_beats_seen():
    assert _display_status({"G1": "mastered"}, {"G1"}, "G1") == "mastered"
