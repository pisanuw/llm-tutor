"""scripts/set_mastery.py: give a demo student enough mastery that a turn runs
at a ceiling other than H5.

The ceiling is the *most permissive* band among the goals in scope
(``policy.compute_envelope``: meet the student at their weakest goal), and a
goal with no graded evidence counts as mastery 0.0, which is H5 — which is why
a new student's turns all run at H5 and the constructive-code floor (H4) and
the judge's code-adjacent gate (H4 and below) never fire for them. The tool writes real graded events through the single writer, enough of
them that the window's mean is exactly the mastery asked for.
"""
from __future__ import annotations

import pytest

import scripts.set_mastery as sm
from tests.helpers import make_session
from tutor.agent import state
from tutor.agent.policy import rung_for_mastery
from tutor.db.models import LearningGoal, Student, StudentProgress


@pytest.fixture
def db():
    session = make_session(
        Student(id=1, email="ada@example.edu", name="Ada Lovelace"),
        Student(id=2, email="bob@example.edu", name="Bob Stone"),
        *[LearningGoal(id=f"AST101-L03-g{i}", title=f"Goal {i}", course="AST101", lecture_num=3)
          for i in range(1, 5)],
        LearningGoal(id="AST101-L04-g1", title="Later", course="AST101", lecture_num=4),
        LearningGoal(id="AST102-L01-g1", title="Other course", course="AST102", lecture_num=1),
    )
    yield session
    session.close()


# ---------------------------------------------------------------------------
# The ceiling the mastery buys
# ---------------------------------------------------------------------------

def test_the_bands_map_mastery_to_a_ceiling():
    assert (sm.ceiling_for(1.0), sm.ceiling_for(0.85)) == (2, 2)
    assert (sm.ceiling_for(0.70), sm.ceiling_for(0.40), sm.ceiling_for(0.0)) == (3, 4, 5)


def test_a_full_window_lands_exactly_the_mastery_asked_for(db):
    """Why WINDOW_K events: mastery_score is the mean success over the window,
    so a full window at 0.9 is 0.9 and not an average with older evidence."""
    score = 0.0
    for _ in range(state.WINDOW_K):
        score = state.record_graded_event(db, student_id=1, goal_id="AST101-L03-g1",
                                          success=0.9, source=sm.DEMO_SOURCE)
    db.commit()
    assert score == pytest.approx(0.9)
    assert sm.ceiling_for(score) == 2
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="AST101-L03-g1").one()
    assert row.mastery_score == pytest.approx(0.9)
    assert sm.current_mastery(db, 1, ["AST101-L03-g1"]) == {"AST101-L03-g1": pytest.approx(0.9)}


def test_one_goal_with_no_evidence_still_pins_the_turn_at_h5(db):
    """The reason --lecture sets every goal of the lecture: the envelope takes
    the max over the goals in scope, so a single bare goal undoes the rest."""
    for _ in range(state.WINDOW_K):
        state.record_graded_event(db, student_id=1, goal_id="AST101-L03-g1", success=1.0,
                                  source=sm.DEMO_SOURCE)
    db.commit()
    have = sm.current_mastery(db, 1, ["AST101-L03-g1", "AST101-L03-g2"])
    ceilings = [rung_for_mastery(have.get(g) or 0.0) for g in ("AST101-L03-g1", "AST101-L03-g2")]
    assert ceilings == [2, 5] and max(ceilings) == 5


# ---------------------------------------------------------------------------
# Naming the student and the goals
# ---------------------------------------------------------------------------

def test_a_student_is_named_by_id_address_or_enough_of_one(db):
    assert sm.resolve_student(db, "1").id == 1
    assert sm.resolve_student(db, "ada@example.edu").id == 1
    assert sm.resolve_student(db, "Lovelace").id == 1
    with pytest.raises(SystemExit):
        sm.resolve_student(db, "example.edu")          # both students
    with pytest.raises(SystemExit):
        sm.resolve_student(db, "99")


def test_goals_come_from_a_lecture_a_course_or_one_id(db):
    assert [g.id for g in sm.goals_for(db, "AST101", 3, None)] == [f"AST101-L03-g{i}" for i in range(1, 5)]
    assert [g.id for g in sm.goals_for(db, "AST101", None, None)] == \
        [f"AST101-L03-g{i}" for i in range(1, 5)] + ["AST101-L04-g1"]
    assert [g.id for g in sm.goals_for(db, "AST102", None, None)] == ["AST102-L01-g1"]
    assert [g.id for g in sm.goals_for(db, "AST101", 3, "AST101-L04-g1")] == ["AST101-L04-g1"]   # id wins
    with pytest.raises(SystemExit):
        sm.goals_for(db, "AST101", None, "no-such-goal")


def test_the_listing_names_the_ceiling_a_turn_would_get(db, capsys):
    for _ in range(state.WINDOW_K):
        state.record_graded_event(db, student_id=1, goal_id="AST101-L03-g1", success=0.9,
                                  source=sm.DEMO_SOURCE)
    db.commit()
    sm.print_listing(db, db.get(Student, 1), "AST101", 3)
    out = capsys.readouterr().out
    assert "ada@example.edu (id 1)" in out
    assert "AST101-L03-g1" in out and "0.90" in out and "H2" in out
    assert "AST101-L03-g2" in out and "—" in out                  # no evidence yet
    assert "A turn with all 4 in scope runs at H5" in out       # the weakest goal decides


# ---------------------------------------------------------------------------
# The command line refuses what it cannot do
# ---------------------------------------------------------------------------

def test_it_wants_a_mastery_in_range_and_something_to_set():
    assert sm.main(["--student", "ada@example.edu", "--lecture", "3", "--mastery", "1.5"]) == 2
    assert sm.main(["--student", "ada@example.edu", "--lecture", "3", "--mastery", "-1"]) == 2
    assert sm.main(["--student", "ada@example.edu"]) == 2             # neither --lecture nor --goal
