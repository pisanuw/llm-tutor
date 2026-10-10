"""Faded worked examples (pedagogy item 10): the per-goal ladder
WORKED_ANALOGY -> COMPLETION_PROBLEM -> FULL_PROBLEM is driven by mastery
evidence, and the two new moves obey the rung ceiling like every other move."""

from __future__ import annotations

from tutor.agent import policy
from tutor.agent.policy import (
    FADING_MOVE, MOVE_GUIDANCE, MOVE_MIN_RUNG, Move, fading_stage, merge_move,
)


def test_stage_no_evidence_starts_worked():
    assert fading_stage([], []) == "worked"


def test_stage_advances_after_worked_example_even_without_grades():
    assert fading_stage([], [2, 5]) == "completion"


def test_stage_from_recent_scores():
    assert fading_stage([0.0, 0.25], [5]) == "worked"      # mean 0.125
    assert fading_stage([0.5, 0.75], [5]) == "completion"  # mean 0.625
    assert fading_stage([1.0, 0.75], [5]) == "full"        # mean 0.875
    # Only the last FADE_RECENT scores count: early failures are forgiven.
    assert fading_stage([0.0, 0.0, 1.0, 1.0], []) == "full"


def test_exam_pass_is_full_regardless_of_scores():
    assert fading_stage([0.0], [], exam_passes=1) == "full"


def test_every_stage_maps_to_a_guided_move():
    for stage in ("worked", "completion", "full"):
        move = FADING_MOVE[stage]
        assert isinstance(move, Move)
        assert move in MOVE_GUIDANCE and MOVE_GUIDANCE[move]


def _env(max_rung: int) -> policy.Envelope:
    return policy.Envelope(max_rung=max_rung, goals=["AST101-1-x"], exam_lockdown=[],
                           must_ground=False, allow_exercise=True,
                           escalate_review=False, reason="test")


def test_completion_problem_respects_ceiling_full_problem_does_not_need_one():
    assert MOVE_MIN_RUNG[Move.COMPLETION_PROBLEM] == 5
    assert Move.FULL_PROBLEM not in MOVE_MIN_RUNG
    low = _env(3)
    # A low ceiling silently drops the showing move but accepts the unaided one.
    assert merge_move(low, Move.COMPLETION_PROBLEM).move is None
    assert merge_move(low, Move.FULL_PROBLEM).move == Move.FULL_PROBLEM
    assert merge_move(_env(6), Move.COMPLETION_PROBLEM).move == Move.COMPLETION_PROBLEM


def test_strategist_facts_include_fading_stage():
    from tutor.agent.strategist import _facts
    from tutor.agent.trajectory import GoalTrajectory, Trajectory

    g = GoalTrajectory(goal_id="AST101-1-x", turns=2, rungs_used=(5,), attempts=1, questions=1,
                       passive=0, repeated_error=False, exam_passes=0, exam_fails=0,
                       graded_scores=(0.5, 0.75))
    traj = Trajectory(session_turns=2, attempts=1, questions=1, passive=0,
                      minutes_since_last_attempt=None, goals=(g,))
    text = _facts(_env(6), traj, policy.Intent.QUESTION)
    assert "fading stage=completion" in text
