"""Spaced-review integration (Engram integration, Phase 1).

Network-free, in-memory SQLite. Covers the FSRS state maintained through the
single-writer state module, the retrievability-based decay used by the envelope,
and the pure scheduling core of the review router (due queue, interleave,
session sizing, amnesty, nudge).
"""

from __future__ import annotations

import datetime

import pytest

from tutor.agent import state
from tutor.agent.spaced_review import Rating
from tutor.db.models import LearningGoal, Student, StudentProgress
from tutor.routers import review as review_router
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="a@example.edu", name="A"))
    s.add(LearningGoal(id="AST101-L03-g1", title="Seasons", description="", course="AST101", lecture_num=3))
    s.add(LearningGoal(id="AST101-L04-g2", title="Light", description="", course="AST101", lecture_num=4))
    s.add(LearningGoal(id="AST101-L04-g3", title="Telescopes", description="", course="AST101", lecture_num=4))
    s.add(LearningGoal(id="AST102-L01-g4", title="Parallax", description="", course="AST102", lecture_num=1))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# FSRS state is maintained through the single writer
# ---------------------------------------------------------------------------

def test_graded_event_populates_fsrs_state(db):
    state.record_graded_event(db, 1, "AST101-L03-g1", 1.0, source="exercise")
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="AST101-L03-g1").one()
    assert row.fsrs_stability is not None
    assert row.fsrs_difficulty is not None
    assert row.fsrs_reps == 1
    assert row.fsrs_lapses == 0
    assert row.fsrs_due is not None
    # A high success maps to EASY, so due date is comfortably in the future.
    assert row.fsrs_due > state._utcnow()


def test_repeated_success_grows_stability_and_reps(db):
    state.record_graded_event(db, 1, "AST101-L03-g1", 1.0)
    first = db.query(StudentProgress).filter_by(student_id=1, goal_id="AST101-L03-g1").one()
    s1 = first.fsrs_stability
    # Simulate elapsed time so the second review has a gap to grow from.
    first.last_evidence_at = state._utcnow() - datetime.timedelta(days=3)
    db.flush()
    state.record_graded_event(db, 1, "AST101-L03-g1", 1.0)
    second = db.query(StudentProgress).filter_by(student_id=1, goal_id="AST101-L03-g1").one()
    assert second.fsrs_reps == 2
    assert second.fsrs_stability > s1


def test_failed_review_counts_a_lapse(db):
    state.record_graded_event(db, 1, "AST101-L03-g1", 1.0)
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="AST101-L03-g1").one()
    row.last_evidence_at = state._utcnow() - datetime.timedelta(days=5)
    db.flush()
    state.record_review_rating(db, 1, "AST101-L03-g1", Rating.AGAIN)
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="AST101-L03-g1").one()
    assert row.fsrs_lapses == 1


def test_record_review_rating_advances_schedule(db):
    ms = state.record_review_rating(db, 1, "AST101-L03-g1", Rating.GOOD)
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="AST101-L03-g1").one()
    assert row.fsrs_reps == 1
    assert row.mastery_score == pytest.approx(ms)
    assert row.fsrs_due is not None


# ---------------------------------------------------------------------------
# FSRS-driven decay (envelope mastery scalar)
# ---------------------------------------------------------------------------

def test_fsrs_effective_mastery_endpoints():
    # No fade right after review (retrievability 1.0) -> full score.
    assert state.fsrs_effective_mastery(0.9, 10.0, 0.0) == pytest.approx(0.9)
    # Deep decay -> approaches the floor.
    faded = state.fsrs_effective_mastery(0.9, 1.0, 100.0)
    assert state.DECAY_FLOOR <= faded < 0.9
    # A score at/below the floor never decays.
    assert state.fsrs_effective_mastery(0.5, 1.0, 100.0) == 0.5
    # No stability -> unfaded score (legacy rows fall back to linear path).
    assert state.fsrs_effective_mastery(0.9, None, 100.0) == 0.9


def test_high_stability_decays_slower_than_low(db):
    slow = state.fsrs_effective_mastery(0.9, 50.0, 20.0)
    fast = state.fsrs_effective_mastery(0.9, 2.0, 20.0)
    assert slow > fast


# ---------------------------------------------------------------------------
# Router scheduling core (with the exam bank stubbed)
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_bank(monkeypatch):
    """Give every goal a two-question probe pool without touching the real bank."""
    def pool(goal_id):
        return [
            {"id": "q0", "kind": "concept", "question": f"{goal_id} q0", "rubric": "r", "starter": ""},
            {"id": "q1", "kind": "concept", "question": f"{goal_id} q1", "rubric": "r", "starter": ""},
        ]
    monkeypatch.setattr(review_router.mastery_mod, "_question_pool", pool)
    monkeypatch.setattr(review_router.mastery_mod, "_has_exam_questions", lambda gid: True)


def _set_due(db, goal_id, days):
    row = StudentProgress(
        student_id=1, goal_id=goal_id, status="mastered", mastery_score=0.9,
        fsrs_stability=10.0, fsrs_difficulty=5.0, fsrs_reps=1, fsrs_lapses=0,
        fsrs_due=state._utcnow() + datetime.timedelta(days=days),
    )
    db.add(row)
    db.flush()


def test_due_rows_only_returns_past_due_for_course(db, stub_bank):
    _set_due(db, "AST101-L03-g1", -1)   # due yesterday
    _set_due(db, "AST101-L04-g2", 5)    # not due
    _set_due(db, "AST102-L01-g4", -1)   # due but different course
    rows = review_router.due_rows(1, "AST101", db)
    assert [r.goal_id for r in rows] == ["AST101-L03-g1"]


def test_build_session_interleaves_and_caps(db, stub_bank):
    # Three due goals across two lectures (L04 has two).
    _set_due(db, "AST101-L04-g2", -2)
    _set_due(db, "AST101-L04-g3", -2)
    _set_due(db, "AST101-L03-g1", -1)
    session = review_router.build_session(1, "AST101", db, mode="standard")
    assert session["due_count"] == 3
    assert session["shown"] == 3
    lectures = [it["lecture_num"] for it in session["items"]]
    # Interleaving must not place both L04 items back-to-back at the front.
    assert lectures[0] != lectures[1]
    assert not session["amnesty"]
    assert session["minutes"] >= 1


def test_build_session_amnesty_when_backlog_exceeds_cap(db, stub_bank):
    for i, gid in enumerate(["AST101-L03-g1", "AST101-L04-g2", "AST101-L04-g3"]):
        _set_due(db, gid, -1 - i)
    session = review_router.build_session(1, "AST101", db, mode="sprint")
    # sprint cap is 7; with only 3 due there is no amnesty.
    assert session["amnesty"] is False
    # Shrink the cap to force amnesty deterministically.
    monkey_cap = 2
    review_router.SESSION_CAPS["tiny"] = monkey_cap
    session = review_router.build_session(1, "AST101", db, mode="tiny")
    assert session["shown"] == monkey_cap
    assert session["amnesty"] is True
    del review_router.SESSION_CAPS["tiny"]


def test_nudge_is_silent_when_nothing_due(db, stub_bank):
    n = review_router.nudge(1, "AST101", db)
    assert n == {"due_count": 0, "minutes": 0, "message": ""}


def test_nudge_reports_due_count(db, stub_bank):
    _set_due(db, "AST101-L03-g1", -1)
    _set_due(db, "AST101-L04-g2", -3)
    n = review_router.nudge(1, "AST101", db)
    assert n["due_count"] == 2
    assert "2 items due" in n["message"]


def test_lookup_probe_rederives_rubric(db, stub_bank):
    probe = review_router._lookup_probe("AST101-L03-g1", "q1")
    assert probe is not None and probe["rubric"] == "r"
    assert review_router._lookup_probe("AST101-L03-g1", "nope") is None
