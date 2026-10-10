"""Task 2 tests: the envelope assembler (DB boundary) and its rendering.

Network-free. Uses an in-memory SQLite DB; check_prerequisite_gate is patched
so these tests exercise the assembler, not the gate (the gate is covered in
test_learning_goals).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tutor.agent.envelope import assemble_envelope
from tutor.agent.policy import EXAM_LOCKDOWN_RUNG, EXPLAIN_MODE_RUNG, Envelope
from tutor.agent.prompt_builder import render_envelope
from tutor.db.models import (
    LearningGoal,
    MasteryAttempt,
    Setting,
    Student,
    StudentProgress,
)
from tutor.utctime import utcnow
from tests.helpers import make_session

_NO_GATE = "tutor.agent.mastery.check_prerequisite_gate"


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="a@example.edu", name="A"))
    s.add(LearningGoal(id="G1", title="Telescopes", description="", course="AST101", lecture_num=4))
    s.add(LearningGoal(id="G2", title="Light", description="", course="AST101", lecture_num=4))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# assemble_envelope
# ---------------------------------------------------------------------------

def test_scope_from_lecture_goals_and_default_ceiling(db):
    with patch(_NO_GATE, return_value=None):
        bundle = assemble_envelope(1, "AST101", db, lecture_num=4)
    assert set(bundle.envelope.goals) == {"G1", "G2"}
    assert bundle.envelope.max_rung == 5  # both locked -> mastery 0 -> H5
    assert bundle.goal_titles["G1"] == "Telescopes"
    assert bundle.gate_goal is None


def test_mastered_goal_raises_mastery_but_weakest_wins(db):
    db.add(StudentProgress(student_id=1, goal_id="G1", status="mastered"))
    db.commit()
    with patch(_NO_GATE, return_value=None):
        bundle = assemble_envelope(1, "AST101", db, lecture_num=4)
    # G1 mastered (0.9 -> H2) but G2 still locked (0.0 -> H5); weakest wins.
    assert bundle.envelope.max_rung == 5


def test_pending_exam_creates_lockdown(db):
    db.add(MasteryAttempt(
        student_id=1, goal_id="G1", questions_json="{}", status="pending",
        attempts=0, started_at=utcnow(),
    ))
    db.commit()
    with patch(_NO_GATE, return_value=None):
        bundle = assemble_envelope(1, "AST101", db, lecture_num=4)
    assert bundle.envelope.exam_lockdown == ["G1"]
    assert bundle.envelope.max_rung == EXAM_LOCKDOWN_RUNG


def test_explain_mode_setting_lifts_ceiling(db):
    db.add(Setting(key="explain_mode", value="AST101"))
    db.commit()
    with patch(_NO_GATE, return_value=None):
        bundle = assemble_envelope(1, "AST101", db, lecture_num=4)
    assert bundle.envelope.max_rung == EXPLAIN_MODE_RUNG


def test_explain_mode_other_course_does_not_apply(db):
    db.add(Setting(key="explain_mode", value="AST102"))
    db.commit()
    with patch(_NO_GATE, return_value=None):
        bundle = assemble_envelope(1, "AST101", db, lecture_num=4)
    assert bundle.envelope.max_rung == 5  # toggle is for AST102, not this course


def test_gate_goal_folded_into_scope_and_returned(db):
    gate = LearningGoal(id="G0", title="Celestial sphere", description="", course="AST101", lecture_num=1)
    db.add(gate)
    db.commit()
    with patch(_NO_GATE, return_value=gate):
        bundle = assemble_envelope(1, "AST101", db, lecture_num=4)
    assert bundle.gate_goal is gate
    assert "G0" in bundle.envelope.goals
    assert bundle.goal_titles["G0"] == "Celestial sphere"


def test_no_lecture_yields_empty_scope(db):
    with patch(_NO_GATE, return_value=None):
        bundle = assemble_envelope(1, "AST101", db, lecture_num=None)
    assert bundle.envelope.goals == []
    assert bundle.gate_goal is None


# ---------------------------------------------------------------------------
# render_envelope
# ---------------------------------------------------------------------------

def _env(max_rung=3, goals=("G1",), lockdown=(), must_ground=True, allow_exercise=True):
    return Envelope(
        max_rung=max_rung,
        goals=list(goals),
        exam_lockdown=list(lockdown),
        must_ground=must_ground,
        allow_exercise=allow_exercise,
        escalate_review=False,
        reason="test",
    )


def test_render_names_ceiling_and_goal_titles():
    out = render_envelope(_env(max_rung=3, goals=("G1",)), {"G1": "Telescopes"})
    assert "H3" in out
    assert "G1 (Telescopes)" in out


def test_render_uses_id_when_title_missing():
    out = render_envelope(_env(goals=("G9",)), {})
    assert "G9" in out


def test_render_includes_lockdown_and_grounding_and_no_exercise():
    out = render_envelope(_env(lockdown=("G1",), must_ground=True, allow_exercise=False), {"G1": "Telescopes"})
    assert "EXAM LOCKDOWN" in out
    assert "Ground course claims" in out
    assert "Do not hand out a new exercise" in out


def test_render_grounding_forbids_citing_unretrieved_sources():
    # 2026-06-14 G2 fix: the actor was citing textbooks/slides it had not
    # retrieved -> must_ground revise. The instruction now forbids that.
    out = render_envelope(_env(must_ground=True), {})
    assert "Do NOT name or cite" in out
    assert "without attributing it to a source" in out


def test_render_h7_omits_no_compilable_code_line():
    out = render_envelope(_env(max_rung=7), {})
    assert "compilable" not in out


def test_render_below_h7_forbids_compilable_code_emphatically():
    out = render_envelope(_env(max_rung=3), {})
    assert "compilable" in out
    assert "not even a corrected line" in out   # emphatic: no signature/body either


def test_render_low_rung_forbids_giving_away_the_answer():
    # 2026-06-14: the actor was stating the exact bug ("<= vs <") at H4 -> auditor
    # over-help. At <= H4 it must nudge, not reveal the bug/fix.
    out = render_envelope(_env(max_rung=4), {})
    assert "Do NOT give away the answer" in out
    assert "ask one leading question" in out


def test_render_h5_allows_more_than_a_nudge():
    # At H5+ (worked examples / pseudocode) the anti-spoiler nudge rule lifts.
    out = render_envelope(_env(max_rung=5), {})
    assert "Do NOT give away the answer" not in out
