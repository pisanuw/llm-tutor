"""Retention anonymization: the 1-year sweep that de-identifies a student.

state.anonymize_student_data strips PII + free text and keeps de-identified
telemetry under a pseudonym (docs/retention-policy.md). scripts/retention_sweep
selects who is eligible by inactivity. These run network-free.
"""
from __future__ import annotations

import datetime

import pytest

from tutor.agent import state
from tutor.db.models import (
    ExerciseAttempt, GradedEvent, JudgeSample, MagicLink, Message,
    Session as SessionModel, SessionSummary, Student, StudentProgress,
    TopicAccess, TurnEvent,
)
from tutor.utctime import utcnow
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    yield s
    s.close()


def _seed_student(db, sid=1, email="stu@example.edu") -> Student:
    now = utcnow()
    s = Student(id=sid, email=email, name="Real Name", google_sub="g-123",
                last_seen=now, last_login=now)
    db.add(s)
    db.flush()
    # Free text / auth (must be removed)
    db.add(Message(student_id=sid, role="user", content="my student id is abc123, why no eclipse?"))
    db.add(Message(student_id=sid, role="assistant", content="let's trace it"))
    db.add(SessionModel(student_id=sid, jwt_jti="j1", method="google",
                        expires_at=now + datetime.timedelta(days=1)))
    db.add(MagicLink(student_id=sid, token="tok-1", expires_at=now + datetime.timedelta(hours=1)))
    db.add(SessionSummary(student_id=sid, course="AST101", summary="Real Name struggled with X"))
    ex = ExerciseAttempt(student_id=sid, exercise_id="AST101-L03-x",
                         submission="int main(){ /* abc123 */ }", feedback="good, abc123", score=3.0, passed=True)
    db.add(ex)
    db.add(TopicAccess(student_id=sid, lecture_num=3, topic_tag="my name is Real Name and..."))
    # Telemetry (must be kept; reason redacted)
    te = TurnEvent(student_id=sid, kind="chat", intent="ATTEMPT", rung_used=2, cost_usd=0.01)
    db.add(te)
    db.flush()
    db.add(JudgeSample(turn_event_id=te.id, judge="haiku", verdict="allow",
                       reason="the draft quoted the student's code abc123"))
    db.add(GradedEvent(student_id=sid, goal_id="AST101-L03-x", success=1.0, source="exercise"))
    db.add(StudentProgress(student_id=sid, goal_id="AST101-L03-x", status="mastered", mastery_score=0.9))
    db.commit()
    return db.get(Student, sid)


def test_anonymize_strips_pii_and_freetext_keeps_telemetry(db):
    _seed_student(db)
    counts = state.anonymize_student_data(db, 1)
    db.commit()

    # Auth + free-text rows are gone.
    assert db.query(Message).filter_by(student_id=1).count() == 0
    assert db.query(SessionModel).filter_by(student_id=1).count() == 0
    assert db.query(MagicLink).filter_by(student_id=1).count() == 0
    assert db.query(SessionSummary).filter_by(student_id=1).count() == 0

    # Free-text fields redacted, de-identified columns kept.
    ex = db.query(ExerciseAttempt).filter_by(student_id=1).one()
    assert ex.submission == "" and ex.feedback == ""
    assert ex.score == 3.0 and ex.passed is True
    ta = db.query(TopicAccess).filter_by(student_id=1).one()
    assert ta.topic_tag == "" and ta.lecture_num == 3
    js = db.query(JudgeSample).one()
    assert js.reason == "" and js.verdict == "allow"

    # Telemetry kept.
    assert db.query(TurnEvent).filter_by(student_id=1).count() == 1
    assert db.query(GradedEvent).filter_by(student_id=1).count() == 1
    assert db.query(StudentProgress).filter_by(student_id=1).count() == 1

    # Identity pseudonymized, row kept as an opaque key.
    s = db.get(Student, 1)
    assert s is not None
    assert s.email == "anon-1@anonymized.invalid"
    assert s.name == ""
    assert s.google_sub is None
    assert s.anonymized_at is not None
    assert counts["students_anonymized"] == 1


def test_anonymize_is_idempotent(db):
    _seed_student(db)
    state.anonymize_student_data(db, 1)
    db.commit()
    first_email = db.get(Student, 1).email

    # Second pass is a no-op (already anonymized).
    assert state.anonymize_student_data(db, 1) == {}
    assert db.get(Student, 1).email == first_email


def test_anonymize_unknown_student_is_noop(db):
    assert state.anonymize_student_data(db, 9999) == {}


def test_anonymized_telemetry_is_still_linkable_but_not_identifiable(db):
    _seed_student(db)
    state.anonymize_student_data(db, 1)
    db.commit()
    # The kept telemetry still joins to the (now pseudonymous) student row.
    te = db.query(TurnEvent).filter_by(student_id=1).one()
    assert db.get(Student, te.student_id).email == "anon-1@anonymized.invalid"


# ---------------------------------------------------------------------------
# Sweep eligibility (scripts/retention_sweep._last_activity)
# ---------------------------------------------------------------------------

def test_last_activity_picks_the_most_recent_signal(db):
    from scripts.retention_sweep import _last_activity

    old = utcnow() - datetime.timedelta(days=400)
    recent = utcnow() - datetime.timedelta(days=2)
    s = Student(id=1, email="a@example.edu", last_seen=old, last_login=old)
    db.add(s)
    db.flush()
    db.add(Message(student_id=1, role="user", content="hi", created_at=recent))
    db.commit()
    la = _last_activity(db, db.get(Student, 1), Message, TurnEvent)
    assert abs((la - recent).total_seconds()) < 1   # the recent message wins over old last_seen


def test_last_activity_counts_reading_the_slides(db):
    """A student who only opened decks (activity events, no message) is
    active (activity analytics, step 1.9)."""
    from scripts.retention_sweep import _last_activity
    from tutor.db.models import ActivityEvent

    old = utcnow() - datetime.timedelta(days=400)
    recent = utcnow() - datetime.timedelta(days=3)
    db.add(Student(id=1, email="a@example.edu", last_seen=old, last_login=old))
    db.flush()
    db.add(ActivityEvent(student_id=1, visit_id="v", page_view_id="p", seq=1, name="deck_open", ts=recent))
    db.commit()
    la = _last_activity(db, db.get(Student, 1), Message, TurnEvent)
    assert abs((la - recent).total_seconds()) < 1


def test_the_sweep_prunes_raw_activity_events_only_when_told(db):
    from scripts.retention_sweep import prune_activity_events
    from tutor.db.models import ActivityEvent

    now = utcnow()
    db.add(Student(id=1, email="a@example.edu"))
    db.flush()
    for days_ago, seq in ((500, 1), (10, 2)):
        db.add(ActivityEvent(student_id=1, visit_id="v", page_view_id="p", seq=seq, name="click",
                             ts=now - datetime.timedelta(days=days_ago)))
    db.commit()
    assert prune_activity_events(db, now, None, confirm=True) == 0       # unset: keep everything
    assert prune_activity_events(db, now, 400, confirm=False) == 1       # a dry run counts
    assert db.query(ActivityEvent).count() == 2
    assert prune_activity_events(db, now, 400, confirm=True) == 1
    assert [e.seq for e in db.query(ActivityEvent).all()] == [2]
