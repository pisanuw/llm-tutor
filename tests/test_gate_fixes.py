"""Gate fixes after a first-week transcript (a question about `*` in code).

A first-week conceptual question turned into two prerequisite exams, the
post-pass replay re-gated, and "can we talk about eclipses instead?" (DEFER)
re-issued the same exam on the very next message. Covers:

1. A deferred exam snoozes its goal in check_prerequisite_gate for
   DEFER_SNOOZE_HOURS; after the snooze the gate resumes the same questions.
2. assemble_envelope(gate_eligible=False) never consults the gate, so a
   plain QUESTION, a post-pass replay, or a post-defer message is answered.
3. Leaked move names (PROBE_PRIOR etc.) are stripped from streamed prose
   across arbitrary chunk boundaries, and from batch text, without touching
   ordinary bold, inline code, or a lone `*`.
4. The actor prompt forbids writing move names.
"""
from __future__ import annotations

import datetime
import json
from unittest.mock import patch

import pytest

from tutor.agent import mastery
from tutor.agent.detector import StreamGate, strip_move_tags
from tutor.agent.envelope import assemble_envelope
from tutor.agent.mastery import DEFER_SNOOZE_HOURS, check_prerequisite_gate, start_exam
from tutor.agent.policy import Move
from tutor.agent import state
from tutor.db.models import (
    GoalPrerequisite, LearningGoal, MasteryAttempt, Student,
)
from tests.helpers import make_session

_BANK = {
    "AST101-L01-celestial-sphere": {
        "q1": {"question": "What is the celestial equator?", "rubric": "Earth's equator on the sky"},
        "q2": {"question": "Name two celestial coordinates.", "rubric": "right ascension, declination"},
        "code": {"question": "Print 1-5.", "starter": "int main() { }", "rubric": "loop",
                 "is_conceptual": False},
    },
    "AST101-L02-eclipse-conditions": {
        "q1": {"question": "When is a solar eclipse possible?", "rubric": "x"},
        "q2": {"question": "Why not at every new Moon?", "rubric": "y"},
        "code": {"question": "Count the eclipse seasons in a year.", "starter": "", "rubric": "two",
                 "is_conceptual": False},
    },
}


@pytest.fixture
def db():
    s = make_session()
    yield s
    s.close()


@pytest.fixture
def student(db):
    s = Student(email="newcomer@example.edu", name="Nell Newcomer")
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def graph(db):
    """L01 + L02 prerequisites feeding an L04 goal (the live shape)."""
    for g in [
        LearningGoal(id="AST101-L01-celestial-sphere", title="Describe the celestial sphere model", description="d",
                     course="AST101", lecture_num=1),
        LearningGoal(id="AST101-L02-eclipse-conditions", title="State the conditions for an eclipse", description="d",
                     course="AST101", lecture_num=2),
        LearningGoal(id="AST101-L04-electromagnetic-spectrum", title="Order the electromagnetic spectrum", description="d",
                     course="AST101", lecture_num=4),
    ]:
        db.add(g)
    db.flush()
    db.add(GoalPrerequisite(from_goal_id="AST101-L01-celestial-sphere", to_goal_id="AST101-L04-electromagnetic-spectrum"))
    db.add(GoalPrerequisite(from_goal_id="AST101-L02-eclipse-conditions", to_goal_id="AST101-L04-electromagnetic-spectrum"))
    db.flush()


def _gate(student, db, lecture=4):
    with patch("tutor.agent.mastery._load_exam_bank", return_value=_BANK):
        return check_prerequisite_gate(student.id, "AST101", lecture, db)


# ---------------------------------------------------------------------------
# 1. Deferral snoozes the gate
# ---------------------------------------------------------------------------

def test_defer_snoozes_the_goal_then_next_prereq_is_not_gated_either(db, student, graph):
    first = _gate(student, db)
    assert first.id == "AST101-L01-celestial-sphere"      # earliest lecture first
    with patch("tutor.agent.mastery._load_exam_bank", return_value=_BANK):
        attempt = start_exam(student.id, first, db)
    assert attempt.status == "pending"

    state.defer_exam_attempt(db, attempt)
    assert attempt.status == "deferred" and attempt.deferred_at is not None

    # The deferred goal is skipped. Before the fix the gate re-fired here
    # (resuming these exact questions on the student's very next message).
    nxt = _gate(student, db)
    assert nxt is not None and nxt.id == "AST101-L02-eclipse-conditions", (
        "the OTHER unmastered prerequisite may still gate a code attempt"
    )
    # Defer that one too: lecture 4 is now free of gates for the day.
    with patch("tutor.agent.mastery._load_exam_bank", return_value=_BANK):
        second = start_exam(student.id, nxt, db)
    state.defer_exam_attempt(db, second)
    assert _gate(student, db) is None


def test_snooze_expires_and_resumes_the_same_questions(db, student, graph):
    with patch("tutor.agent.mastery._load_exam_bank", return_value=_BANK):
        attempt = start_exam(student.id, _gate(student, db), db)
    original_questions = attempt.questions_json
    state.defer_exam_attempt(db, attempt)
    assert _gate(student, db) is None or _gate(student, db).id != attempt.goal_id

    # Age the deferral past the snooze window.
    attempt.deferred_at = attempt.deferred_at - datetime.timedelta(hours=DEFER_SNOOZE_HOURS + 1)
    db.flush()
    g = _gate(student, db)
    assert g is not None and g.id == "AST101-L01-celestial-sphere"
    with patch("tutor.agent.mastery._load_exam_bank", return_value=_BANK):
        resumed = start_exam(student.id, g, db)
    assert resumed.id == attempt.id                # same attempt row
    assert resumed.status == "pending"
    assert resumed.questions_json == original_questions   # deferring never rerolls


def test_legacy_deferred_rows_without_timestamp_still_gate(db, student, graph):
    db.add(MasteryAttempt(student_id=student.id, goal_id="AST101-L01-celestial-sphere",
                          questions_json=json.dumps(_BANK["AST101-L01-celestial-sphere"]),
                          status="deferred", deferred_at=None))
    db.flush()
    assert _gate(student, db).id == "AST101-L01-celestial-sphere"


def test_snoozed_goal_ids_helper(db, student, graph):
    now = datetime.datetime(2026, 9, 4, 12, 0, 0)
    db.add(MasteryAttempt(student_id=student.id, goal_id="AST101-L01-celestial-sphere",
                          questions_json="{}", status="deferred",
                          deferred_at=now - datetime.timedelta(hours=2)))
    db.add(MasteryAttempt(student_id=student.id, goal_id="AST101-L02-eclipse-conditions",
                          questions_json="{}", status="deferred",
                          deferred_at=now - datetime.timedelta(hours=DEFER_SNOOZE_HOURS + 1)))
    db.flush()
    assert mastery._snoozed_goal_ids(student.id, db, now=now) == {"AST101-L01-celestial-sphere"}


# ---------------------------------------------------------------------------
# 2. gate_eligible
# ---------------------------------------------------------------------------

def test_envelope_gate_eligible_false_skips_the_gate_entirely(db, student, graph):
    with patch("tutor.agent.mastery.check_prerequisite_gate") as gate:
        gate.return_value = db.get(LearningGoal, "AST101-L01-celestial-sphere")
        eligible = assemble_envelope(student.id, "AST101", db, lecture_num=4, gate_eligible=True)
        question = assemble_envelope(student.id, "AST101", db, lecture_num=4, gate_eligible=False)
    assert eligible.gate_goal is not None
    assert question.gate_goal is None
    assert gate.call_count == 1, "an ineligible turn must not even consult the gate"
    # Scope is unaffected: the lecture 4 goal is still in play for the ceiling.
    assert "AST101-L04-electromagnetic-spectrum" in question.envelope.goals


def test_turn_gates_only_code_adjacent_messages():
    """The preflight wires gate_eligible = code_adjacent and not skip_gate;
    the exam intercept sets skip_gate (plan step 5.2 moved both out of
    run_turn)."""
    import inspect
    from tutor.agent.turn_stages import exam_intercept, preflight
    src = inspect.getsource(preflight.assemble_envelope)
    assert "gate_eligible = code_adjacent and not req.skip_gate" in src
    assert "gate_eligible=gate_eligible" in src
    # The two skip sites: after DEFER, and on the replay of a held question.
    assert inspect.getsource(exam_intercept).count("req.skip_gate = True") == 2


# ---------------------------------------------------------------------------
# 3. Move-tag leak guard
# ---------------------------------------------------------------------------

LEAK = "Good question — `*` means multiply here.\n\nBefore I explain anything though — **PROBE_PRIOR** — let me see where you're starting from."


def test_strip_move_tags_batch():
    out = strip_move_tags(LEAK)
    assert "PROBE_PRIOR" not in out
    assert "`*` means multiply here" in out and "let me see where you're starting from" in out
    for m in Move:
        assert m.value not in strip_move_tags(f"Opening move: [{m.value}] then `{m.value}` and {m.value}.")
    # Identifiers that merely contain a move name, ordinary bold, inline code
    # and a lone asterisk are untouched.
    keep = "Use `d * d;` and **Q1:** what is *d? STAR_INDEX_ORDER matters; PROBE_PRIOR_FOO too; a * b."
    assert strip_move_tags(keep) == keep
    assert strip_move_tags("") == ""


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 11, 1000])
def test_stream_gate_strips_move_tags_across_chunks(chunk):
    text = "Try **CHECK_UNDERSTANDING** now, then `ADVANCE` please. 2*3 = 6 and `d * d;` ok.\n```cpp\nint x = 5;\n```\nDone PROBE_PRIOR."
    g = StreamGate()
    out = "".join(g.feed(text[i:i + chunk]) for i in range(0, len(text), chunk)) + g.close()
    assert "CHECK_UNDERSTANDING" not in out and "ADVANCE" not in out and "PROBE_PRIOR" not in out
    assert out.startswith("Try now, then please. 2*3 = 6 and `d * d;` ok.")
    assert "```cpp\nint x = 5;\n```" in out          # fenced code untouched
    assert out.endswith("Done.")
    assert g.cut is False                              # not a detector cut


def test_stream_gate_keeps_bold_and_base64_holds_intact():
    text = "**Quick Mastery Check** on SELF-driven work: **Q1:** why? token: aGVsbG8gd29ybGQ= done."
    g = StreamGate()
    out = "".join(g.feed(ch) for ch in text) + g.close()
    assert out == text


def test_actor_prompt_forbids_writing_move_names():
    from tutor.agent import prompt_builder
    import inspect
    src = inspect.getsource(prompt_builder)
    assert "never write them in your reply" in src
