"""Task 5 tests: graded mastery (windowed score + decay) and exam pooling."""

from __future__ import annotations

import datetime
import json
from unittest.mock import patch

import pytest

from tutor.agent import mastery, state
from tutor.db.models import (
    GradedEvent,
    LearningGoal,
    MasteryAttempt,
    Student,
    StudentProgress,
)
from tests.helpers import make_session, pool_bank
from tutor.utctime import utcnow


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="a@example.edu", name="A"))
    s.add(LearningGoal(id="G1", title="The electromagnetic spectrum", description="", course="AST101", lecture_num=4))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Graded mastery: windowed score + status sync
# ---------------------------------------------------------------------------

def test_mastery_score_is_mean_of_window(db):
    for v in [0.0, 1.0, 1.0]:
        state.record_graded_event(db, 1, "G1", v, source="exercise")
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one()
    assert row.mastery_score == pytest.approx(2 / 3)


def test_window_keeps_only_last_k(db):
    # 4 zeros then 6 ones: window of 6 -> all ones.
    for _ in range(4):
        state.record_graded_event(db, 1, "G1", 0.0)
    for _ in range(6):
        state.record_graded_event(db, 1, "G1", 1.0)
    score, total = state.windowed_score(db, 1, "G1")
    assert score == pytest.approx(1.0)
    assert total == 10


def test_unlock_at_threshold(db):
    state.record_graded_event(db, 1, "G1", 0.6)
    assert db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one().status == "unlocked"


def test_master_requires_high_score_and_enough_events(db):
    # Three perfect events: high score but < 4 events -> unlocked, not mastered.
    for _ in range(3):
        state.record_graded_event(db, 1, "G1", 1.0)
    assert db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one().status == "unlocked"
    # Fourth event crosses the >=4 events bar.
    state.record_graded_event(db, 1, "G1", 1.0)
    assert db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one().status == "mastered"


def test_status_is_upgrade_only(db):
    for _ in range(4):
        state.record_graded_event(db, 1, "G1", 1.0)
    assert db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one().status == "mastered"
    # A later bad streak must not downgrade a mastered goal.
    for _ in range(6):
        state.record_graded_event(db, 1, "G1", 0.0)
    assert db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one().status == "mastered"


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------

def test_effective_mastery_no_decay_when_fresh():
    now = datetime.datetime(2026, 6, 10)
    assert state.effective_mastery(1.0, now, now) == pytest.approx(1.0)


def test_effective_mastery_decays_to_floor_after_window():
    now = datetime.datetime(2026, 6, 10)
    old = now - datetime.timedelta(days=30)
    assert state.effective_mastery(1.0, old, now) == pytest.approx(state.DECAY_FLOOR)


def test_effective_mastery_linear_midpoint():
    now = datetime.datetime(2026, 6, 10)
    mid = now - datetime.timedelta(days=state.DECAY_DAYS / 2)
    # halfway from 1.0 toward floor 0.6 -> 0.8
    assert state.effective_mastery(1.0, mid, now) == pytest.approx(0.8)


def test_low_score_does_not_decay():
    now = datetime.datetime(2026, 6, 10)
    old = now - datetime.timedelta(days=100)
    assert state.effective_mastery(0.5, old, now) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Exam / exercise feed the window
# ---------------------------------------------------------------------------

def test_exam_pass_records_graded_event(db):
    a = state.start_exam_attempt(db, 1, "G1", "{}")
    state.record_exam_result(db, a, passed=True)
    ev = db.query(GradedEvent).filter_by(student_id=1, goal_id="G1", source="exam").one()
    assert ev.success == pytest.approx(1.0)


def test_exercise_outcome_records_fractional_event(db):
    state.record_exercise_outcome(
        db, student_id=1, exercise_id="e1", lecture_num=4,
        submission="x", score=2, passed=False, feedback="", course="AST101",
        goal_ids=["G1"],
    )
    ev = db.query(GradedEvent).filter_by(student_id=1, goal_id="G1", source="exercise").one()
    assert ev.success == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Exam pooling / sampling
# ---------------------------------------------------------------------------


def test_question_pool_normalizes_new_format():
    with patch("tutor.agent.mastery._load_exam_bank", return_value=pool_bank(n=8)):
        pool = mastery._question_pool("G1")
    assert len(pool) == 8
    assert sum(1 for q in pool if q["kind"] == "code") == 2


def test_question_pool_normalizes_legacy_format():
    legacy = {"G1": {
        "q1": {"question": "a", "rubric": "r"},
        "q2": {"question": "b", "rubric": "r"},
        "code": {"question": "c", "rubric": "r", "starter": "", "is_conceptual": False},
    }}
    with patch("tutor.agent.mastery._load_exam_bank", return_value=legacy):
        pool = mastery._question_pool("G1")
    assert len(pool) == 3
    assert [q["kind"] for q in pool] == ["concept", "concept", "code"]


def test_sample_three_distinct_with_code_slot():
    with patch("tutor.agent.mastery._load_exam_bank", return_value=pool_bank(n=8)):
        picks = mastery._sample_three(mastery._question_pool("G1"))
    assert picks is not None and len(picks) == 3
    texts = {p["question"] for p in picks}
    assert len(texts) == 3  # distinct
    assert picks[2]["kind"] == "code"  # code slot prefers a code question


def test_start_exam_stores_q1_q2_code(db):
    goal = db.get(LearningGoal, "G1")
    with patch("tutor.agent.mastery._load_exam_bank", return_value=pool_bank(n=8)):
        attempt = mastery.start_exam(1, goal, db)
    stored = json.loads(attempt.questions_json)
    assert {"q1", "q2", "code"} <= set(stored)


def test_start_exam_avoids_previous_failed_sample(db):
    goal = db.get(LearningGoal, "G1")
    # A prior failed attempt used these three texts.
    failed_texts = {"concept-0?", "concept-1?", "code-0?"}
    db.add(MasteryAttempt(
        student_id=1, goal_id="G1", status="failed",
        questions_json=json.dumps({
            "q1": {"question": "concept-0?"},
            "q2": {"question": "concept-1?"},
            "code": {"question": "code-0?"},
        }),
        attempts=1, started_at=utcnow(),
    ))
    db.commit()
    with patch("tutor.agent.mastery._load_exam_bank", return_value=pool_bank(n=8)):
        attempt = mastery.start_exam(1, goal, db)
    stored = json.loads(attempt.questions_json)
    new_texts = {stored["q1"]["question"], stored["q2"]["question"], stored["code"]["question"]}
    assert new_texts.isdisjoint(failed_texts)
