"""Session 10 tests: the judge learns moves (S18); restrict gets teeth (S24).

Exit gate: judge tests for move compliance; restricted-student envelope test.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tutor.agent import conduct, judge, state
from tutor.agent.envelope import assemble_envelope
from tutor.agent.policy import (
    DeterministicFacts,
    Envelope,
    Intent,
    Move,
    RESTRICTED_RUNG,
    StudentState,
    Toggles,
    compute_envelope,
    merge_move,
)
from tutor.db.models import LearningGoal, Student, TurnEvent
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Celestial sphere", description="", course="AST101", lecture_num=1))
    s.commit()
    yield s
    s.close()


def _envelope(move=None, max_rung=4):
    env = Envelope(
        max_rung=max_rung, goals=["G1"], exam_lockdown=[], must_ground=True,
        allow_exercise=True, escalate_review=False, reason="t",
    )
    return merge_move(env, move) if move else env


def _judge_client(verdict="allow"):
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps({"verdict": verdict, "rule": "move", "reason": "ignored the move"})
    resp = MagicMock(content=[block])
    resp.usage = MagicMock(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
    client.messages.create.return_value = resp
    return client


# ---------------------------------------------------------------------------
# Judge: move compliance (S18)
# ---------------------------------------------------------------------------

def test_judge_input_names_the_move_and_its_guidance():
    client = _judge_client()
    judge.judge(_envelope(move=Move.ELICIT_PREDICTION), "a draft", [], client=client)
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    # The judge sees the move and the guidance the policy defines for it, from
    # the policy's own table rather than a pinned word (plan step 6.2a, F16).
    from tutor.agent.policy import MOVE_GUIDANCE

    assert f"move: {Move.ELICIT_PREDICTION.value} ({MOVE_GUIDANCE[Move.ELICIT_PREDICTION]})" in sent


def test_judge_input_omits_move_line_when_none():
    client = _judge_client()
    judge.judge(_envelope(), "a draft", [], client=client)
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "move:" not in sent


def test_judge_rubric_treats_move_noncompliance_as_revise_never_block():
    sys_prompt = judge._SYSTEM
    assert "ATTEMPT that move" in sys_prompt
    assert "NEVER block" in sys_prompt
    assert "Move noncompliance alone is never block" in sys_prompt


def test_judge_returns_revise_for_ignored_move():
    client = _judge_client(verdict="revise")
    v = judge.judge(_envelope(move=Move.SELF_EXPLAIN), "draft that ignores it", [], client=client)
    assert v.verdict == "revise"
    assert v.rule == "move"


# ---------------------------------------------------------------------------
# Restriction: the policy core caps at H1, nothing lifts it
# ---------------------------------------------------------------------------

def test_restricted_facts_cap_the_ceiling():
    env = compute_envelope(
        StudentState(), Intent.QUESTION,
        DeterministicFacts(scope_goal_ids=("G1",), restricted=True),
        Toggles(),
    )
    assert env.max_rung == RESTRICTED_RUNG
    assert "conduct restriction" in env.reason


def test_explain_mode_cannot_lift_a_restriction():
    env = compute_envelope(
        StudentState(), Intent.QUESTION,
        DeterministicFacts(scope_goal_ids=("G1",), restricted=True),
        Toggles(explain_mode=True),
    )
    assert env.max_rung == RESTRICTED_RUNG


def test_content_moves_cannot_attach_under_restriction():
    env = compute_envelope(
        StudentState(), Intent.QUESTION,
        DeterministicFacts(scope_goal_ids=("G1",), restricted=True),
        Toggles(),
    )
    assert merge_move(env, Move.WORKED_ANALOGY, "x").move is None
    assert merge_move(env, Move.LOCATED_OBSERVATION, "x").move is None
    # Pure-question moves still work at H1.
    assert merge_move(env, Move.SELF_EXPLAIN, "x").move == Move.SELF_EXPLAIN


# ---------------------------------------------------------------------------
# Restriction: end to end through trusted state
# ---------------------------------------------------------------------------

def test_assemble_envelope_reads_the_restriction_flag(db):
    db.get(Student, 1).restricted = True
    db.commit()
    bundle = assemble_envelope(1, "AST101", db, lecture_num=1)
    assert bundle.envelope.max_rung == RESTRICTED_RUNG

    state.set_student_restricted(db, 1, False)   # instructor clears it
    db.commit()
    bundle = assemble_envelope(1, "AST101", db, lecture_num=1)
    assert bundle.envelope.max_rung > RESTRICTED_RUNG


def test_second_conduct_strike_sets_the_flag(db):
    assert conduct.conduct_response(db, 1, Intent.HOSTILE) == conduct.WARN_MESSAGE
    assert db.get(Student, 1).restricted is False   # warn only

    assert conduct.conduct_response(db, 1, Intent.ABUSIVE) == conduct.RESTRICT_MESSAGE
    assert db.get(Student, 1).restricted is True    # teeth

    # The flag change is observable as a 'restriction' TurnEvent.
    ev = db.query(TurnEvent).filter_by(kind="restriction").one()
    assert json.loads(ev.verdicts_json)["restricted"] is True


def test_clearing_does_not_feed_the_conduct_counter(db):
    # A cleared restriction must not make the NEXT offence count as repeat.
    state.set_student_restricted(db, 1, True)
    state.set_student_restricted(db, 1, False)
    db.commit()
    # First offence after the clear: warn, not restrict.
    assert conduct.conduct_response(db, 1, Intent.HOSTILE) == conduct.WARN_MESSAGE
    assert db.get(Student, 1).restricted is False
