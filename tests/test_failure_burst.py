"""A burst of failed turns emails the operator once (tutor/agent/failures.py,
student-facing review step 0.8): every failure inside a turn
is caught and shown as a friendly sentence, so the 500 handler's alert never
fires for a dead key or a renamed model."""

from __future__ import annotations

import pytest

from tutor import alerts
from tutor.agent import failures
from tutor.agent.turn_stages import errors


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: out.append((key, subject)) or "queued")
    failures._reset_for_tests()
    yield out
    failures._reset_for_tests()


def test_two_failures_only_count(sent):
    assert failures.note("stream") == "counted"
    assert failures.note("crash") == "counted"
    assert sent == []


def test_the_third_failure_in_the_window_alerts_with_the_kinds(sent):
    failures.note("stream")
    failures.note("stream")
    assert failures.note("grader") == "queued"
    assert sent == [("tutor-errors", "Tutor failures: 3 in 10 minutes")]


def test_old_failures_fall_out_of_the_window(sent, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(failures.time, "monotonic", lambda: clock[0])
    failures.note("stream")
    failures.note("stream")
    clock[0] += failures.WINDOW_S + 1
    assert failures.note("stream") == "counted"
    assert sent == []


def test_friendly_error_counts_the_failure_and_keeps_the_traceback(sent, caplog):
    import logging

    exc = RuntimeError("paused DB")
    try:
        raise exc
    except RuntimeError as caught:
        with caplog.at_level(logging.ERROR, logger="tutor"):
            text = errors.friendly_error(caught, failure="crash")
    assert "ran into a problem" in text
    record = next(r for r in caplog.records if "agent error" in r.getMessage())
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert len(failures._recent) == 1 and failures._recent[0][1] == "crash"


def test_the_alert_module_dedups_the_key_for_an_hour(monkeypatch):
    # Through the real send_alert: no Resend key, so "disabled", then the
    # cooldown holds the key.
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    alerts._reset_for_tests()
    failures._reset_for_tests()
    for _ in range(3):
        last = failures.note("stream")
    assert last == "disabled"
    assert failures.note("stream") == "cooldown"
