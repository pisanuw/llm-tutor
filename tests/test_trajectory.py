"""Session 8 tests: trajectory state over synthetic ledgers (stuck, cruising,
passive, grinding), plus the window and purity invariants."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest

from tutor.agent import state
from tutor.agent.trajectory import SESSION_WINDOW_HOURS, compute_trajectory
from tutor.db.models import LearningGoal, Student
from tutor.utctime import utcnow
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Light and telescopes", description="", course="AST101", lecture_num=4))
    s.commit()
    yield s
    s.close()


def _chat(db, goal="G1", intent="QUESTION", rung=4, ts=None):
    ev = state.record_turn_event(
        db, student_id=1, kind="chat", goal_ids=[goal] if goal else None,
        intent=intent, rung_used=rung,
    )
    if ts is not None:
        ev.ts = ts
        db.flush()
    return ev


def _exam(db, goal="G1", passed=False):
    return state.record_turn_event(
        db, student_id=1, kind="exam", goal_ids=[goal], verdicts={"passed": passed},
    )


# ---------------------------------------------------------------------------
# The four archetype ledgers
# ---------------------------------------------------------------------------

def test_stuck_ledger_flags_repeated_error(db):
    # Four turns on the same goal, ceiling not easing, nothing graded.
    for rung in (4, 4, 5, 5):
        _chat(db, rung=rung)
    g = compute_trajectory(db, 1).get("G1")
    assert g.turns == 4
    assert g.rungs_used == (4, 4, 5, 5)
    assert g.repeated_error is True


def test_cruising_ledger_is_not_stuck(db):
    # The ceiling eases (4 -> 2) and a graded success lands.
    for rung in (4, 3, 2):
        _chat(db, rung=rung)
    state.record_graded_event(db, 1, "G1", 1.0, source="exercise")
    g = compute_trajectory(db, 1).get("G1")
    assert g.turns == 3
    assert g.repeated_error is False
    assert g.graded_scores[-1] == 1.0


def test_passive_ledger_counts_presence_without_attempts(db):
    for _ in range(5):
        _chat(db, intent="OTHER", rung=5)
    t = compute_trajectory(db, 1)
    assert t.session_turns == 5
    assert t.passive == 5
    assert t.attempts == 0
    assert t.minutes_since_last_attempt is None   # never attempted anything


def test_grinding_ledger_attempts_pile_up_with_exam_fails(db):
    for _ in range(3):
        _chat(db, intent="ATTEMPT", rung=4)
    _exam(db, passed=False)
    _exam(db, passed=False)
    t = compute_trajectory(db, 1)
    g = t.get("G1")
    assert t.attempts == 3
    assert g.attempts == 3
    assert g.exam_fails == 2 and g.exam_passes == 0
    assert g.repeated_error is True               # effort without success
    assert t.minutes_since_last_attempt is not None
    assert t.minutes_since_last_attempt < 5


def test_exam_pass_clears_the_stuck_signal(db):
    for rung in (4, 4, 4):
        _chat(db, rung=rung)
    _exam(db, passed=True)
    g = compute_trajectory(db, 1).get("G1")
    assert g.exam_passes == 1
    assert g.repeated_error is False


# ---------------------------------------------------------------------------
# Window and structure
# ---------------------------------------------------------------------------

def test_events_outside_the_window_are_ignored(db):
    old = utcnow() - datetime.timedelta(hours=SESSION_WINDOW_HOURS + 1)
    for _ in range(4):
        _chat(db, ts=old)
    t = compute_trajectory(db, 1)
    assert t.session_turns == 0
    assert t.get("G1") is None


def test_old_attempt_still_sets_minutes_since_last_attempt(db):
    old = utcnow() - datetime.timedelta(hours=20)
    _chat(db, intent="ATTEMPT", ts=old)
    t = compute_trajectory(db, 1)
    assert t.session_turns == 0                   # outside the window
    assert t.minutes_since_last_attempt == pytest.approx(20 * 60, rel=0.05)


def test_trajectory_is_frozen(db):
    _chat(db)
    t = compute_trajectory(db, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.session_turns = 99
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.goals[0].turns = 99


def test_trajectory_module_reads_no_prose_and_calls_no_llm():
    src = (Path(__file__).resolve().parent.parent / "tutor" / "agent" / "trajectory.py").read_text()
    assert "anthropic" not in src
    assert "Message" not in src.replace("GradedEvent", "").replace("TurnEvent", "")
