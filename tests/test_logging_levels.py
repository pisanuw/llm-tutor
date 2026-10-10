"""Multi-level activity logging + slow-turn threshold (monitoring).

Network-free. Covers the ACTIVITY_LEVEL tier filtering (minimal/standard/verbose),
the SLOW_TURN_MS reader, and that activity() actually suppresses/emits a JSON line
by tier.
"""

from __future__ import annotations

import json
import logging

import pytest

from tutor import logging_config as lc


# ---------------------------------------------------------------------------
# Level configuration
# ---------------------------------------------------------------------------

def test_activity_level_defaults_to_standard(monkeypatch):
    monkeypatch.delenv("ACTIVITY_LEVEL", raising=False)
    assert lc.activity_level() == "standard"


@pytest.mark.parametrize("val,expect", [
    ("minimal", "minimal"), ("VERBOSE", "verbose"), (" Standard ", "standard"),
    ("bogus", "standard"), ("", "standard"),
])
def test_activity_level_parses_env(monkeypatch, val, expect):
    monkeypatch.setenv("ACTIVITY_LEVEL", val)
    assert lc.activity_level() == expect


@pytest.mark.parametrize("config,event,emits", [
    ("minimal", "minimal", True),
    ("minimal", "standard", False),
    ("minimal", "verbose", False),
    ("standard", "minimal", True),
    ("standard", "standard", True),
    ("standard", "verbose", False),
    ("verbose", "minimal", True),
    ("verbose", "standard", True),
    ("verbose", "verbose", True),
])
def test_level_emits_matrix(monkeypatch, config, event, emits):
    monkeypatch.setenv("ACTIVITY_LEVEL", config)
    assert lc._level_emits(event) is emits


# ---------------------------------------------------------------------------
# Slow-turn threshold
# ---------------------------------------------------------------------------

def test_slow_turn_ms_default(monkeypatch):
    monkeypatch.delenv("SLOW_TURN_MS", raising=False)
    assert lc.slow_turn_ms() == 25000


def test_slow_turn_ms_env_and_bad_value(monkeypatch):
    monkeypatch.setenv("SLOW_TURN_MS", "8000")
    assert lc.slow_turn_ms() == 8000
    monkeypatch.setenv("SLOW_TURN_MS", "not-a-number")
    assert lc.slow_turn_ms() == 25000


# ---------------------------------------------------------------------------
# activity() emits / suppresses by tier
# ---------------------------------------------------------------------------

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record):
        try:
            self.records.append(json.loads(record.getMessage()))
        except Exception:
            pass


@pytest.fixture()
def captured():
    log = logging.getLogger("tutor.activity")
    h = _Capture()
    log.addHandler(h)
    old_level = log.level
    log.setLevel(logging.INFO)
    yield h
    log.removeHandler(h)
    log.setLevel(old_level)


def test_activity_suppressed_below_configured_level(monkeypatch, captured):
    monkeypatch.setenv("ACTIVITY_LEVEL", "standard")
    lc.activity("turn_start", level="verbose", turn_id="abc")
    assert captured.records == []  # verbose event dropped at standard


def test_activity_emitted_at_or_above_level(monkeypatch, captured):
    monkeypatch.setenv("ACTIVITY_LEVEL", "standard")
    lc.activity("chat_turn", level="standard", student_id=1, latency_ms=1200)
    lc.activity("slow_turn", level="minimal", student_id=1, latency_ms=40000)
    events = {r["event"] for r in captured.records}
    assert events == {"chat_turn", "slow_turn"}
    row = next(r for r in captured.records if r["event"] == "chat_turn")
    assert row["student_id"] == 1 and row["latency_ms"] == 1200


def test_activity_verbose_visible_when_verbose(monkeypatch, captured):
    monkeypatch.setenv("ACTIVITY_LEVEL", "verbose")
    lc.activity("ui_event", level="verbose", action="click", target="teach-btn")
    assert captured.records and captured.records[0]["event"] == "ui_event"
    assert captured.records[0]["action"] == "click"


def test_activity_default_level_is_standard(monkeypatch, captured):
    # No explicit level => treated as standard, visible at standard config.
    monkeypatch.setenv("ACTIVITY_LEVEL", "standard")
    lc.activity("login", student_id=7)
    assert captured.records and captured.records[0]["event"] == "login"
