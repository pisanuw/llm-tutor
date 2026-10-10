"""Learning metrics tests (pedagogy item 13): mastery velocity,
14-day retention gating, and calibration error, over seeded sqlite rows."""

from __future__ import annotations

import datetime

import pytest

from tutor.db.models import (
    GradedEvent, LearningGoal, Message, ScorePrediction, Student, StudentProgress,
)
from tutor.learning_metrics import (
    RETENTION_DAYS, compute_learning_metrics, render_learning_markdown,
)
from tests.helpers import make_session

NOW = datetime.datetime(2026, 9, 1, 12, 0, 0)
SINCE = NOW - datetime.timedelta(hours=24)


@pytest.fixture()
def db():
    s = make_session()
    yield s
    s.close()


def _seed_students(db, n=2):
    students = [Student(email=f"s{i}@example.com", name=f"S{i}") for i in range(n)]
    db.add_all(students)
    db.flush()
    db.add(LearningGoal(id="g1", title="G1", description="", course="AST101", lecture_num=1))
    db.add(LearningGoal(id="g2", title="G2", description="", course="AST101", lecture_num=1))
    db.flush()
    for st in students:  # both active in the window
        db.add(Message(student_id=st.id, role="user", content="hi", course="AST101",
                       created_at=NOW - datetime.timedelta(hours=1)))
    db.commit()
    return students


def _mastered(db, student_id, goal_id, when, confirmed=None):
    db.add(StudentProgress(student_id=student_id, goal_id=goal_id, status="mastered",
                           evidence_type="graded", updated_at=when,
                           mastered_at=when, confirmed_at=confirmed))


def test_empty_window_renders_placeholder(db):
    m = compute_learning_metrics(db, SINCE, NOW)
    assert m.active_students == 0 and m.goals_mastered == 0 and m.predictions == 0
    assert "No learning signals" in render_learning_markdown(m)


def test_mastery_velocity_uses_confirmed_tier(db):
    s0, s1 = _seed_students(db)
    recent = NOW - datetime.timedelta(hours=2)
    # Two confirmations in the window, one provisional-only, one old confirmation.
    _mastered(db, s0.id, "g1", NOW - datetime.timedelta(days=5), confirmed=recent)
    _mastered(db, s1.id, "g1", NOW - datetime.timedelta(days=4), confirmed=recent)
    _mastered(db, s1.id, "g2", recent)                                        # quiz tier only
    _mastered(db, s0.id, "g2", NOW - datetime.timedelta(days=9),
              confirmed=NOW - datetime.timedelta(days=3))                     # outside window
    db.commit()
    m = compute_learning_metrics(db, SINCE, NOW)
    assert m.active_students == 2
    assert m.goals_confirmed == 2 and m.cumulative_confirmed == 3
    assert m.goals_mastered == 1            # provisional reached mastered in window
    assert m.cumulative_mastered == 4
    # 2 confirmations / 2 students over a 1-day window => 7 per student-week
    assert m.confirmed_per_student_week == pytest.approx(7.0)
    md = render_learning_markdown(m)
    assert "confirmed by spaced retrieval):** 2" in md
    assert "1 awaiting all-time" in md


def test_retention_counts_only_reviews_14_days_after_mastery(db):
    (s0,) = _seed_students(db, n=1)
    early = NOW - datetime.timedelta(days=20)
    _mastered(db, s0.id, "g1", early)                                   # >=14d ago
    _mastered(db, s0.id, "g2", NOW - datetime.timedelta(days=3))        # too recent
    db.add(GradedEvent(student_id=s0.id, goal_id="g1", success=1.0, source="review",
                       ts=NOW - datetime.timedelta(hours=1)))            # counts: retained
    db.add(GradedEvent(student_id=s0.id, goal_id="g1", success=0.0, source="review",
                       ts=NOW - datetime.timedelta(hours=2)))            # counts: forgot
    db.add(GradedEvent(student_id=s0.id, goal_id="g2", success=1.0, source="review",
                       ts=NOW - datetime.timedelta(hours=1)))            # excluded (<14d)
    db.add(GradedEvent(student_id=s0.id, goal_id="g1", success=1.0, source="exam",
                       ts=NOW - datetime.timedelta(hours=1)))            # excluded (not review)
    db.commit()
    m = compute_learning_metrics(db, SINCE, NOW)
    assert m.retention_reviews == 2
    assert m.retention_rate == pytest.approx(0.5)
    assert m.retention_mean_success == pytest.approx(0.5)
    md = render_learning_markdown(m)
    assert f"{RETENTION_DAYS}-day retention:** 50% retained" in md


def test_retention_note_when_reviews_are_all_too_early(db):
    (s0,) = _seed_students(db, n=1)
    _mastered(db, s0.id, "g1", NOW - datetime.timedelta(days=2))
    db.add(GradedEvent(student_id=s0.id, goal_id="g1", success=1.0, source="review",
                       ts=NOW - datetime.timedelta(hours=1)))
    db.commit()
    m = compute_learning_metrics(db, SINCE, NOW)
    assert m.retention_reviews == 0 and m.retention_rate is None
    assert any("none yet" in n for n in m.notes)


def test_calibration_metrics(db):
    (s0,) = _seed_students(db, n=1)
    for pred, actual in [(3, 2), (4, 4), (1, 3), (2, 2)]:
        db.add(ScorePrediction(student_id=s0.id, course="AST101", predicted=pred, actual=actual,
                               correct=actual == 4, ts=NOW - datetime.timedelta(hours=1)))
    db.commit()
    m = compute_learning_metrics(db, SINCE, NOW)
    assert m.predictions == 4
    assert m.calibration_mae == pytest.approx((1 + 0 + 2 + 0) / 4)
    assert m.calibration_bias == pytest.approx((1 + 0 - 2 + 0) / 4)
    assert m.calibration_exact_rate == pytest.approx(0.5)
    md = render_learning_markdown(m)
    assert "MAE 0.75" in md and "n=4" in md


def test_digest_leads_with_learning_section():
    from tutor.reporting import render_digest_markdown

    md = render_digest_markdown([], "2026-09-01", learning_md="## Learning signals\n\n- x\n")
    assert md.index("## Learning signals") < md.index("_No student activity")
