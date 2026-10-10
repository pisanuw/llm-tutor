"""Session 18 (S28): the curated exercise bank.

Covers:
- curriculum/exercises.json loads, is well-formed, and every exercise has
  goal ids + an expected_behavior (the exit gate)
- every goal id an exercise references exists in learning_goals.json
- get_exercise filters by goal id (and falls back across difficulty)
- goals_for_exercise resolves bank ids and is empty for synthetic ids
- record_exercise_outcome attributes the graded event to the exercise's goals
  only, not every goal in the lecture (S9)
- the build script's leading-verb difficulty classifier
"""
from __future__ import annotations

import json

import pytest

from tutor.agent import state
from tutor.agent.exercises import (
    Exercise,
    get_exercise,
    goals_for_exercise,
    load_exercise_bank,
)
from tutor.db.models import ExerciseAttempt, GradedEvent, LearningGoal, Student
from tutor.paths import CURRICULUM_DIR
from tests.helpers import make_session

EXERCISES_JSON = CURRICULUM_DIR / "exercises.json"
GOALS_JSON = CURRICULUM_DIR / "learning_goals.json"


# ---------------------------------------------------------------------------
# The curated file and the loaded bank
# ---------------------------------------------------------------------------

def test_exercises_json_exists_and_is_wrapped():
    assert EXERCISES_JSON.exists(), "curriculum/exercises.json must be committed"
    data = json.loads(EXERCISES_JSON.read_text())
    assert isinstance(data, dict) and "exercises" in data
    # At least one drill for every goal of the curriculum.
    goal_ids = {g["id"] for g in json.loads(GOALS_JSON.read_text())["goals"]}
    assert {gid for ex in data["exercises"] for gid in ex["goal_ids"]} == goal_ids


def test_every_exercise_has_goals_and_expected_behavior():
    """The exit gate: every exercise has goals and expected_behavior."""
    bank = load_exercise_bank()
    assert bank, "bank loaded from JSON should be non-empty"
    for ex in bank:
        assert ex.goal_ids, f"{ex.id} has no goal_ids"
        assert ex.expected_behavior.strip(), f"{ex.id} has empty expected_behavior"
        assert ex.prompt.strip(), f"{ex.id} has empty prompt"
        assert ex.difficulty in {"concept", "trace", "code", "debug"}


def test_exercise_ids_unique():
    bank = load_exercise_bank()
    ids = [ex.id for ex in bank]
    assert len(ids) == len(set(ids))


def test_every_referenced_goal_exists():
    goal_ids = {g["id"] for g in json.loads(GOALS_JSON.read_text())["goals"]}
    bank = load_exercise_bank()
    referenced = {gid for ex in bank for gid in ex.goal_ids}
    missing = referenced - goal_ids
    assert not missing, f"exercises reference unknown goals: {sorted(missing)[:5]}"


def test_bank_covers_a_known_goal():
    bank = load_exercise_bank()
    by_goal = {gid for ex in bank for gid in ex.goal_ids}
    # An audited goal that should certainly have a drill.
    assert "AST101-L03-sun-path" in by_goal


# ---------------------------------------------------------------------------
# get_exercise filtering
# ---------------------------------------------------------------------------

def test_get_exercise_filters_by_goal_id():
    res = get_exercise(topic="ignored", goal_id="AST101-L03-sun-path", difficulty="code")
    assert "AST101-L03-sun-path" in res["goal_ids"]
    assert res["difficulty"] == "code"


def test_get_exercise_goal_id_falls_back_across_difficulty():
    # No 'debug' exercise exists for this goal; it should still return one for
    # the goal rather than dropping to a [GENERATE] placeholder.
    res = get_exercise(topic="x", goal_id="AST101-L03-sun-path", difficulty="debug")
    assert "AST101-L03-sun-path" in res["goal_ids"]
    assert not res["prompt"].startswith("[GENERATE]")


def test_get_exercise_topic_match_without_goal_id():
    res = get_exercise(topic="moon phases", difficulty="concept", course="AST101")
    assert res["course"] == "AST101"
    assert res["difficulty"] == "concept"


def test_get_exercise_unknown_goal_returns_placeholder():
    res = get_exercise(topic="z", goal_id="AST101-L99-nonexistent")
    assert res["prompt"].startswith("[GENERATE]")
    assert res["goal_ids"] == ["AST101-L99-nonexistent"]


# ---------------------------------------------------------------------------
# goals_for_exercise
# ---------------------------------------------------------------------------

def test_goals_for_exercise_resolves_bank_id():
    assert goals_for_exercise("AST101-L03-sun-path-code") == ["AST101-L03-sun-path"]


def test_goals_for_exercise_empty_for_synthetic_id():
    assert goals_for_exercise("chat-1-5") == []


# ---------------------------------------------------------------------------
# Per-goal attribution in record_exercise_outcome (S9)
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    s = make_session()
    yield s
    s.close()


def _student(db) -> Student:
    s = Student(email="s@example.edu", name="T")
    db.add(s)
    db.flush()
    return s


def _goal(db, gid, course="AST101", lecture=3):
    g = LearningGoal(id=gid, title=gid, description="", course=course, lecture_num=lecture)
    db.add(g)
    db.flush()
    return g


def test_record_exercise_outcome_attributes_only_mapped_goals(db):
    s = _student(db)
    # Two goals in the same lecture; only one is the exercise's goal.
    _goal(db, "AST101-L03-sun-path", lecture=3)
    _goal(db, "AST101-L03-solstice-equinox", lecture=3)

    state.record_exercise_outcome(
        db,
        student_id=s.id,
        exercise_id="AST101-L03-sun-path-code",
        lecture_num=3,
        submission="...",
        score=4.0,
        passed=True,
        feedback="ok",
        course="AST101",
        goal_ids=["AST101-L03-sun-path"],
    )
    db.flush()
    events = db.query(GradedEvent).filter_by(student_id=s.id).all()
    goals_hit = {e.goal_id for e in events}
    assert goals_hit == {"AST101-L03-sun-path"}
    assert all(e.source == "exercise" for e in events)


def test_record_exercise_outcome_without_goal_ids_records_no_evidence(db):
    """Plan step 1.5 (B4): no lecture fallback. A synthetic chat submission
    with no classified goals leaves an attempt row and no graded events; it
    used to credit (or penalize) every goal of the header-selected lecture."""
    s = _student(db)
    _goal(db, "AST101-L03-sun-path", lecture=3)
    _goal(db, "AST101-L03-solstice-equinox", lecture=3)

    state.record_exercise_outcome(
        db,
        student_id=s.id,
        exercise_id="chat-1-3",
        lecture_num=3,
        submission="...",
        score=3.0,
        passed=True,
        feedback="ok",
        course="AST101",
        goal_ids=None,
    )
    db.flush()
    assert db.query(GradedEvent).filter_by(student_id=s.id).count() == 0
    assert db.query(ExerciseAttempt).filter_by(student_id=s.id).count() == 1


# ---------------------------------------------------------------------------
# Build-script difficulty classification (leading verb)
# ---------------------------------------------------------------------------

def test_build_leading_verb_classification():
    from pipeline.build_exercises import _exercises_for_goal

    def diffs(goal):
        return {e["difficulty"] for e in _exercises_for_goal(goal)}

    # "Locate the pole star" asks for neither code nor a worked example:
    # concept only, even though the title mentions a program.
    locate = {
        "id": "AST101-L01-pole-star", "course": "AST101", "lecture": 1,
        "title": "Locate the pole star with a star-chart program",
        "description": "Student can find Polaris from the Big Dipper.", "hook": "",
    }
    assert diffs(locate) == {"concept"}

    # "Implement ..." earns a code exercise.
    impl = {
        "id": "AST101-L04-inverse-square", "course": "AST101", "lecture": 4,
        "title": "Implement the inverse-square law for brightness",
        "description": "Student can compute how a star dims with distance.", "hook": "",
    }
    assert diffs(impl) == {"concept", "code"}

    # "Trace ..." earns a trace exercise.
    trace = {
        "id": "AST101-L02-moon-orbit", "course": "AST101", "lecture": 2,
        "title": "Trace the Moon's orbit through one month",
        "description": "Student can sketch the Moon's phase at each quarter.", "hook": "",
    }
    assert diffs(trace) == {"concept", "trace"}


def test_build_is_deterministic(tmp_path):
    from pipeline.build_exercises import build_bank
    from tutor.agent.exercises import _load_from_json

    goals = json.loads(GOALS_JSON.read_text())["goals"]
    a = build_bank(goals)
    b = build_bank(goals)
    assert [e["id"] for e in a] == [e["id"] for e in b]
    # And the app reads back every exercise the build writes: each row is a
    # whole Exercise, and every goal gets a drill. (The fixture's committed
    # bank is hand-curated, with extra exercises other tests need, so the
    # build is compared through the reader rather than with that file.)
    assert all(Exercise(**row).goal_ids for row in a)
    written = tmp_path / "exercises.json"
    written.write_text(json.dumps({"exercises": a}, indent=2) + "\n")
    assert [e.id for e in _load_from_json(written)] == [e["id"] for e in a]
    assert {gid for e in a for gid in e["goal_ids"]} == {g["id"] for g in goals}
