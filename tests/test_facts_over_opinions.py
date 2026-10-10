"""Session 20 (S29/F6): deterministic facts outrank LLM opinions.

Covers:
- score_from_facts maps compiler+test facts to a 0-4 score, deterministically
- evaluate_code uses the facts path when run_code returns a verdict, with
  eval_source "test", and never calls the rubric LLM
- evaluate_code falls back to the rubric (eval_source "llm-opinion") when
  execution is disabled or no tests exist
- feedback never reveals expected output or fix code
- record_exercise_outcome tags the graded event source from evidence_source
- the SAME buggy submission scores low by facts and the telemetry distinguishes
  the two sources
"""
from __future__ import annotations

import pytest

from tutor.agent import exercises, state
from tutor.agent.exercises import (
    _feedback_from_facts,
    evaluate_code,
    score_from_facts,
)
from tutor.db.models import GradedEvent, LearningGoal, Student, TurnEvent
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# score_from_facts (pure)
# ---------------------------------------------------------------------------

def _facts(compiled, tests, stderr="", compile_error=""):
    return {
        "enabled": True, "backend": "subprocess", "compiled": compiled,
        "compile_error": compile_error, "timed_out": False, "tests": tests,
        "all_passed": bool(compiled and tests and all(t["passed"] for t in tests)),
        "stderr_head": stderr, "note": "",
    }


def _t(name, passed):
    return {"name": name, "passed": passed, "expected": "", "got": "", "timed_out": False}


def test_score_no_compile_is_zero():
    assert score_from_facts(_facts(False, [], compile_error="error: ...")) == (0, False)


def test_score_all_fail_is_one():
    f = _facts(True, [_t("a", False), _t("b", False)])
    assert score_from_facts(f) == (1, False)


def test_score_partial_is_two():
    f = _facts(True, [_t("a", True), _t("b", False)])
    assert score_from_facts(f) == (2, False)


def test_score_all_pass_clean_is_four():
    f = _facts(True, [_t("a", True), _t("b", True)])
    assert score_from_facts(f) == (4, True)


def test_score_all_pass_dirty_is_three():
    f = _facts(True, [_t("a", True)], stderr="AddressSanitizer: global-buffer-overflow")
    assert score_from_facts(f) == (3, True)


def test_score_compiled_no_tests_is_two():
    assert score_from_facts(_facts(True, [])) == (2, False)


# ---------------------------------------------------------------------------
# feedback never leaks the answer or fix code
# ---------------------------------------------------------------------------

def test_feedback_hides_expected_and_fix():
    f = _facts(True, [{"name": "five", "passed": False, "expected": "120",
                       "got": "sk-ant-api03-leaked", "timed_out": False}])
    feedback, hint = _feedback_from_facts(f)
    assert "120" not in feedback  # expected output is not revealed
    # Nothing the program printed is echoed (plan step 6.9, finding B6): it
    # can carry any file the program could read.
    assert "leaked" not in feedback and "sk-ant" not in feedback
    assert "five" in feedback and "produced output" in feedback
    assert "```" not in feedback and "```" not in hint  # no code
    silent = _facts(True, [{"name": "five", "passed": False, "expected": "120", "got": " \n", "timed_out": False}])
    assert "produced no output" in _feedback_from_facts(silent)[0]
    flood = _facts(True, [{"name": "five", "passed": False, "expected": "120", "got": "x" * 50,
                           "timed_out": False, "output_capped": True}])
    assert "stopped" in _feedback_from_facts(flood)[0] and "xxx" not in _feedback_from_facts(flood)[0]


def test_feedback_compile_error_is_actionable():
    f = _facts(False, [], compile_error="main.cpp:3:5: error: expected ';'")
    feedback, hint = _feedback_from_facts(f)
    assert "did not compile" in feedback
    assert "main.cpp:3:5: error: expected ';'" in feedback


def test_feedback_quotes_only_a_complaint_about_the_students_file():
    """An #include of another file makes the compiler quote that file."""
    f = _facts(False, [], compile_error=(
        "In file included from main.cpp:1:\n"
        "/app/.env:1:1: error: 'ANTHROPIC_API_KEY' does not name a type\n"
        "main.cpp:4:2: error: expected ';' before '}' token"))
    feedback, _hint = _feedback_from_facts(f)
    assert "ANTHROPIC_API_KEY" not in feedback and ".env" not in feedback
    assert "main.cpp:4:2: error: expected ';' before '}' token" in feedback
    only_other = _facts(False, [], compile_error="/etc/secret:1:1: error: 'TOKEN' does not name a type")
    feedback, _hint = _feedback_from_facts(only_other)
    assert "TOKEN" not in feedback and "did not compile" in feedback


def test_an_infrastructure_miss_is_never_the_students_grade():
    """Plan step 6.9 (finding B9): a compile that did not finish, a toolchain
    failure, or a refused backend used to read as the student's 0."""
    from tutor.agent.exercises import _facts_path_valid

    real = _facts(False, [], compile_error="main.cpp:3:5: error: expected ';'")
    assert _facts_path_valid(real)
    for why in ("compilation did not finish in 30 s", "the toolchain failed: cannot find -lasan",
                "the subprocess backend does not run where DATABASE_URL is set"):
        assert not _facts_path_valid({**real, "compile_error": "", "infra_error": why})
        assert not _facts_path_valid({**_facts(True, [_t("a", True)]), "infra_error": why})


# ---------------------------------------------------------------------------
# evaluate_code routing: facts vs opinion (no network)
# ---------------------------------------------------------------------------

def test_evaluate_code_uses_facts_when_available(monkeypatch):
    canned = _facts(True, [_t("a", False), _t("b", False)])
    monkeypatch.setattr(exercises, "code_exec_enabled", lambda: True)
    monkeypatch.setattr(exercises, "load_exercise_tests", lambda eid: [{"name": "a"}])
    monkeypatch.setattr(exercises, "run_code", lambda *a, **k: canned)

    # If this reached the rubric path it would try to build an Anthropic client.
    def _boom(*a, **k):
        raise AssertionError("rubric LLM must not be called on the facts path")
    monkeypatch.setattr(exercises, "_evaluate_with_rubric", _boom)

    res = evaluate_code("int main(){}", "square a number", exercise_id="AST101-L04-inverse-square-law-code")
    assert res["eval_source"] == "test"
    assert res["score"] == 1 and res["correct"] is False
    assert "facts" in res["eval_details"]


def test_evaluate_code_falls_back_to_rubric_when_exec_disabled(monkeypatch):
    monkeypatch.setattr(exercises, "code_exec_enabled", lambda: False)
    sentinel = {"score": 2, "correct": False, "feedback": "f", "hint": "h",
                "eval_details": {"improved_code": ""}}
    monkeypatch.setattr(exercises, "_evaluate_with_rubric", lambda *a, **k: dict(sentinel))

    res = evaluate_code("code", "problem", exercise_id="AST101-L04-inverse-square-law-code")
    assert res["eval_source"] == "llm-opinion"
    assert res["score"] == 2
    assert res["exercise_id"] == "AST101-L04-inverse-square-law-code"


def test_evaluate_code_rubric_when_no_tests(monkeypatch):
    monkeypatch.setattr(exercises, "code_exec_enabled", lambda: True)
    monkeypatch.setattr(exercises, "load_exercise_tests", lambda eid: [])  # no tests
    called = {}
    monkeypatch.setattr(exercises, "run_code",
                        lambda *a, **k: called.setdefault("ran", True))
    monkeypatch.setattr(exercises, "_evaluate_with_rubric",
                        lambda *a, **k: {"score": 3, "correct": True, "feedback": "",
                                          "hint": "", "eval_details": {"improved_code": ""}})
    res = evaluate_code("code", "problem", exercise_id="AST101-L03-sun-path-code")
    assert res["eval_source"] == "llm-opinion"
    assert "ran" not in called  # run_code is not invoked without tests


# ---------------------------------------------------------------------------
# Telemetry distinguishes the two sources
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    s = make_session()
    yield s
    s.close()


def _setup(db):
    student = Student(email="s@example.edu", name="T")
    db.add(student)
    db.add(LearningGoal(id="G1", title="g", description="", course="AST101", lecture_num=4))
    db.flush()
    return student


def test_graded_event_source_test_vs_exercise(db):
    s = _setup(db)
    # Facts-based attempt -> graded source "test".
    state.record_exercise_outcome(
        db, student_id=s.id, exercise_id="AST101-L04-inverse-square-law-code",
        lecture_num=4, submission="x", score=1.0, passed=False, feedback="f",
        course="AST101", goal_ids=["G1"], evidence_source="test",
    )
    # Opinion-based attempt -> graded source "exercise".
    state.record_exercise_outcome(
        db, student_id=s.id, exercise_id="chat-1-9",
        lecture_num=4, submission="x", score=2.0, passed=False, feedback="f",
        course="AST101", goal_ids=["G1"], evidence_source="llm-opinion",
    )
    db.flush()
    sources = sorted(e.source for e in db.query(GradedEvent).all())
    assert sources == ["exercise", "test"]

    verdict_sources = {
        te.verdicts_json for te in db.query(TurnEvent).filter_by(kind="exercise").all()
    }
    blob = " ".join(verdict_sources)
    assert "test" in blob and "llm-opinion" in blob
