"""Plan step 1.8 (findings B3, B13): prerequisites across courses, gate chains.

B3: the navigator filtered the mastered set by course while keeping edges
from the other course, so with all of AST 101 mastered, every root AST 102 goal
with an AST 101 prerequisite was never recommended. B13: a fresh AST102 student
faced a chain of 3 to 7 prerequisite exams per lecture, several on AST101
goals, and a deferral snoozed one link of the chain. Decision 5's default: a prerequisite in the
other course is advisory (on the map, never a block or an exam).
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tutor.agent import mastery, navigator, state
from tutor.courses import lecture_numbers
from tutor.db.models import (
    Base,
    GoalPrerequisite,
    LearningGoal,
    Message,
    Student,
    StudentProgress,
)
from tutor.utctime import utcnow


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# B3 on the real goal graph
# ---------------------------------------------------------------------------

@pytest.fixture
def real_graph():
    from tutor.startup import seed_learning_goals_if_empty

    engine = _engine()
    seed_learning_goals_if_empty(engine)
    db = sessionmaker(bind=engine)()
    db.add(Student(id=1, email="stu@example.edu", name="Stu"))
    db.commit()
    goals = {g.id: g for g in db.query(LearningGoal).all()}
    if not goals:
        pytest.skip("curriculum/learning_goals.json not present")
    edges = db.query(GoalPrerequisite).all()
    same_course_prereqs: dict[str, list[str]] = {}
    for e in edges:
        if goals[e.from_goal_id].course == goals[e.to_goal_id].course:
            same_course_prereqs.setdefault(e.to_goal_id, []).append(e.from_goal_id)
    roots_ast102 = {gid for gid, g in goals.items()
                 if g.course == "AST102" and not same_course_prereqs.get(gid)}
    yield db, goals, roots_ast102
    db.close()


def test_all_root_ast102_goals_are_recommended_once_ast101_is_mastered(real_graph):
    db, goals, roots_ast102 = real_graph
    # The fixture's AST 102 roots: three L01 goals and explain-redshift (L04),
    # and every one has an AST 101 prerequisite, the edges B3 tripped on.
    assert len(roots_ast102) == 4
    edges = db.query(GoalPrerequisite).all()
    assert {e.to_goal_id for e in edges if e.to_goal_id in roots_ast102
            and goals[e.from_goal_id].course == "AST101"} == roots_ast102
    for gid, g in goals.items():
        if g.course == "AST101":
            db.add(StudentProgress(student_id=1, goal_id=gid, status="mastered"))
    db.commit()
    assert roots_ast102 <= set(navigator.frontier(1, "AST102", db))


def test_a_ast102_student_without_ast101_records_still_sees_every_root(real_graph):
    db, _, roots_ast102 = real_graph
    assert roots_ast102 <= set(navigator.frontier(1, "AST102", db))


def test_the_gate_chain_walk_on_the_real_graph(real_graph):
    """B13 as the review walked it (plan step 6.1): a fresh AST102 student with
    exam questions for every goal asks a question in each lecture on one day
    and passes every exam it is given. Each lecture raises at most one
    prerequisite exam, never one on an AST101 goal; before step 1.8 a lecture
    raised a chain of 3 to 7."""
    db, goals, _ = real_graph
    per_lecture: dict[int, int] = {}
    with patch.object(mastery, "_load_exam_bank", return_value=_bank(*goals)):
        for lecture in lecture_numbers("AST102"):
            exams = 0
            while (gate := mastery.check_prerequisite_gate(1, "AST102", lecture, db)) is not None:
                assert goals[gate.id].course == "AST102", gate.id
                exams += 1
                if exams > 7:
                    break
                attempt = mastery.start_exam(1, gate, db, trigger_message_id=_message(db, lecture))
                state.record_exam_result(db, attempt, True)
                db.commit()
            per_lecture[lecture] = exams
    assert max(per_lecture.values()) == 1          # one exam, not a chain
    # and the gate does still run: lectures 2, 3 and 4 each have a goal with
    # an earlier-lecture AST 102 prerequisite.
    assert sum(per_lecture.values()) >= 3


# ---------------------------------------------------------------------------
# Small graphs for the navigator and the gate
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = _engine()
    s = sessionmaker(bind=engine)()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    for gid, course, lec in [
        ("AST101-P", "AST101", 2),        # other-course prerequisite of AST102-T
        ("AST102-P1", "AST102", 1),       # same-course prerequisites of lecture 3
        ("AST102-P2", "AST102", 2),
        ("AST102-T", "AST102", 3),
        ("AST102-U", "AST102", 3),
    ]:
        s.add(LearningGoal(id=gid, title=gid, description="", course=course, lecture_num=lec))
    s.flush()
    for frm, to in [("AST101-P", "AST102-T"), ("AST102-P1", "AST102-T"), ("AST102-P2", "AST102-U")]:
        s.add(GoalPrerequisite(from_goal_id=frm, to_goal_id=to))
    s.commit()
    yield s
    s.close()


def _bank(*goal_ids):
    return {gid: {"questions": [
        {"id": f"{gid}-{i}", "kind": "concept" if i < 3 else "code",
         "question": f"{gid} question {i}?", "rubric": "r"} for i in range(5)
    ]} for gid in goal_ids}


def _message(db, lecture=3, course="AST102") -> int:
    m = Message(student_id=1, role="user", content="how do I do this?", lecture_num=lecture, course=course)
    db.add(m)
    db.flush()
    return m.id


def test_cross_course_edge_is_advisory_for_the_navigator(db):
    front = navigator.frontier(1, "AST102", db)
    assert "AST102-T" not in front          # AST102-P1, same course, still binds
    db.add(StudentProgress(student_id=1, goal_id="AST102-P1", status="mastered"))
    db.commit()
    assert "AST102-T" in navigator.frontier(1, "AST102", db)   # AST101-P does not bind


def test_cross_course_prerequisite_never_gates(db):
    db.add(StudentProgress(student_id=1, goal_id="AST102-P1", status="mastered"))
    db.add(StudentProgress(student_id=1, goal_id="AST102-P2", status="mastered"))
    db.commit()
    with patch.object(mastery, "_load_exam_bank", return_value=_bank("AST101-P", "AST102-P1", "AST102-P2")):
        assert mastery.check_prerequisite_gate(1, "AST102", 3, db) is None


def test_one_prerequisite_exam_per_lecture_per_day(db):
    with patch.object(mastery, "_load_exam_bank", return_value=_bank("AST102-P1", "AST102-P2")):
        first = mastery.check_prerequisite_gate(1, "AST102", 3, db)
        assert first is not None and first.id == "AST102-P1"
        attempt = mastery.start_exam(1, first, db, trigger_message_id=_message(db))
        state.record_exam_result(db, attempt, True)          # passed
        db.commit()
        # AST102-P2 is still unmastered, but lecture 3 had its exam today.
        assert mastery.check_prerequisite_gate(1, "AST102", 3, db) is None
        # Tomorrow the next link of the chain may be checked.
        attempt.started_at = utcnow() - datetime.timedelta(days=1)
        db.commit()
        nxt = mastery.check_prerequisite_gate(1, "AST102", 3, db)
        assert nxt is not None and nxt.id == "AST102-P2"


def test_a_deferral_snoozes_the_whole_lecture(db):
    with patch.object(mastery, "_load_exam_bank", return_value=_bank("AST102-P1", "AST102-P2")):
        goal = mastery.check_prerequisite_gate(1, "AST102", 3, db)
        attempt = mastery.start_exam(1, goal, db, trigger_message_id=_message(db))
        state.defer_exam_attempt(db, attempt)
        # Started yesterday, deferred an hour ago: only the snooze applies.
        attempt.started_at = utcnow() - datetime.timedelta(days=1)
        attempt.deferred_at = utcnow() - datetime.timedelta(hours=1)
        db.commit()
        assert mastery.check_prerequisite_gate(1, "AST102", 3, db) is None   # not AST102-P2 either
        attempt.deferred_at = utcnow() - datetime.timedelta(hours=mastery.DEFER_SNOOZE_HOURS + 1)
        db.commit()
        again = mastery.check_prerequisite_gate(1, "AST102", 3, db)
        assert again is not None and again.id == "AST102-P1"
        resumed = mastery.start_exam(1, again, db, trigger_message_id=_message(db))
        assert resumed.id == attempt.id and resumed.status == "pending"   # same questions


def test_another_lecture_is_gated_independently(db):
    db.add(LearningGoal(id="AST102-V", title="V", description="", course="AST102", lecture_num=4))
    db.flush()
    db.add(GoalPrerequisite(from_goal_id="AST102-P2", to_goal_id="AST102-V"))
    db.commit()
    with patch.object(mastery, "_load_exam_bank", return_value=_bank("AST102-P1", "AST102-P2")):
        goal = mastery.check_prerequisite_gate(1, "AST102", 3, db)
        a = mastery.start_exam(1, goal, db, trigger_message_id=_message(db, lecture=3))
        state.record_exam_result(db, a, True)
        db.commit()
        other = mastery.check_prerequisite_gate(1, "AST102", 4, db)
    assert other is not None and other.id == "AST102-P2"
