"""Evidence-based mastery (pedagogy item 9): a single sitting can reach
status=mastered (progression tier, stamps mastered_at) but cannot CONFIRM;
confirmed_at is set only when two successful retrievals are at least
CONFIRM_MIN_GAP_HOURS apart, which the spaced-review system supplies later."""

from __future__ import annotations

import datetime

import pytest

from tutor.agent import state
from tutor.db.models import GradedEvent, LearningGoal, Student, StudentProgress
from tests.helpers import make_session

DAY1 = datetime.datetime(2026, 9, 1, 15, 0, 0)


@pytest.fixture()
def db():
    s = make_session()
    st = Student(email="ev@example.com", name="Ev")
    s.add(st)
    s.add(LearningGoal(id="g1", title="G1", description="", course="AST101", lecture_num=1))
    s.commit()
    yield s, st.id
    s.close()


def _row(db, sid):
    return db.query(StudentProgress).filter_by(student_id=sid, goal_id="g1").one()


def test_one_sitting_masters_but_does_not_confirm(db):
    s, sid = db
    for _ in range(state.MASTER_MIN_EVENTS):
        state.record_graded_event(s, sid, "g1", 1.0, source="exam")
    row = _row(s, sid)
    assert row.status == "mastered"
    assert row.mastered_at is not None
    assert row.confirmed_at is None  # all evidence on one day


def test_direct_grant_stamps_mastered_at_but_not_confirmed(db):
    s, sid = db
    assert state.mark_goal_mastered(s, sid, "g1", evidence_type="mastery_exam") is True
    row = _row(s, sid)
    assert row.status == "mastered" and row.mastered_at is not None
    assert row.confirmed_at is None


def test_spaced_success_confirms(db, monkeypatch):
    s, sid = db
    for _ in range(state.MASTER_MIN_EVENTS):
        state.record_graded_event(s, sid, "g1", 1.0, source="exam")
    assert _row(s, sid).confirmed_at is None
    # Backdate the first sitting's evidence by three days, then a review lands.
    for ev in s.query(GradedEvent).filter_by(student_id=sid, goal_id="g1").all():
        ev.ts = ev.ts - datetime.timedelta(days=3)
    s.flush()
    state.record_graded_event(s, sid, "g1", 1.0, source="review")
    row = _row(s, sid)
    assert row.confirmed_at is not None


def test_failed_spaced_review_does_not_confirm(db):
    s, sid = db
    for _ in range(state.MASTER_MIN_EVENTS):
        state.record_graded_event(s, sid, "g1", 1.0, source="exam")
    for ev in s.query(GradedEvent).filter_by(student_id=sid, goal_id="g1").all():
        ev.ts = ev.ts - datetime.timedelta(days=3)
    s.flush()
    state.record_graded_event(s, sid, "g1", 0.0, source="review")  # forgot it
    assert _row(s, sid).confirmed_at is None


def test_confirmation_is_permanent(db):
    s, sid = db
    for _ in range(state.MASTER_MIN_EVENTS):
        state.record_graded_event(s, sid, "g1", 1.0, source="exam")
    for ev in s.query(GradedEvent).filter_by(student_id=sid, goal_id="g1").all():
        ev.ts = ev.ts - datetime.timedelta(days=3)
    s.flush()
    state.record_graded_event(s, sid, "g1", 1.0, source="review")
    first = _row(s, sid).confirmed_at
    assert first is not None
    state.record_graded_event(s, sid, "g1", 0.0, source="review")  # later lapse
    assert _row(s, sid).confirmed_at == first


def test_progress_summary_flags_unconfirmed(db):
    s, sid = db
    from tutor.agent.progress import progress_summary
    state.mark_goal_mastered(s, sid, "g1", evidence_type="mastery_exam")
    s.commit()
    text = progress_summary(sid, s, course="AST101")
    assert "await a spaced review" in text
