"""Task 3 tests: the single-writer state module and its invariant.

Network-free, in-memory SQLite. Covers the typed transitions and the
mechanical invariant that no module other than state.py constructs the guarded
progress / mastery / event models.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from tutor.agent import state
from tutor.db.models import (
    JudgeSample,
    GradedEvent,
    LearningGoal,
    MasteryAttempt,
    Student,
    StudentProgress,
    TurnEvent,
)
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="a@example.edu", name="A"))
    s.add(LearningGoal(id="G1", title="Light and telescopes", description="", course="AST101", lecture_num=4))
    s.add(LearningGoal(id="G2", title="The spectrum", description="", course="AST101", lecture_num=4))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Progress transitions
# ---------------------------------------------------------------------------

def test_unlock_goal_creates_then_noops(db):
    assert state.unlock_goal(db, 1, "G1") is True
    assert state.unlock_goal(db, 1, "G1") is False  # already unlocked
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one()
    assert row.status == "unlocked"


def test_mark_goal_mastered_upgrades_from_unlocked(db):
    state.unlock_goal(db, 1, "G1")
    assert state.mark_goal_mastered(db, 1, "G1") is True
    assert state.mark_goal_mastered(db, 1, "G1") is False
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one()
    assert row.status == "mastered"


# ---------------------------------------------------------------------------
# Mastery exam transitions
# ---------------------------------------------------------------------------

def test_start_exam_attempt_is_pending(db):
    a = state.start_exam_attempt(db, 1, "G1", json.dumps({"q": 1}))
    assert a.status == "pending"
    assert a.id is not None


def test_record_exam_result_pass_masters_goal_and_logs_event(db):
    a = state.start_exam_attempt(db, 1, "G1", "{}")
    reopened = state.record_exam_result(db, a, passed=True)
    assert reopened is None
    assert a.status == "passed"
    assert db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one().status == "mastered"
    assert db.query(TurnEvent).filter_by(kind="exam").count() == 1


def test_record_exam_result_fail_reopens_only_with_requeue(db):
    # Without requeued questions the attempt closes and nothing reopens
    # (mastery.py decides whether to requeue, applying the daily cap).
    a = state.start_exam_attempt(db, 1, "G1", "{}")
    assert state.record_exam_result(db, a, passed=False) is None
    assert a.status == "failed"

    # With requeued questions a fresh pending attempt carries the count,
    # the resampled questions, and the original gate trigger.
    b = state.start_exam_attempt(db, 1, "G1", "{}", trigger_message_id=77)
    reopened = state.record_exam_result(db, b, passed=False, requeue_questions_json='{"new": 1}')
    assert reopened is not None and reopened.status == "pending"
    assert reopened.attempts == 1  # carried the incremented count
    assert reopened.questions_json == '{"new": 1}'
    assert reopened.trigger_message_id == 77
    # A graded event (score 0) is recorded; the goal is NOT mastered.
    prog = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one_or_none()
    assert prog is None or prog.status != "mastered"


def test_mark_attempt_failed(db):
    a = state.start_exam_attempt(db, 1, "G1", "{}")
    state.mark_attempt_failed(db, a)
    assert a.status == "failed"
    assert db.query(MasteryAttempt).filter_by(status="pending").count() == 0


# ---------------------------------------------------------------------------
# Exercise outcome
# ---------------------------------------------------------------------------

def _mastered(db):
    return db.query(StudentProgress).filter_by(student_id=1, status="mastered").count()


def test_record_exercise_outcome_one_opinion_never_masters(db):
    """Plan step 1.5 (B4): a single LLM-rubric 4 is evidence, not mastery."""
    state.record_exercise_outcome(
        db, student_id=1, exercise_id="ex-1", lecture_num=4,
        submission="code", score=4, passed=True, feedback="great", course="AST101",
        goal_ids=["G1", "G2"], evidence_source="llm-opinion",
    )
    assert _mastered(db) == 0
    assert db.query(GradedEvent).filter_by(student_id=1).count() == 2
    assert db.query(TurnEvent).filter_by(kind="exercise").count() == 1


def test_record_exercise_outcome_test_evidence_masters(db):
    state.record_exercise_outcome(
        db, student_id=1, exercise_id="ex-1", lecture_num=4,
        submission="code", score=4, passed=True, feedback="great", course="AST101",
        goal_ids=["G1"], evidence_source="test",
    )
    assert _mastered(db) == 1


def test_record_exercise_outcome_masters_after_enough_events(db):
    """Four 3/4 opinions on the classified goal master it; never before."""
    for i in range(state.MASTER_MIN_EVENTS):
        assert _mastered(db) == 0, f"mastered after {i} events"
        state.record_exercise_outcome(
            db, student_id=1, exercise_id="chat-1-4", lecture_num=4,
            submission="code", score=3, passed=True, feedback="ok", course="AST101",
            goal_ids=["G1"], evidence_source="llm-opinion",
        )
    assert _mastered(db) == 1
    # G2, the lecture's other goal, was never touched.
    assert db.query(GradedEvent).filter_by(student_id=1, goal_id="G2").count() == 0


def test_record_exercise_outcome_unavailable_records_no_evidence(db):
    state.record_exercise_outcome(
        db, student_id=1, exercise_id="chat-1-4", lecture_num=4,
        submission="code", score=0, passed=False, feedback="", course="AST101",
        goal_ids=["G1"], evidence_source="unavailable",
    )
    assert db.query(GradedEvent).filter_by(student_id=1).count() == 0


def test_record_exercise_outcome_low_score_no_mastery(db):
    state.record_exercise_outcome(
        db, student_id=1, exercise_id="ex-1", lecture_num=4,
        submission="code", score=1, passed=False, feedback="see hint", course="AST101",
    )
    assert db.query(StudentProgress).filter_by(student_id=1, status="mastered").count() == 0


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def test_record_turn_event_serialises_fields(db):
    ev = state.record_turn_event(
        db, student_id=1, kind="chat", goal_ids=["G1", "G2"],
        intent="QUESTION", rung_used=3, grounding_hit=True,
        model="claude-sonnet-4-6", in_tokens=100, out_tokens=20,
        cache_read_tokens=30000, cost_usd=0.012,
    )
    assert ev.id is not None
    assert json.loads(ev.goal_ids_json) == ["G1", "G2"]
    assert ev.rung_used == 3 and ev.grounding_hit is True
    assert ev.cost_usd == pytest.approx(0.012)


def test_add_judge_sample(db):
    ev = state.record_turn_event(db, student_id=1, kind="chat")
    s = state.add_judge_sample(db, ev.id, judge="haiku", verdict="allow", reason="ok")
    assert s.id is not None
    assert db.query(JudgeSample).filter_by(turn_event_id=ev.id).count() == 1


# ---------------------------------------------------------------------------
# Single-writer invariant (Section 4)
# ---------------------------------------------------------------------------

# Every model that holds a learner's state. Plan step 1.11 (finding B16) added
# the last seven; Message (the transcript) and cohort data are not in the set.
GUARDED_MODELS = frozenset({
    "StudentProgress", "MasteryAttempt", "TurnEvent", "JudgeSample", "GradedEvent",
    "SessionSummary", "Misconception", "PathEvent", "StudentPath", "ExerciseAttempt",
    "TopicAccess", "ScorePrediction", "MessageFeedback",
})
WRITER_ALLOWED = {"state.py", "models.py"}  # state writes; models defines


def _names(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _writer_offenses(source: str) -> list[str]:
    """Learner-state writes in ``source``, found without running it:
    constructing a guarded model (bare or dotted name); a bulk
    ``.update()``/``.delete()`` on a query over one, or core update/delete;
    and, per function, an attribute assignment or ``session.delete`` on a
    name bound from an expression that mentions a guarded model
    (``row = db.get(StudentProgress, ...)``, ``for r in db.query(...)``)."""
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if name in GUARDED_MODELS:
            out.append(f"{node.lineno} {name}(...)")
        if isinstance(f, ast.Attribute) and f.attr in ("update", "delete") and _names(f.value) & GUARDED_MODELS:
            out.append(f"{node.lineno} bulk .{f.attr}()")
        if isinstance(f, ast.Name) and f.id in ("update", "delete") and node.args \
                and _names(node.args[0]) & GUARDED_MODELS:
            out.append(f"{node.lineno} core {f.id}()")
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        bound: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and _names(n.value) & GUARDED_MODELS:
                bound |= {t.id for t in n.targets if isinstance(t, ast.Name)}
            if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and _names(n.iter) & GUARDED_MODELS:
                if isinstance(n.target, ast.Name):
                    bound.add(n.target.id)
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                targets = n.targets
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                targets = [n.target]
            else:
                targets = []
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in bound:
                    out.append(f"{n.lineno} {t.value.id}.{t.attr} = ...")
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "delete"
                    and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in bound):
                out.append(f"{n.lineno} delete({n.args[0].id})")
    return out


def test_only_state_module_writes_learner_state():
    root = pathlib.Path(__file__).resolve().parent.parent / "tutor"
    offenders: list[str] = []
    for p in root.rglob("*.py"):
        if p.name in WRITER_ALLOWED:
            continue
        offenders += [f"{p.relative_to(root)}:{o}" for o in _writer_offenses(p.read_text())]
    assert not offenders, "learner state written outside state.py:\n" + "\n".join(offenders)


@pytest.mark.parametrize("snippet", [
    # The plan's "done when": a router that flips a status directly.
    """
def mark(db, sid, gid):
    row = db.query(StudentProgress).filter_by(student_id=sid, goal_id=gid).first()
    row.status = "mastered"
""",
    """
def bump(db, attempt_id):
    a = db.get(MasteryAttempt, attempt_id)
    a.attempts += 1
""",
    """
def reset(db, sid):
    for r in db.query(StudentProgress).filter_by(student_id=sid):
        r.mastery_score = None
""",
    "def f(db, sid):\n    db.query(GradedEvent).filter_by(student_id=sid).delete()\n",
    "def f(db):\n    db.query(StudentProgress).update({'status': 'mastered'})\n",
    "def f(db):\n    db.execute(delete(TurnEvent))\n",
    "def f(db, sid):\n    db.add(models.ScorePrediction(student_id=sid))\n",
    "def f(db, mid):\n    fb = db.get(MessageFeedback, mid)\n    db.delete(fb)\n",
])
def test_writer_guard_catches(snippet):
    assert _writer_offenses(snippet), snippet


def test_writer_guard_ignores_reads_and_other_models():
    snippet = """
def f(db, sid):
    rows = db.query(StudentProgress).filter_by(student_id=sid).all()
    total = sum(r.mastery_score or 0 for r in rows)
    student = db.get(Student, sid)
    student.name = "x"
    return total
"""
    assert _writer_offenses(snippet) == []


def test_misconception_mining_library_never_commits():
    """Plan step 1.11: the library flushes; its callers (the weekly script and
    the admin route) own the transaction."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "tutor" / "agent"
           / "misconception_mining.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "commit"]
    assert not calls, [c.lineno for c in calls]
