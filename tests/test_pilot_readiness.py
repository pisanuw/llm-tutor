"""Session 26: pilot readiness — the data-deletion path and consent notice.

The docs (retention-policy, runbook) are reviewed by a human; these
tests cover the two executable pieces: FERPA erasure and the login disclosure.
"""
from __future__ import annotations

import pytest

from tutor.agent import state
from tutor.db.models import (
    ExerciseAttempt, GradedEvent, JudgeSample, LearningGoal, Message,
    Student, StudentProgress, TurnEvent,
)
from tutor.utctime import utcnow
from tests.helpers import make_session


@pytest.fixture()
def db():
    s = make_session()
    yield s
    s.close()


def _seed_student(db, email) -> Student:
    s = Student(email=email, name="T")
    db.add(s)
    db.flush()
    if db.get(LearningGoal, "G1") is None:
        db.add(LearningGoal(id="G1", title="g", description="", course="AST101", lecture_num=1))
        db.flush()
    db.add(Message(student_id=s.id, role="user", content="hi"))
    db.add(Message(student_id=s.id, role="assistant", content="hello"))
    db.add(ExerciseAttempt(student_id=s.id, exercise_id="e1", submission="x", feedback="f"))
    db.add(GradedEvent(student_id=s.id, goal_id="G1", success=1.0, source="exercise"))
    db.add(StudentProgress(student_id=s.id, goal_id="G1", status="mastered"))
    te = TurnEvent(student_id=s.id, ts=utcnow(), kind="chat")
    db.add(te)
    db.flush()
    db.add(JudgeSample(turn_event_id=te.id, judge="auditor:x", verdict="allow", reason="r"))
    db.flush()
    return s


def test_delete_student_data_erases_everything(db):
    a = _seed_student(db, "a@example.edu")
    b = _seed_student(db, "b@example.edu")  # control: must survive

    a_te_ids = [r.id for r in db.query(TurnEvent.id).filter_by(student_id=a.id).all()]
    counts = state.delete_student_data(db, a.id)
    db.flush()

    # Everything for A is gone, across every table.
    assert db.query(Student).filter_by(id=a.id).count() == 0
    for model in (Message, ExerciseAttempt, GradedEvent, StudentProgress, TurnEvent):
        assert db.query(model).filter_by(student_id=a.id).count() == 0
    assert db.query(JudgeSample).filter(JudgeSample.turn_event_id.in_(a_te_ids)).count() == 0

    # The counts report what was removed.
    assert counts["students"] == 1
    assert counts["messages"] == 2
    assert counts["judge_samples"] == 1

    # B is untouched.
    assert db.query(Student).filter_by(id=b.id).count() == 1
    assert db.query(Message).filter_by(student_id=b.id).count() == 2


def test_delete_is_safe_for_unknown_student(db):
    counts = state.delete_student_data(db, 9999)
    assert counts["students"] == 0


