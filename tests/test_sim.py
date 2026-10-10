"""Session 24 (Task 8): the adversarial sim harness gates.

These run the offline, deterministic gates in CI. The live-only gates (earnest
revise rate, rung compliance) are operator-measured; what is tested here is
that the runner says so whenever the last live run does not describe the
supervision code (plan step 6.2b, finding F12).
"""
from __future__ import annotations

import pytest

from tutor.agent import detector
from tests.sim import corpus, run_sim


# ---------------------------------------------------------------------------
# G1: no solution reaches the student
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("strict", [True, False], ids=["no-code-rung", "code-rung"])
def test_no_reveal_or_rewrite_of_one_leaks(strict):
    leaked = [name for name, draft, leak in corpus.reveal_cases(strict)
              if run_sim.leaks(draft, leak, strict=strict)]
    assert leaked == [], f"these solutions reached the student: {leaked}"


@pytest.mark.parametrize("strict", [True, False], ids=["no-code-rung", "code-rung"])
def test_gate_no_reveals_passes(strict):
    g = run_sim.gate_no_reveals(strict)
    assert g.passed, g.detail


def test_every_case_shows_its_solution_before_redaction():
    """The oracle is not vacuous: each case's leak is visible in the draft as
    written, so a case can only pass by being cut."""
    cases = corpus.reveal_cases(True) + [b[:3] for b in corpus.BLIND_SPOT_DRAFTS]
    hidden = [name for name, draft, leak in cases if run_sim.visible(leak) not in run_sim.visible(draft)]
    assert hidden == []
    assert len(corpus.reveal_cases(True)) > 80 and len(corpus.reveal_cases(False)) > 40


def test_the_oracle_does_not_ask_the_detector():
    """The old check asked the detector whether it still saw a solution after
    redaction, so a reveal it missed counted as cut. A 4-line solution on a
    code rung is such a reveal: not flagged, and it reaches the student."""
    name, draft, leak = corpus.SHORT_REVEAL_DRAFTS[0]
    assert not detector.scan(draft).violating
    assert run_sim.leaks(draft, leak, strict=False)
    assert not run_sim.leaks(draft, leak, strict=True)      # a no-code rung cuts it


def test_a_leak_through_the_stream_alone_is_caught(monkeypatch):
    """The streaming gate is checked on its own: if it passed a block the batch
    redaction cuts, the case leaks."""
    monkeypatch.setattr(detector.StreamGate, "_emit_block", lambda self, block: block)
    name, draft, leak = corpus.REVEAL_DRAFTS[0]
    assert leak not in detector.redact(draft)
    assert run_sim.leaks(draft, leak, strict=False)


# ---------------------------------------------------------------------------
# G2 proxy: earnest tutoring is not over-blocked
# ---------------------------------------------------------------------------

def test_earnest_drafts_not_cut():
    for name, draft in corpus.EARNEST_DRAFTS:
        assert not detector.scan(draft).violating, f"earnest draft '{name}' was cut"


def test_gate_earnest_passes():
    assert run_sim.gate_earnest_not_cut().passed


# ---------------------------------------------------------------------------
# G4: exam grader resists injection and fails closed
# ---------------------------------------------------------------------------

def test_gate_exam_injection_passes():
    g = run_sim.gate_exam_injection()
    assert g.passed, g.detail


def test_model_asserted_pass_is_ignored():
    # passed must derive from the score, not the model's "passed" field.
    assert run_sim._passes_with_grader({"score": 0, "passed": True}) is False


def test_grader_outage_fails_closed():
    assert run_sim._passes_with_grader(None, raises=RuntimeError("boom")) is False


def test_legitimate_high_score_does_pass():
    # Sanity: a real score >= threshold still passes (the gate is not vacuous).
    assert run_sim._passes_with_grader({"score": 3, "passed": True}) is True


# ---------------------------------------------------------------------------
# Blind spots: known misses, each a strict xfail (S11)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="a known detector blind spot (S11); the live judge reads these turns")
@pytest.mark.parametrize("name, draft, leak, rungs", corpus.BLIND_SPOT_DRAFTS,
                         ids=[b[0] for b in corpus.BLIND_SPOT_DRAFTS])
def test_blind_spot_is_cut(name, draft, leak, rungs):
    """What the detector should do and does not. An "every" blind spot is
    checked where the detector is strictest, a "code" one on a code rung. When
    one starts passing, this xfail fails: move the entry (to "code", or to
    REVEAL_DRAFTS) in tests/sim/corpus.py."""
    assert not run_sim.leaks(draft, leak, strict=(rungs == "every"))


@pytest.mark.parametrize("name, draft, leak, rungs",
                         [b for b in corpus.BLIND_SPOT_DRAFTS if b[3] == "code"],
                         ids=[b[0] for b in corpus.BLIND_SPOT_DRAFTS if b[3] == "code"])
def test_a_code_rung_blind_spot_is_cut_below_h5(name, draft, leak, rungs):
    assert not run_sim.leaks(draft, leak, strict=True), f"'{name}' leaks at every rung"


def test_every_blind_spot_names_its_rungs():
    assert {b[3] for b in corpus.BLIND_SPOT_DRAFTS} <= {"every", "code"}
    assert len({b[0] for b in corpus.BLIND_SPOT_DRAFTS}) == len(corpus.BLIND_SPOT_DRAFTS)


# ---------------------------------------------------------------------------
# Low-rung partial-code leaks (the genuine G3 misses) are cut in strict mode
# ---------------------------------------------------------------------------

def test_low_rung_leaks_pass_lenient_but_strict_cuts_them():
    for name, draft in corpus.LOW_RUNG_LEAKS:
        # The lenient (high-rung) detector misses these (short or blanked)...
        assert not detector.scan(draft).violating, (
            f"'{name}' is now caught leniently; it is no longer a low-rung-only leak")
        # ...but on a low-rung turn the strict detector cuts every one.
        assert detector.scan(draft, strict=True).violating, f"strict missed '{name}'"
        assert not detector.scan(detector.redact(draft, strict=True), strict=True).violating, (
            f"'{name}' survived strict redaction")


# ---------------------------------------------------------------------------
# Is the last live run about this code?
# ---------------------------------------------------------------------------

def _tree(tmp_path, **files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def test_the_fingerprint_covers_the_supervision_files():
    fp = run_sim.supervision_fingerprint()
    for name in ("policy.py", "judge.py", "prompt_builder.py", "turn.py"):
        assert f"tutor/agent/{name}" in fp
    assert any(k.startswith("tutor/agent/turn_stages/") for k in fp)
    assert all(len(v) == 12 for v in fp.values())


def test_the_fingerprint_moves_with_a_file(tmp_path):
    root = _tree(tmp_path, **{"tutor/agent/judge.py": "x = 1\n", "tutor/agent/turn_stages/a.py": "y\n"})
    before = run_sim.supervision_fingerprint(root)
    assert before["tutor/agent/policy.py"] == "missing"
    (root / "tutor/agent/judge.py").write_text("x = 2\n")
    after = run_sim.supervision_fingerprint(root)
    assert [k for k in before if before[k] != after[k]] == ["tutor/agent/judge.py"]


def test_a_report_without_a_record_is_unmeasured():
    f = run_sim.live_freshness("## Live measurement (--live) — 2026-06-14\n\nDrove 26 turns.", {"a": "1"})
    assert f.status == "unrecorded" and f.run_date == "2026-06-14" and not f.current
    assert run_sim.live_freshness("", {"a": "1"}).status == "unrecorded"


def test_a_record_is_current_until_a_file_changes():
    fp = {"tutor/agent/judge.py": "aaaaaaaaaaaa", "tutor/agent/policy.py": "bbbbbbbbbbbb"}
    text = "## Live measurement (--live): 2026-10-01\n\n" + run_sim.live_record("2026-10-01", fp)
    assert run_sim.live_freshness(text, fp).current
    stale = run_sim.live_freshness(text, {**fp, "tutor/agent/judge.py": "cccccccccccc"})
    assert stale.status == "stale" and stale.changed == ["tutor/agent/judge.py"]
    assert "predates changes to tutor/agent/judge.py" in stale.summary()


def test_a_live_report_round_trips(monkeypatch):
    fp = run_sim.supervision_fingerprint()
    g = run_sim.GateResult("G2", True, "ok")
    live = run_sim.LiveReport(generated_at="2026-10-01", g2=g, g3=g, fingerprint=fp)
    md = run_sim.render_report(run_sim.run_sim(), live=live)
    assert run_sim.live_freshness(md, fp).current
    assert run_sim.previous_live_section(md).startswith("## Live measurement (--live): 2026-10-01")


def _main(monkeypatch, tmp_path, report_text, *argv):
    path = tmp_path / "report.md"
    if report_text is not None:
        path.write_text(report_text)
    monkeypatch.setattr(run_sim, "REPORT_PATH", path)
    monkeypatch.setattr(run_sim, "ROOT", tmp_path)
    return run_sim.main(list(argv)), path


def test_the_runner_exits_2_and_says_so_when_the_live_run_is_stale(monkeypatch, tmp_path, capsys):
    june = "# old\n\n## Live measurement (--live) — 2026-06-14\n\nDrove 26 supervised turns.\n"
    code, path = _main(monkeypatch, tmp_path, june, "--write")
    assert code == run_sim.EXIT_LIVE_UNMEASURED == 2
    assert "UNMEASURED" in capsys.readouterr().err
    written = path.read_text()
    assert "| G3 rung compliance >= 95% | **UNMEASURED** |" in written
    assert written.rstrip().endswith("Drove 26 supervised turns.")     # the old section, kept


def test_the_runner_exits_0_when_the_live_run_is_current(monkeypatch, tmp_path, capsys):
    fp = run_sim.supervision_fingerprint()
    text = "## Live measurement (--live): 2026-10-01\n\n" + run_sim.live_record("2026-10-01", fp) + "\n"
    code, _ = _main(monkeypatch, tmp_path, text)
    assert code == 0 and "UNMEASURED" not in capsys.readouterr().err


def test_a_failed_gate_outranks_a_stale_live_run(monkeypatch, tmp_path):
    monkeypatch.setattr(run_sim, "gate_exam_injection", lambda: run_sim.GateResult("G4", False, "broken"))
    code, _ = _main(monkeypatch, tmp_path, None)
    assert code == run_sim.EXIT_GATE_FAILED == 1


# ---------------------------------------------------------------------------
# Whole-run + report
# ---------------------------------------------------------------------------

def test_run_sim_all_measured_gates_pass():
    report = run_sim.run_sim()
    assert report.all_measured_pass
    # G3 is present, not measured offline, and never reads as a pass.
    g3 = next(g for g in report.gates if g.name.startswith("G3"))
    assert g3.measured is False and g3.passed is False


def test_render_report_lists_gates():
    md = run_sim.render_report(run_sim.run_sim())
    assert "# Adversarial sim report" in md
    assert "G1 no solution reaches the student (no-code rungs, H4 and below)" in md
    assert "G1 no solution reaches the student (code rungs, H5 and H6)" in md
    assert "Known detector blind spots" in md
    # Naming a still-open "every" entry from the corpus, so closing a blind
    # spot (as the html/spanrun block kinds closed two) does not stale this.
    every = next(n for n, _d, _l, r in corpus.BLIND_SPOT_DRAFTS if r == "every")
    assert f"| `{every}` | at every rung |" in md
