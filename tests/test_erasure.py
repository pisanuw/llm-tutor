"""FERPA erasure and the one-year anonymization, with foreign keys enforced.

SQLite ignores foreign keys unless asked; Postgres always enforces them. These
tests turn enforcement on and seed a row in every table that can hold a
student's data, so a table the erasure code misses fails here instead of in
production (plan step 0.4, findings B1 and B19).
"""

from __future__ import annotations

import datetime
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tutor.agent import state
from tutor.db.models import (
    ActivityEvent, Base, ClassEnrollment, CourseClass, ExerciseAttempt, GoalMisconception,
    FeedbackEvent, GradedEvent, JudgeSample, LearningGoal, MagicLink, MasteryAttempt, Message,
    MessageFeedback, Misconception, PathEvent, ScorePrediction,
    Session as SessionModel, SessionSummary, Student, StudentPath,
    StudentProgress, TopicAccess, Track, TurnEvent,
)

GOAL = "AST101-L03-tilt"
NOW = datetime.datetime(2026, 9, 1, 12, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(LearningGoal(id=GOAL, title="Axial tilt", course="AST101", lecture_num=3))
    session.add(Track(id="AST101-lecture-order", course="AST101", title="Lecture order",
                      goal_ids_json=json.dumps([GOAL])))
    session.flush()
    yield session
    session.close()


def _note(sid: int) -> str:
    return f"thinks summer comes when Earth is nearest the Sun (student {sid})"


def _seed(db, sid: int) -> None:
    """One row in every table that references a student, plus free text that
    names the student, so leftovers are easy to spot."""
    db.add(Student(id=sid, email=f"s{sid}@example.edu", name=f"Real Name {sid}",
                   google_sub=f"g-{sid}", last_seen=NOW, last_login=NOW))
    db.flush()
    db.add(SessionModel(student_id=sid, jwt_jti=f"jti-{sid}", method="google",
                        expires_at=NOW + datetime.timedelta(days=7)))
    db.add(MagicLink(student_id=sid, token=f"tok-{sid}", expires_at=NOW))
    db.add(Message(student_id=sid, role="user", course="AST101", content=f"login s{sid}, help"))
    reply = Message(student_id=sid, role="assistant", course="AST101", content="let's sketch it")
    db.add(reply)
    db.flush()
    db.add(MessageFeedback(message_id=reply.id, student_id=sid, rating=-1,
                           comment=f"I'm student {sid} and this was wrong"))
    db.add(SessionSummary(student_id=sid, course="AST101", summary=f"Real Name {sid} struggled"))
    db.add(ExerciseAttempt(student_id=sid, exercise_id=GOAL, submission="int main(){}",
                           feedback=f"good, s{sid}", score=3.0, passed=True))
    db.add(TopicAccess(student_id=sid, lecture_num=3, topic_tag=f"my name is Real Name {sid}"))
    db.add(ScorePrediction(student_id=sid, course="AST101", predicted=3, actual=2,
                           context=f"login s{sid}, help"))
    db.add(GradedEvent(student_id=sid, goal_id=GOAL, success=1.0, source="exercise"))
    db.add(StudentProgress(student_id=sid, goal_id=GOAL, status="mastered", mastery_score=0.9))
    db.add(MasteryAttempt(student_id=sid, goal_id=GOAL, questions_json="[]"))
    db.add(StudentPath(student_id=sid, course="AST101", track_id="AST101-lecture-order"))
    db.add(PathEvent(student_id=sid, course="AST101", modality="teach", goal_id=GOAL))
    db.add(ActivityEvent(student_id=sid, visit_id=f"v{sid}", page_view_id=f"p{sid}", seq=1,
                         name="click", ts=NOW, target="send-btn", label=f"Send, s{sid}",
                         value_ms=1200, detail_json='{"kind": "typed"}'))
    db.add(Misconception(student_id=sid, goal_id=GOAL, description=_note(sid)))
    te = TurnEvent(student_id=sid, kind="chat", intent="ATTEMPT", rung_used=2,
                   verdicts_json=json.dumps({
                       "verdict": "revise", "judge_rule": "leaked_code",
                       "judge_reason": f"draft quoted s{sid}'s code",
                       "error": f"[parameters: {{'content': 'login s{sid}'}}]",
                   }))
    db.add(te)
    db.flush()
    db.add(JudgeSample(turn_event_id=te.id, judge="auditor:x", verdict="allow",
                       reason=f"draft quoted s{sid}"))
    db.flush()


def _with_rating_history(db, sid: int) -> None:
    """_seed covers message_feedback (the latest opinion); add the append-only
    rating log, which references the same reply."""
    reply = db.query(Message).filter_by(student_id=sid, role="assistant").one()
    db.add(FeedbackEvent(message_id=reply.id, student_id=sid, rating=-1, comment="wrong", ts=NOW))
    db.flush()


# ---------------------------------------------------------------------------
# Clearing one conversation: a reset of what was said, not an erasure
# ---------------------------------------------------------------------------

def test_clear_conversation_removes_the_transcript_and_the_rolling_summary(db):
    """What a clean demo needs: the page goes blank AND the tutor forgets.
    Clearing the messages alone would leave the summary feeding the previous
    conversation into the next prompt."""
    _seed(db, 1)
    _with_rating_history(db, 1)
    db.commit()

    counts = state.clear_conversation(db, 1)
    db.commit()

    assert set(counts) == set(state.CONVERSATION_TABLES)
    assert db.query(Message).filter_by(student_id=1).count() == 0
    assert db.query(MessageFeedback).filter_by(student_id=1).count() == 0
    assert db.query(FeedbackEvent).filter_by(student_id=1).count() == 0
    assert db.query(SessionSummary).filter_by(student_id=1).count() == 0


def test_clear_conversation_keeps_the_student_their_progress_and_the_ledger(db):
    _seed(db, 1)
    _with_rating_history(db, 1)
    db.commit()
    state.clear_conversation(db, 1)
    db.commit()

    assert db.get(Student, 1) is not None                                  # still signed up
    assert db.query(SessionModel).filter_by(student_id=1).count() == 1      # still signed in
    # the mastery a demo was given, and the evidence behind it
    assert db.query(StudentProgress).filter_by(student_id=1).count() == 1
    assert db.query(GradedEvent).filter_by(student_id=1).count() == 1
    assert db.query(MasteryAttempt).filter_by(student_id=1).count() == 1
    # the telemetry the Supervision tab reads, and its children
    assert db.query(TurnEvent).filter_by(student_id=1).count() == 1
    assert db.query(JudgeSample).count() == 1
    assert db.query(TopicAccess).filter_by(student_id=1).count() == 1
    assert db.query(ActivityEvent).filter_by(student_id=1).count() == 1


def test_clear_conversation_is_idempotent_and_touches_one_student(db):
    _seed(db, 1)
    _seed(db, 2)
    _with_rating_history(db, 1)
    _with_rating_history(db, 2)
    db.commit()

    state.clear_conversation(db, 1)
    db.commit()
    assert db.query(Message).filter_by(student_id=2).count() == 2           # the other student stands
    assert db.query(SessionSummary).filter_by(student_id=2).count() == 1
    assert db.query(FeedbackEvent).filter_by(student_id=2).count() == 1

    assert all(n == 0 for n in state.clear_conversation(db, 1).values())    # nothing left to do


def _seed_pair_with_class(db) -> int:
    """Student 1 teaches a class that student 2 is enrolled in; both have data;
    the cohort library holds one note from each. Returns the class id."""
    _seed(db, 1)
    _seed(db, 2)
    klass = CourseClass(name="AST 101 A", course="AST101", instructor_id=1)
    db.add(klass)
    db.flush()
    db.add(ClassEnrollment(class_id=klass.id, student_id=2))
    db.add(GoalMisconception(goal_id=GOAL, description="seasons-from-distance confusion",
                             examples_json=json.dumps([_note(1), _note(2)])))
    db.commit()
    return klass.id


def _tables_referencing_students() -> set[str]:
    return {t.name for t in Base.metadata.sorted_tables
            if any(fk.column.table.name == "students" for fk in t.foreign_keys)}


# ---------------------------------------------------------------------------
# Scope and policy cover every table
# ---------------------------------------------------------------------------

def test_erasure_scope_covers_every_table_that_holds_student_rows():
    scope = set(state.erasure_scope(1))
    assert _tables_referencing_students() <= scope
    # Rows that reach a student only through another student-owned row.
    assert {"judge_samples", "message_feedback", "class_enrollments"} <= scope
    # Shared curriculum is never in scope.
    assert not {"learning_goals", "goal_prerequisites", "tracks",
                "goal_misconceptions", "whitelist", "settings"} & scope


def test_anonymize_policy_decides_every_table_in_scope():
    assert set(state.ANONYMIZE_POLICY) == set(state.erasure_scope(1))
    assert set(state.ANONYMIZE_POLICY.values()) <= {"delete", "redact", "keep", "pseudonymize"}
    for table, policy in state.ANONYMIZE_POLICY.items():
        if policy == "redact":
            assert table in state._REDACTIONS or table == "turn_events", table


# ---------------------------------------------------------------------------
# Deletion on request
# ---------------------------------------------------------------------------

def test_delete_removes_every_row_for_the_student_with_foreign_keys_enforced(db):
    class_id = _seed_pair_with_class(db)
    counts = state.delete_student_data(db, 1)
    db.commit()

    for model in (SessionModel, MagicLink, Message, MessageFeedback, SessionSummary,
                  ExerciseAttempt, TopicAccess, ScorePrediction, GradedEvent,
                  StudentProgress, MasteryAttempt, StudentPath, PathEvent,
                  Misconception, TurnEvent):
        assert db.query(model).filter_by(student_id=1).count() == 0, model.__tablename__
    assert db.get(Student, 1) is None
    assert db.query(JudgeSample).count() == 1          # only student 2's
    assert db.get(CourseClass, class_id) is None       # the class student 1 taught
    assert db.query(ClassEnrollment).count() == 0      # and its roster
    examples = json.loads(db.query(GoalMisconception).one().examples_json)
    assert examples == [_note(2)]

    assert counts["students"] == 1
    assert counts["message_feedback"] == 1
    assert counts["score_predictions"] == 1
    assert counts["judge_samples"] == 1
    assert counts["classes"] == 1
    assert counts["class_enrollments"] == 1
    assert counts["goal_misconception_examples_scrubbed"] == 1


def test_delete_leaves_other_students_untouched(db):
    _seed_pair_with_class(db)
    state.delete_student_data(db, 1)
    db.commit()
    assert db.get(Student, 2).email == "s2@example.edu"
    assert db.query(Message).filter_by(student_id=2).count() == 2
    assert db.query(MessageFeedback).filter_by(student_id=2).count() == 1
    assert db.query(ScorePrediction).filter_by(student_id=2).one().context == "login s2, help"
    assert db.query(Misconception).filter_by(student_id=2).one().description == _note(2)


def test_delete_unknown_student_is_a_no_op(db):
    _seed(db, 2)
    db.commit()
    counts = state.delete_student_data(db, 9999)
    db.commit()
    assert counts["students"] == 0
    assert db.query(Message).count() == 2


# ---------------------------------------------------------------------------
# One-year anonymization
# ---------------------------------------------------------------------------

def test_anonymize_removes_free_text_everywhere_with_foreign_keys_enforced(db):
    _seed_pair_with_class(db)
    counts = state.anonymize_student_data(db, 1)
    db.commit()

    for model in (SessionModel, MagicLink, Message, MessageFeedback, SessionSummary):
        assert db.query(model).filter_by(student_id=1).count() == 0, model.__tablename__
    assert db.query(ExerciseAttempt).filter_by(student_id=1).one().submission == ""
    assert db.query(TopicAccess).filter_by(student_id=1).one().topic_tag == ""
    assert db.query(Misconception).filter_by(student_id=1).one().description == ""

    prediction = db.query(ScorePrediction).filter_by(student_id=1).one()
    assert prediction.context == ""
    assert (prediction.predicted, prediction.actual) == (3, 2)   # calibration data kept

    click = db.query(ActivityEvent).filter_by(student_id=1).one()
    assert click.label == ""                                   # a control's text, blanked
    assert (click.target, click.value_ms, click.detail_json) == ("send-btn", 1200, '{"kind": "typed"}')

    verdicts = json.loads(db.query(TurnEvent).filter_by(student_id=1).one().verdicts_json)
    assert verdicts == {"verdict": "revise", "judge_rule": "leaked_code"}
    te_ids = [te.id for te in db.query(TurnEvent).filter_by(student_id=1)]
    assert db.query(JudgeSample).filter(JudgeSample.turn_event_id.in_(te_ids)).one().reason == ""

    examples = json.loads(db.query(GoalMisconception).one().examples_json)
    assert examples == [_note(2)]

    student = db.get(Student, 1)
    assert student.email == "anon-1@anonymized.invalid"
    assert student.name == "" and student.google_sub is None
    assert counts["message_feedback"] == 1
    assert counts["score_predictions_redacted"] == 1
    assert counts["turn_events_redacted"] == 1
    assert counts["students_anonymized"] == 1


def test_anonymize_keeps_structured_telemetry_and_other_students(db):
    _seed_pair_with_class(db)
    state.anonymize_student_data(db, 1)
    db.commit()
    for model in (GradedEvent, StudentProgress, MasteryAttempt, StudentPath, PathEvent,
                  ActivityEvent):
        assert db.query(model).filter_by(student_id=1).count() == 1, model.__tablename__
    assert db.query(CourseClass).count() == 1
    assert db.query(Message).filter_by(student_id=2).count() == 2
    assert db.query(MessageFeedback).filter_by(student_id=2).one().comment
    verdicts = json.loads(db.query(TurnEvent).filter_by(student_id=2).one().verdicts_json)
    assert "judge_reason" in verdicts
