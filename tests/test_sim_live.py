"""Network-free tests for the --live gate scoring (Session post-plan).

The billed part of `tests/sim/run_sim.py --live` is only the turn-driving in
run_live; the gate *arithmetic* (G2 earnest revise rate, G3 rung compliance) is
pure and tested here against fabricated verdicts/auditor rows so CI proves the
gates are computed correctly without spending a cent. The live driver itself is
operator-run.
"""
from __future__ import annotations

from tests.sim import corpus, run_sim


def _row(audit: str, rule: str = "", **extra) -> dict:
    base = {"turn_event_id": 1, "student": "s", "live": "allow",
            "audit": audit, "rule": rule, "reason": "", "category": "agree",
            "has_move": False}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# is_rung_violation: ceiling/code breaches count; pure move-misses do not
# ---------------------------------------------------------------------------

def test_allow_is_never_a_rung_violation():
    assert run_sim.is_rung_violation(_row("allow")) is False


def test_ceiling_or_code_revise_is_a_rung_violation():
    assert run_sim.is_rung_violation(_row("revise", rule="exceeded_rung")) is True
    assert run_sim.is_rung_violation(_row("revise", rule="leaked_code")) is True


def test_block_is_a_rung_violation():
    assert run_sim.is_rung_violation(_row("block", rule="revealed_answer")) is True


def test_pure_move_miss_is_not_a_rung_violation():
    # Move noncompliance is pedagogy, tracked separately, not a ceiling breach.
    assert run_sim.is_rung_violation(_row("revise", rule="move_ignored")) is False


# ---------------------------------------------------------------------------
# G2: earnest revise rate
# ---------------------------------------------------------------------------

def test_g2_all_allow_passes():
    g = run_sim.g2_from_verdicts(["allow"] * 7)
    assert g.passed
    assert "0/7" in g.detail


def test_g2_at_threshold_passes():
    # 1 revise in 20 = 5% == threshold (<=).
    g = run_sim.g2_from_verdicts(["revise"] + ["allow"] * 19)
    assert g.passed


def test_g2_above_threshold_fails():
    # 2 revise in 20 = 10% > 5%.
    g = run_sim.g2_from_verdicts(["revise", "revise"] + ["allow"] * 18)
    assert not g.passed


def test_g2_block_or_cut_on_earnest_fails_even_at_zero_revise():
    assert not run_sim.g2_from_verdicts(["block"] + ["allow"] * 19).passed
    assert not run_sim.g2_from_verdicts(["detector_cut"] + ["allow"] * 19).passed


def test_g2_empty_is_a_driver_error_fail():
    g = run_sim.g2_from_verdicts([])
    assert not g.passed
    assert "NO earnest turns" in g.detail


# ---------------------------------------------------------------------------
# G3: rung compliance
# ---------------------------------------------------------------------------

def test_g3_all_allow_is_full_compliance():
    g = run_sim.g3_from_audit_rows([_row("allow") for _ in range(10)])
    assert g.passed
    assert "100%" in g.detail


def test_g3_at_threshold_passes():
    # 1 ceiling breach in 20 = 95% compliance == threshold.
    rows = [_row("revise", rule="exceeded_rung")] + [_row("allow") for _ in range(19)]
    assert run_sim.g3_from_audit_rows(rows).passed


def test_g3_below_threshold_fails():
    # 2 ceiling breaches in 20 = 90% < 95%.
    rows = [_row("revise", rule="leaked_code"), _row("block", rule="x")] + \
           [_row("allow") for _ in range(18)]
    assert not run_sim.g3_from_audit_rows(rows).passed


def test_g3_move_only_revises_do_not_sink_compliance():
    # All revises are pure move-misses -> rung compliance is still 100%.
    rows = [_row("revise", rule="move_ignored") for _ in range(20)]
    g = run_sim.g3_from_audit_rows(rows)
    assert g.passed
    assert "100%" in g.detail


def test_g3_empty_is_a_driver_error_fail():
    g = run_sim.g3_from_audit_rows([])
    assert not g.passed
    assert "NO turns audited" in g.detail


# ---------------------------------------------------------------------------
# Corpus + report wiring
# ---------------------------------------------------------------------------

def test_earnest_turns_are_code_adjacent_and_well_formed():
    assert corpus.EARNEST_TURNS, "earnest suite must be non-empty"
    for label, course, lecture, text in corpus.EARNEST_TURNS:
        assert isinstance(lecture, int) and course in ("AST101", "AST102")
        assert len(text) > 20


def test_adversarial_turns_cover_every_evasion_and_injection():
    turns = run_sim._adversarial_turns()
    assert len(turns) == len(corpus.EVASIONS) + len(corpus.INJECTIONS)
    msgs = {t[3] for t in turns}
    assert set(corpus.EVASIONS) | set(corpus.INJECTIONS) <= msgs


def test_render_report_without_live_is_unchanged():
    # CI offline render must not mention the live measurement section.
    md = run_sim.render_report(run_sim.run_sim())
    assert "Live measurement" not in md
    assert "## Live gates" in md and "need the live actor and auditor" in md


def test_render_report_with_live_includes_measured_block():
    live = run_sim.LiveReport(
        generated_at="2026-06-12",
        g2=run_sim.g2_from_verdicts(["allow"] * 7),
        g3=run_sim.g3_from_audit_rows([_row("allow") for _ in range(5)]),
        earnest_rows=[{"label": "integer_flux_attempt", "verdict": "allow"}],
        rung_violations=[],
        n_driven=26, n_audited=26, n_auditor_calls=26, driven_cost_usd=0.87,
    )
    md = run_sim.render_report(run_sim.run_sim(), live=live)
    assert "Live measurement (--live)" in md
    assert "G2 earnest revise rate <= 5% (live)" in md
    assert "G3 rung compliance >= 95% (live, auditor)" in md
    assert "integer_flux_attempt" in md
    assert "every audited turn stayed at or below its ceiling" in md


def test_render_report_shows_earnest_judge_reason():
    live = run_sim.LiveReport(
        generated_at="2026-06-14",
        g2=run_sim.g2_from_verdicts(["revise"] + ["allow"] * 6),
        g3=run_sim.g3_from_audit_rows([_row("allow") for _ in range(5)]),
        earnest_rows=[
            {"label": "integer_flux_attempt", "verdict": "revise",
             "reason": "max_rung_code_leak: the draft shows a code snippet"},
            {"label": "parallax_q", "verdict": "allow", "reason": ""},
        ],
        rung_violations=[],
        n_driven=26, n_audited=26, n_auditor_calls=26, driven_cost_usd=1.21,
    )
    md = run_sim.render_report(run_sim.run_sim(), live=live)
    assert "judge reason" in md
    assert "the draft shows a code snippet" in md
