"""Tests for learning-goal unlock/mastery progress tracking.

Covers:
- state.unlock_goal         (a lecture at a time, through unlock_lecture below)
- state.mark_goal_mastered  (a lecture at a time, through master_lecture below)
- get_student_progress      (progress.py)
- progress_summary          (progress.py)
- check_prerequisite_gate   (mastery.py)
- start_exam                (mastery.py)
- format_exam_intro         (mastery.py)

All tests use an in-memory SQLite DB seeded with a minimal goal graph:
  L01: 4 goals (daily-motion, pole-star, angular-measure, celestial-sphere)
  L02: 2 goals (explain-tides, eclipse-conditions)
  Edge: L01-celestial-sphere -> L02-explain-tides
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tutor.db.models import (
    GoalPrerequisite,
    LearningGoal,
    MasteryAttempt,
    Student,
    StudentProgress,
)
from tutor.agent import state
from tutor.agent.progress import get_student_progress, progress_summary
from tutor.agent.mastery import check_prerequisite_gate, format_exam_intro, start_exam
from tutor.utctime import utcnow
from tests.helpers import make_session


# The lecture-wide helpers progress.unlock_goals_for_lecture and
# master_goals_for_lecture had no caller outside these tests; plan step 5.7
# (finding B27) deleted them. The tests keep their coverage of the single
# writer's two transitions through these local equivalents.
def unlock_lecture(student_id, db, course, lecture_num, evidence_type="chat") -> int:
    goals = db.query(LearningGoal).filter_by(course=course, lecture_num=lecture_num).all()
    return sum(1 for g in goals if state.unlock_goal(db, student_id, g.id, evidence_type))


def master_lecture(student_id, db, course, lecture_num, evidence_type="exercise") -> int:
    goals = db.query(LearningGoal).filter_by(course=course, lecture_num=lecture_num).all()
    return sum(1 for g in goals if state.mark_goal_mastered(db, student_id, g.id, evidence_type))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def student(db):
    s = Student(email="tester@example.edu", name="Test Student")
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def goals(db):
    """Two lectures with a cross-lecture prerequisite edge."""
    l01 = [
        LearningGoal(id="AST101-L01-daily-motion",     title="Explain why stars rise and set",       description="d", course="AST101", lecture_num=1),
        LearningGoal(id="AST101-L01-pole-star",        title="Find the pole star",                   description="d", course="AST101", lecture_num=1),
        LearningGoal(id="AST101-L01-angular-measure",  title="Estimate angles on the sky",           description="d", course="AST101", lecture_num=1),
        LearningGoal(id="AST101-L01-celestial-sphere", title="Describe the celestial sphere model",  description="d", course="AST101", lecture_num=1),
    ]
    l02 = [
        LearningGoal(id="AST101-L02-explain-tides",      title="Explain why there are two tides a day", description="d", course="AST101", lecture_num=2),
        LearningGoal(id="AST101-L02-eclipse-conditions", title="State the conditions for an eclipse",   description="d", course="AST101", lecture_num=2),
    ]
    for g in l01 + l02:
        db.add(g)
    db.flush()
    # L01-celestial-sphere must be mastered before L02-explain-tides
    db.add(GoalPrerequisite(
        from_goal_id="AST101-L01-celestial-sphere",
        to_goal_id="AST101-L02-explain-tides",
    ))
    db.flush()
    return {"l01": l01, "l02": l02}


# Minimal exam bank used by gate-check tests (no real JSON file needed)
_FAKE_EXAM_BANK = {
    "AST101-L01-celestial-sphere": {
        "q1":   {"question": "What is the celestial equator?",    "rubric": "Earth's equator projected onto the sky"},
        "q2":   {"question": "Name two celestial coordinates.",   "rubric": "right ascension, declination"},
        "code": {"question": "Write a loop printing 1-5.",        "starter": "int main() { }",
                 "rubric": "for or while printing 1 to 5", "is_conceptual": False},
    }
}


# ---------------------------------------------------------------------------
# state.unlock_goal, a lecture at a time
# ---------------------------------------------------------------------------

class TestUnlockGoal:
    def test_unlocks_all_goals_in_lecture(self, db, student, goals):
        count = unlock_lecture(student.id, db, "AST101", 1)
        assert count == 4
        progress = get_student_progress(student.id, db)
        assert set(progress.keys()) == {g.id for g in goals["l01"]}
        assert all(s == "unlocked" for s in progress.values())

    def test_does_not_downgrade_mastered_goal(self, db, student, goals):
        db.add(StudentProgress(
            student_id=student.id, goal_id="AST101-L01-daily-motion",
            status="mastered", updated_at=utcnow(),
        ))
        db.flush()

        unlock_lecture(student.id, db, "AST101", 1)

        progress = get_student_progress(student.id, db)
        assert progress["AST101-L01-daily-motion"] == "mastered"

    def test_upgrades_locked_to_unlocked(self, db, student, goals):
        db.add(StudentProgress(
            student_id=student.id, goal_id="AST101-L01-daily-motion",
            status="locked", updated_at=utcnow(),
        ))
        db.flush()

        unlock_lecture(student.id, db, "AST101", 1)

        assert get_student_progress(student.id, db)["AST101-L01-daily-motion"] == "unlocked"

    def test_idempotent_on_second_call(self, db, student, goals):
        unlock_lecture(student.id, db, "AST101", 1)
        count2 = unlock_lecture(student.id, db, "AST101", 1)
        assert count2 == 0

    def test_empty_lecture_number_returns_zero(self, db, student, goals):
        assert unlock_lecture(student.id, db, "AST101", 99) == 0

    def test_only_affects_requested_lecture(self, db, student, goals):
        unlock_lecture(student.id, db, "AST101", 1)
        progress = get_student_progress(student.id, db)
        l02_ids = {g.id for g in goals["l02"]}
        assert not l02_ids.intersection(progress.keys())


# ---------------------------------------------------------------------------
# state.mark_goal_mastered, a lecture at a time
# ---------------------------------------------------------------------------

class TestMarkGoalMastered:
    def test_masters_all_goals_in_lecture(self, db, student, goals):
        count = master_lecture(student.id, db, "AST101", 1)
        assert count == 4
        progress = get_student_progress(student.id, db)
        assert all(s == "mastered" for s in progress.values())

    def test_upgrades_unlocked_to_mastered(self, db, student, goals):
        unlock_lecture(student.id, db, "AST101", 1)
        count = master_lecture(student.id, db, "AST101", 1)
        assert count == 4
        assert all(s == "mastered" for s in get_student_progress(student.id, db).values())

    def test_idempotent_when_already_mastered(self, db, student, goals):
        master_lecture(student.id, db, "AST101", 1)
        count2 = master_lecture(student.id, db, "AST101", 1)
        assert count2 == 0

    def test_records_evidence_type(self, db, student, goals):
        master_lecture(student.id, db, "AST101", 1, evidence_type="exercise")
        rows = db.query(StudentProgress).filter_by(student_id=student.id).all()
        assert all(r.evidence_type == "exercise" for r in rows)


# ---------------------------------------------------------------------------
# get_student_progress
# ---------------------------------------------------------------------------

class TestGetStudentProgress:
    def test_empty_dict_for_new_student(self, db, student):
        assert get_student_progress(student.id, db) == {}

    def test_returns_correct_statuses(self, db, student, goals):
        unlock_lecture(student.id, db, "AST101", 1)
        master_lecture(student.id, db, "AST101", 2)
        progress = get_student_progress(student.id, db)
        assert all(progress[g.id] == "unlocked" for g in goals["l01"])
        assert all(progress[g.id] == "mastered" for g in goals["l02"])

    def test_isolated_per_student(self, db, goals):
        s1 = Student(email="s1@example.edu", name="S1")
        s2 = Student(email="s2@example.edu", name="S2")
        db.add_all([s1, s2])
        db.flush()

        unlock_lecture(s1.id, db, "AST101", 1)

        assert get_student_progress(s1.id, db) != {}
        assert get_student_progress(s2.id, db) == {}


# ---------------------------------------------------------------------------
# progress_summary
# ---------------------------------------------------------------------------

class TestProgressSummary:
    def test_empty_string_for_untouched_student(self, db, student, goals):
        assert progress_summary(student.id, db, "AST101") == ""

    def test_contains_course_label_after_unlock(self, db, student, goals):
        unlock_lecture(student.id, db, "AST101", 1)
        summary = progress_summary(student.id, db, "AST101")
        assert "AST 101" in summary
        assert "4" in summary
        assert "unlocked" in summary

    def test_shows_mastered_topics(self, db, student, goals):
        master_lecture(student.id, db, "AST101", 1)
        summary = progress_summary(student.id, db, "AST101")
        assert "mastered" in summary
        assert "Mastered:" in summary

    def test_filters_to_requested_course(self, db, student, goals):
        unlock_lecture(student.id, db, "AST101", 1)
        summary_ast102 = progress_summary(student.id, db, "AST102")
        assert summary_ast102 == ""


# ---------------------------------------------------------------------------
# check_prerequisite_gate
# ---------------------------------------------------------------------------

class TestCheckPrerequisiteGate:
    def test_no_gate_when_exam_bank_empty(self, db, student, goals):
        with patch("tutor.agent.mastery._load_exam_bank", return_value={}):
            assert check_prerequisite_gate(student.id, "AST101", 2, db) is None

    def test_gate_fires_for_unmastered_prereq(self, db, student, goals):
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            gate = check_prerequisite_gate(student.id, "AST101", 2, db)
        assert gate is not None
        assert gate.id == "AST101-L01-celestial-sphere"

    def test_no_gate_when_prereq_mastered(self, db, student, goals):
        master_lecture(student.id, db, "AST101", 1)
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            assert check_prerequisite_gate(student.id, "AST101", 2, db) is None

    def test_no_gate_for_lecture_with_no_prerequisites(self, db, student, goals):
        # L01 has no incoming edges in our test graph
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            assert check_prerequisite_gate(student.id, "AST101", 1, db) is None

    def test_no_gate_when_exam_already_pending(self, db, student, goals):
        db.add(MasteryAttempt(
            student_id=student.id,
            goal_id="AST101-L01-celestial-sphere",
            questions_json=json.dumps(_FAKE_EXAM_BANK["AST101-L01-celestial-sphere"]),
            status="pending",
        ))
        db.flush()
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            assert check_prerequisite_gate(student.id, "AST101", 2, db) is None

    def test_gate_returns_after_failed_attempt(self, db, student, goals):
        # A *failed* attempt should allow re-triggering (pending suppresses, not failed)
        db.add(MasteryAttempt(
            student_id=student.id,
            goal_id="AST101-L01-celestial-sphere",
            questions_json=json.dumps(_FAKE_EXAM_BANK["AST101-L01-celestial-sphere"]),
            status="failed",
        ))
        db.flush()
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            gate = check_prerequisite_gate(student.id, "AST101", 2, db)
        assert gate is not None

    def test_no_gate_for_unknown_lecture(self, db, student, goals):
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            assert check_prerequisite_gate(student.id, "AST101", 99, db) is None


# ---------------------------------------------------------------------------
# start_exam
# ---------------------------------------------------------------------------

class TestStartExam:
    def test_creates_pending_attempt(self, db, student, goals):
        goal = goals["l01"][3]  # celestial-sphere
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            attempt = start_exam(student.id, goal, db)
        assert attempt is not None
        assert attempt.status == "pending"
        assert attempt.student_id == student.id
        assert attempt.goal_id == goal.id

    def test_stores_questions_json(self, db, student, goals):
        goal = goals["l01"][3]
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            attempt = start_exam(student.id, goal, db)
        stored = json.loads(attempt.questions_json)
        assert "q1" in stored and "q2" in stored and "code" in stored

    def test_returns_none_when_no_questions(self, db, student, goals):
        goal = goals["l01"][0]  # daily-motion, not in the fake bank
        with patch("tutor.agent.mastery._load_exam_bank", return_value=_FAKE_EXAM_BANK):
            assert start_exam(student.id, goal, db) is None


# ---------------------------------------------------------------------------
# format_exam_intro
# ---------------------------------------------------------------------------

class TestFormatExamIntro:
    def test_contains_goal_title(self):
        goal = LearningGoal(
            id="AST101-L01-celestial-sphere",
            title="Describe the celestial sphere model",
            description="d",
            course="AST101",
            lecture_num=1,
        )
        text = format_exam_intro(goal, _FAKE_EXAM_BANK["AST101-L01-celestial-sphere"])
        assert "Describe the celestial sphere model" in text

    def test_contains_all_three_questions(self):
        goal = LearningGoal(
            id="AST101-L01-celestial-sphere",
            title="Describe the celestial sphere model",
            description="d",
            course="AST101",
            lecture_num=1,
        )
        text = format_exam_intro(goal, _FAKE_EXAM_BANK["AST101-L01-celestial-sphere"])
        assert "Q1:" in text
        assert "Q2:" in text
        assert "Code:" in text

    def test_conceptual_third_question_label(self):
        goal = LearningGoal(
            id="g", title="T", description="d", course="AST101", lecture_num=1,
        )
        conceptual_bank = {
            "q1":   {"question": "Q?", "rubric": "r"},
            "q2":   {"question": "Q?", "rubric": "r"},
            "code": {"question": "Q?", "starter": "", "rubric": "r", "is_conceptual": True},
        }
        text = format_exam_intro(goal, conceptual_bank)
        assert "Q3:" in text
        assert "Code:" not in text
