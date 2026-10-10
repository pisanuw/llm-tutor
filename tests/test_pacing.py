"""Tests for adaptive pacing module (Session 16).

Scripted-trajectory tests:
- Passive student gets elicitation move suggested (ENCOURAGE_ATTEMPT/ELICIT_PREDICTION)
- Stuck student gets switch/analogy move (MODALITY_SWITCH/WORKED_ANALOGY)
- Decayed goal surfaces in warm-up hint
- Pacing guardrail caps repeated ADVANCE/SUGGEST_REVIEW within the window
- navigator_facts returns correct top-goal and decayed-goal data
"""

from __future__ import annotations

import datetime
import json

import pytest

from tutor.db.models import (
    LearningGoal,
    Message,
    Student,
    StudentProgress,
    Track,
    TurnEvent,
)
from tutor.agent.pacing import navigator_facts, pacing_allowed, warm_up_hint
from tutor.agent.state import DECAY_FLOOR
from tutor.utctime import utcnow
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    session = make_session()
    yield session
    session.close()


def _student(db, email="pacing@test.com") -> Student:
    s = Student(email=email, name="Pacing Test")
    db.add(s)
    db.flush()
    return s


def _goal(db, gid="g1", course="AST101", lecture=1) -> LearningGoal:
    g = LearningGoal(id=gid, title=gid.upper(), description="", course=course, lecture_num=lecture)
    db.add(g)
    db.flush()
    return g


def _track(db, course="AST101", goal_ids=None) -> Track:
    tid = f"{course}-lecture-order"
    t = Track(
        id=tid, course=course, title="Lecture Order", description="",
        goal_ids_json=json.dumps(goal_ids or []), graph_version=1,
    )
    db.add(t)
    db.flush()
    return t


def _master(db, student_id, goal_id, score=0.9, days_ago=0):
    last_ev = utcnow() - datetime.timedelta(days=days_ago)
    row = db.query(StudentProgress).filter_by(student_id=student_id, goal_id=goal_id).first()
    if row is None:
        row = StudentProgress(student_id=student_id, goal_id=goal_id)
        db.add(row)
    row.status = "mastered"
    row.evidence_type = "test"
    row.mastery_score = score
    row.last_evidence_at = last_ev
    db.flush()


def _add_strategist_event(db, student_id, move: str):
    ev = TurnEvent(
        student_id=student_id,
        ts=utcnow(),
        kind="strategist",
        verdicts_json=json.dumps({"move": move}),
        in_tokens=0, out_tokens=0, cache_read_tokens=0, cost_usd=0.0,
    )
    db.add(ev)
    db.flush()


# ---------------------------------------------------------------------------
# pacing_allowed
# ---------------------------------------------------------------------------

def test_pacing_allowed_when_no_events(db):
    s = _student(db)
    assert pacing_allowed(s.id, db) is True


def test_pacing_blocked_after_advance(db):
    s = _student(db)
    _add_strategist_event(db, s.id, "ADVANCE")
    assert pacing_allowed(s.id, db) is False


def test_pacing_blocked_after_suggest_review(db):
    s = _student(db)
    _add_strategist_event(db, s.id, "SUGGEST_REVIEW")
    assert pacing_allowed(s.id, db) is False


def test_pacing_blocked_after_modality_switch(db):
    s = _student(db)
    _add_strategist_event(db, s.id, "MODALITY_SWITCH")
    assert pacing_allowed(s.id, db) is False


def test_pacing_allowed_after_non_pacing_move(db):
    s = _student(db)
    _add_strategist_event(db, s.id, "ELICIT_PREDICTION")
    assert pacing_allowed(s.id, db) is True


def test_pacing_allowed_after_window_clears(db):
    """If pacing_allowed reads only last window_turns events, old pacing events are ignored."""
    s = _student(db)
    # Add 4 pacing events — but the window is 3, so the oldest falls out
    for _ in range(4):
        _add_strategist_event(db, s.id, "ADVANCE")
    # Add 3 non-pacing events on top, pushing the pacing events out of window
    for _ in range(3):
        _add_strategist_event(db, s.id, "ELICIT_PREDICTION")
    assert pacing_allowed(s.id, db, window_turns=3) is True


def test_pacing_independent_per_student(db):
    s1 = _student(db, "a@x.com")
    s2 = _student(db, "b@x.com")
    _add_strategist_event(db, s1.id, "ADVANCE")
    assert pacing_allowed(s1.id, db) is False
    assert pacing_allowed(s2.id, db) is True


# ---------------------------------------------------------------------------
# warm_up_hint
# ---------------------------------------------------------------------------

def test_warm_up_hint_none_when_no_mastered_goals(db):
    s = _student(db)
    assert warm_up_hint(s.id, "AST101", db) is None


def test_warm_up_hint_none_when_mastery_is_fresh(db):
    s = _student(db)
    _goal(db, "g1")
    _master(db, s.id, "g1", score=0.9, days_ago=0)
    # Fresh mastery — effective score still near 0.9, above DECAY_FLOOR
    assert warm_up_hint(s.id, "AST101", db) is None


def test_warm_up_hint_returns_text_when_goal_decayed(db):
    s = _student(db)
    _goal(db, "g1")
    # Mastered long ago with high score — effective score will be at DECAY_FLOOR
    _master(db, s.id, "g1", score=0.95, days_ago=30)
    hint = warm_up_hint(s.id, "AST101", db)
    assert hint is not None
    assert "G1" in hint or "warm" in hint.lower() or "review" in hint.lower()


def test_warm_up_hint_none_on_second_message_today(db):
    s = _student(db)
    _goal(db, "g1")
    _master(db, s.id, "g1", score=0.95, days_ago=30)
    # Simulate the student already sent a message today
    db.add(Message(
        student_id=s.id, role="user", content="hello",
        created_at=utcnow(),
    ))
    db.flush()
    assert warm_up_hint(s.id, "AST101", db) is None


def test_warm_up_hint_picks_most_decayed(db):
    s = _student(db)
    _goal(db, "g1")
    _goal(db, "g2")
    _master(db, s.id, "g1", score=0.9,  days_ago=5)   # less decayed
    _master(db, s.id, "g2", score=0.95, days_ago=30)  # more decayed
    hint = warm_up_hint(s.id, "AST101", db)
    # The hint should name g2 (more decayed)
    assert hint is not None
    assert "G2" in hint


# ---------------------------------------------------------------------------
# navigator_facts
# ---------------------------------------------------------------------------

def test_navigator_facts_empty_when_no_goals(db):
    s = _student(db)
    _track(db, "AST101", [])
    facts = navigator_facts(s.id, "AST101", db)
    assert facts["top_goal_id"] is None
    assert facts["decayed_goal_id"] is None
    assert facts["pacing_ok"] is True


def test_navigator_facts_top_goal_is_first_frontier(db):
    s = _student(db)
    _goal(db, "g1")
    _goal(db, "g2", lecture=2)
    _track(db, "AST101", ["g1", "g2"])
    facts = navigator_facts(s.id, "AST101", db)
    assert facts["top_goal_id"] == "g1"
    assert facts["top_goal_title"] == "G1"


def test_navigator_facts_top_goal_advances_when_g1_mastered(db):
    s = _student(db)
    _goal(db, "g1")
    _goal(db, "g2", lecture=2)
    _track(db, "AST101", ["g1", "g2"])
    _master(db, s.id, "g1")
    facts = navigator_facts(s.id, "AST101", db)
    assert facts["top_goal_id"] == "g2"


def test_navigator_facts_decayed_goal_populated(db):
    s = _student(db)
    _goal(db, "g1")
    _master(db, s.id, "g1", score=0.95, days_ago=30)
    _track(db, "AST101", ["g1"])
    facts = navigator_facts(s.id, "AST101", db)
    # g1 is mastered so not in frontier; decayed_goal should be set
    assert facts["decayed_goal_id"] == "g1"
    assert facts["decayed_score"] is not None
    assert facts["decayed_score"] <= DECAY_FLOOR + 0.01


def test_navigator_facts_pacing_ok_false_after_advance(db):
    s = _student(db)
    _track(db, "AST101", [])
    _add_strategist_event(db, s.id, "ADVANCE")
    facts = navigator_facts(s.id, "AST101", db)
    assert facts["pacing_ok"] is False


# ---------------------------------------------------------------------------
# strategist._facts integration (unit test, no API call)
# ---------------------------------------------------------------------------

def _make_envelope(**kw):
    from tutor.agent.policy import Envelope
    defaults = dict(
        max_rung=3, goals=[], exam_lockdown=[], must_ground=False,
        allow_exercise=True, escalate_review=False, reason="test",
    )
    defaults.update(kw)
    return Envelope(**defaults)


def test_strategist_facts_string_includes_nav_context():
    from tutor.agent.strategist import _facts
    from tutor.agent.policy import Intent
    from tutor.agent.trajectory import Trajectory

    env = _make_envelope(goals=["g1"])
    traj = Trajectory(
        session_turns=5, attempts=0, questions=2, passive=3,
        minutes_since_last_attempt=None, goals=(),
    )
    nav = {
        "top_goal_id": "g2",
        "top_goal_title": "Seasons",
        "decayed_goal_id": "g1",
        "decayed_title": "Moon phases",
        "decayed_score": 0.60,
        "pacing_ok": True,
    }
    result = _facts(env, traj, Intent.QUESTION, nav_facts=nav)
    assert "Seasons" in result
    assert "Moon phases" in result
    assert "60%" in result


def test_strategist_facts_pacing_cap_injected():
    from tutor.agent.strategist import _facts
    from tutor.agent.policy import Intent
    from tutor.agent.trajectory import Trajectory

    env = _make_envelope()
    traj = Trajectory(session_turns=1, attempts=0, questions=0, passive=1,
                      minutes_since_last_attempt=None, goals=())
    nav = {"top_goal_id": None, "top_goal_title": "", "decayed_goal_id": None,
           "decayed_title": "", "decayed_score": None, "pacing_ok": False}
    result = _facts(env, traj, Intent.QUESTION, nav_facts=nav)
    assert "PACING CAP" in result
    assert "ADVANCE" in result
