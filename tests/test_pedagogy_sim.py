"""Session 25: pedagogy sim metrics + preference pairing.

Scripted-trajectory metrics (move distribution, elicitation rate, depth-to-
convergence) and the pure pairing used by the TA preference sampler.
"""
from __future__ import annotations

from tutor.agent.policy import Intent, Move
from tests.sim import pedagogy
from pipeline.sample_preferences import make_pairs


# ---------------------------------------------------------------------------
# reference_move encodes the playbook signals
# ---------------------------------------------------------------------------

def test_passive_student_gets_elicitation():
    g = pedagogy._goal(attempts=0, questions=3, passive=1)
    assert pedagogy.reference_move(g, Intent.QUESTION) in pedagogy.ELICITATION_MOVES


def test_stuck_student_gets_modality_switch():
    g = pedagogy._goal(attempts=3, questions=0, passive=0,
                       scores=(0.25, 0.25, 0.25), repeated=True)
    assert pedagogy.reference_move(g, Intent.ATTEMPT) == Move.MODALITY_SWITCH


def test_cruising_student_advances():
    g = pedagogy._goal(attempts=3, questions=0, passive=0, scores=(0.9, 1.0, 1.0))
    assert pedagogy.reference_move(g, Intent.ATTEMPT) == Move.ADVANCE


def test_review_due_takes_priority():
    g = pedagogy._goal(attempts=0, questions=3, passive=1)
    assert pedagogy.reference_move(g, Intent.QUESTION, {"review_due": True}) == Move.SUGGEST_REVIEW


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def test_pedagogy_metrics():
    report = pedagogy.run_pedagogy_sim()
    # Passive students are mostly put to work, not told.
    assert report.passive_elicitation_rate >= 0.5
    # Stuck students see at least one modality switch.
    assert report.stuck_modality_rate > 0
    # Cruising converges within the scripted window.
    assert report.depth_to_convergence == 3
    assert report.move_distribution  # non-empty


def test_render_pedagogy_report():
    md = pedagogy.render_report(pedagogy.run_pedagogy_sim(), generated_at="2026-06-12")
    assert "# Pedagogy sim report" in md
    assert "elicitation rate" in md
    assert "Move distribution" in md


# ---------------------------------------------------------------------------
# Preference pairing (pure)
# ---------------------------------------------------------------------------

def _item(goal, resp):
    return {"goal_id": goal, "rung": 3, "context": "ctx", "response": resp}


def test_make_pairs_groups_by_goal():
    items = [
        _item("G1", "a"), _item("G1", "b"),
        _item("G2", "c"), _item("G2", "d"),
        _item("G3", "e"),  # singleton -> no pair
    ]
    pairs = make_pairs(items, n=10, seed=0)
    assert len(pairs) == 2
    goals = {p["goal_id"] for p in pairs}
    assert goals == {"G1", "G2"}
    for p in pairs:
        assert p["response_a"] != p["response_b"]


def test_make_pairs_respects_n_and_is_deterministic():
    items = [_item("G1", str(i)) for i in range(10)]
    a = make_pairs(items, n=2, seed=0)
    b = make_pairs(items, n=2, seed=0)
    assert len(a) == 2
    assert [p["pair_id"] for p in a] == [p["pair_id"] for p in b]


def test_make_pairs_empty_when_no_duplicates():
    items = [_item("G1", "a"), _item("G2", "b")]
    assert make_pairs(items, n=5) == []
