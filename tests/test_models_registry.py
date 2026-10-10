"""Session 6 (S21): one model registry; every model id flows from it."""

from __future__ import annotations

from pathlib import Path

from tutor.agent import judge, mastery, models, preclassify

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "tutor"


def test_supervisor_tier_drives_all_auxiliary_callers():
    assert preclassify._CLASSIFIER_MODEL == models.SUPERVISOR.id
    assert judge._JUDGE_MODEL == models.SUPERVISOR.id
    assert mastery._GRADER_MODEL == models.SUPERVISOR.id


def test_no_hardcoded_model_ids_outside_registry():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "models.py":
            continue
        if "claude-" in path.read_text():
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"hardcoded model ids in: {offenders}"


def test_pricing_known_and_unknown_models():
    assert models.pricing(models.ACTOR.id) == (3.0, 15.0, 0.30)
    assert models.pricing(models.SUPERVISOR.id) == (1.0, 5.0, 0.10)
    # Unknown ids price conservatively at actor rates.
    assert models.pricing("some-future-model") == models.DEFAULT_PRICING


def test_usage_estimator_reads_the_registry():
    from tutor.agent.usage import estimate_cost_usd

    # 1M input tokens of supervisor = $1.00 exactly (per the registry).
    assert estimate_cost_usd(models.SUPERVISOR.id, 1_000_000, 0, 0) == 1.0
    assert estimate_cost_usd(models.ACTOR.id, 0, 1_000_000, 0) == 15.0
