"""Task 2 policy-core tests: the per-turn envelope, table-driven.

Pure and network-free. Covers every mastery band x exam x toggle combination,
plus the Section-4 purity invariants (policy.py imports no anthropic; the
compute_envelope signature carries no bare str fields).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from tutor.agent.policy import (
    DeterministicFacts,
    Envelope,
    GoalState,
    Intent,
    StudentState,
    Toggles,
    compute_envelope,
    rung_for_mastery,
    ATTEMPT_RUNG_FLOOR,
    EXAM_LOCKDOWN_RUNG,
    EXPLAIN_MODE_RUNG,
    RESTRICTED_RUNG,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _state(goal_id="G1", mastery=0.0, exam_active=False, status="unlocked"):
    return StudentState(
        goals=(GoalState(goal_id=goal_id, mastery=mastery, exam_active=exam_active, status=status),)
    )


def _facts(scope=("G1",), gate=None, code_adjacent=False, on_live=False):
    return DeterministicFacts(
        scope_goal_ids=tuple(scope),
        gate_goal_id=gate,
        code_adjacent=code_adjacent,
        on_live_exercise=on_live,
    )


def _env(state, facts, intent=Intent.QUESTION, toggles=None):
    return compute_envelope(state, intent, facts, toggles or Toggles())


# ---------------------------------------------------------------------------
# Rung bands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mastery,expected",
    [
        (0.95, 2), (0.85, 2),
        (0.84, 3), (0.70, 3),
        (0.69, 4), (0.40, 4),
        (0.39, 5), (0.00, 5),
    ],
)
def test_rung_for_mastery_bands(mastery, expected):
    assert rung_for_mastery(mastery) == expected


@pytest.mark.parametrize(
    "mastery,expected",
    [(0.95, 2), (0.75, 3), (0.5, 4), (0.1, 5)],
)
def test_envelope_base_ceiling_from_mastery(mastery, expected):
    env = _env(_state(mastery=mastery), _facts())
    assert env.max_rung == expected


# ---------------------------------------------------------------------------
# Exam lockdown
# ---------------------------------------------------------------------------

def test_exam_lockdown_caps_rung_even_for_low_mastery():
    env = _env(_state(mastery=0.1, exam_active=True), _facts())
    assert env.max_rung == EXAM_LOCKDOWN_RUNG
    assert env.exam_lockdown == ["G1"]


def test_exam_lockdown_only_lists_in_scope_goals():
    state = StudentState(goals=(
        GoalState("G1", mastery=0.1, exam_active=True),
        GoalState("G2", mastery=0.1, exam_active=True),
    ))
    env = _env(state, _facts(scope=("G1",)))
    assert env.exam_lockdown == ["G1"]  # G2 active but out of scope


def test_explain_mode_does_not_override_exam_lockdown():
    env = _env(
        _state(mastery=0.1, exam_active=True),
        _facts(),
        toggles=Toggles(explain_mode=True),
    )
    assert env.max_rung == EXAM_LOCKDOWN_RUNG


# ---------------------------------------------------------------------------
# Explain mode
# ---------------------------------------------------------------------------

def test_explain_mode_lifts_ceiling_to_h7():
    env = _env(_state(mastery=0.9), _facts(), toggles=Toggles(explain_mode=True))
    assert env.max_rung == EXPLAIN_MODE_RUNG


# ---------------------------------------------------------------------------
# Intent (P4: aggressive intent must not tighten the rung)
# ---------------------------------------------------------------------------

def test_solution_demand_does_not_change_rung():
    state, facts = _state(mastery=0.5), _facts()
    q = _env(state, facts, intent=Intent.QUESTION)
    sd = _env(state, facts, intent=Intent.SOLUTION_DEMAND)
    assert sd.max_rung == q.max_rung


@pytest.mark.parametrize(
    "intent,escalates",
    [
        (Intent.QUESTION, False),
        (Intent.ATTEMPT, False),
        (Intent.SOLUTION_DEMAND, True),
        (Intent.EXAM_LEAK, True),
        (Intent.INJECTION, True),
    ],
)
def test_escalate_review_by_intent(intent, escalates):
    env = _env(_state(mastery=0.5), _facts(), intent=intent)
    assert env.escalate_review is escalates


def test_escalate_review_on_exam_lockdown():
    env = _env(_state(mastery=0.5, exam_active=True), _facts(), intent=Intent.QUESTION)
    assert env.escalate_review is True


# ---------------------------------------------------------------------------
# Attempt rung floor (G2: don't over-revise genuine debugging help)
# ---------------------------------------------------------------------------

def test_attempt_floors_low_ceiling_so_debugging_help_fits():
    # A mastered goal gives H2 (here a non-code question); a genuine attempt
    # floors it to H4 so a targeted debugging hint is within the ceiling.
    base = _env(_state(mastery=0.95), _facts(code_adjacent=False), intent=Intent.QUESTION)
    attempt = _env(_state(mastery=0.95), _facts(code_adjacent=True), intent=Intent.ATTEMPT)
    assert base.max_rung == 2
    assert attempt.max_rung == ATTEMPT_RUNG_FLOOR == 4


def test_attempt_floor_never_lowers_a_higher_ceiling():
    # A struggling student already gets H5; the floor must not pull them down.
    env = _env(_state(mastery=0.1), _facts(code_adjacent=True), intent=Intent.ATTEMPT)
    assert env.max_rung == 5


def test_solution_demand_does_not_get_the_attempt_floor():
    # P4: an aggressive demand is routed kindly, not rewarded with more help.
    sd = _env(_state(mastery=0.95), _facts(code_adjacent=True), intent=Intent.SOLUTION_DEMAND)
    assert sd.max_rung == 2  # base ceiling, unfloored


def test_code_adjacent_question_gets_the_floor():
    # 2026-06-14 live fix: "here's my code, what's wrong?" classifies as QUESTION
    # as often as ATTEMPT, so a code-adjacent QUESTION now floors to H4 too (else
    # the actor's verbal debugging help is revised as over the H2 ceiling).
    q = _env(_state(mastery=0.95), _facts(code_adjacent=True), intent=Intent.QUESTION)
    assert q.max_rung == ATTEMPT_RUNG_FLOOR == 4


def test_non_code_question_keeps_the_mastery_ceiling():
    # A pure conceptual question (no code) is not floored.
    q = _env(_state(mastery=0.95), _facts(code_adjacent=False), intent=Intent.QUESTION)
    assert q.max_rung == 2


def test_exam_lockdown_still_caps_an_attempt_below_the_floor():
    env = _env(_state(mastery=0.95, exam_active=True),
               _facts(code_adjacent=True), intent=Intent.ATTEMPT)
    assert env.max_rung == EXAM_LOCKDOWN_RUNG  # H1, lockdown wins over the H4 floor


def test_conduct_restriction_still_caps_an_attempt_below_the_floor():
    facts = DeterministicFacts(scope_goal_ids=("G1",), code_adjacent=True, restricted=True)
    env = compute_envelope(_state(mastery=0.95), Intent.ATTEMPT, facts, Toggles())
    assert env.max_rung == RESTRICTED_RUNG  # H1, restriction wins over the H4 floor


# ---------------------------------------------------------------------------
# Grounding, exercises, scope
# ---------------------------------------------------------------------------

def test_must_ground_true_when_scope_present():
    assert _env(_state(), _facts(scope=("G1",))).must_ground is True


def test_must_ground_true_on_code_adjacent_even_without_scope():
    env = _env(StudentState(), _facts(scope=(), code_adjacent=True))
    assert env.must_ground is True


def test_must_ground_false_for_off_topic_chitchat():
    env = _env(StudentState(), _facts(scope=(), code_adjacent=False))
    assert env.must_ground is False


def test_allow_exercise_suppressed_during_lockdown():
    env = _env(_state(mastery=0.5, exam_active=True), _facts(), toggles=Toggles(allow_exercise=True))
    assert env.allow_exercise is False


def test_allow_exercise_follows_toggle_when_no_lockdown():
    on = _env(_state(mastery=0.5), _facts(), toggles=Toggles(allow_exercise=True))
    off = _env(_state(mastery=0.5), _facts(), toggles=Toggles(allow_exercise=False))
    assert on.allow_exercise is True and off.allow_exercise is False


def test_gate_goal_folded_into_scope():
    env = _env(StudentState(), _facts(scope=(), gate="G-prereq"))
    assert "G-prereq" in env.goals


def test_weakest_goal_sets_ceiling_when_multiple_in_scope():
    state = StudentState(goals=(
        GoalState("G1", mastery=0.9),   # strong -> rung 2
        GoalState("G2", mastery=0.2),   # weak   -> rung 5
    ))
    env = _env(state, _facts(scope=("G1", "G2")))
    assert env.max_rung == 5  # meet the student at their weakest goal


def test_envelope_is_frozen():
    env = _env(_state(), _facts())
    assert isinstance(env, Envelope)
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.max_rung = 7  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Section-4 purity invariants
# ---------------------------------------------------------------------------

def test_policy_module_does_not_import_anthropic():
    src = Path(inspect.getfile(compute_envelope)).read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "anthropic" not in imported


def test_compute_envelope_signature_has_no_bare_str_params():
    sig = inspect.signature(compute_envelope)
    for name, param in sig.parameters.items():
        ann = param.annotation
        ann_str = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
        assert ann_str != "str", f"param {name} must not be a bare str (P2)"
