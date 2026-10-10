"""Alert delivery details (finding F55): a
delivery failure gives the key's slot back, and the database and boot alerts
are never crowded out by a storm of distinct exception keys."""

from __future__ import annotations

import pytest

from tutor import alerts


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.edu")
    from tutor.config import get_config

    get_config.cache_clear()
    alerts._reset_for_tests()
    yield
    alerts._reset_for_tests()


def test_a_failed_delivery_frees_the_key(monkeypatch):
    results = iter(["error", "sent"])
    monkeypatch.setattr(alerts, "_deliver_safe", lambda *a: next(results))
    assert alerts.send_alert("db-down", "Database unreachable", "x", background=False) == "error"
    # The cooldown would have silenced the retry for an hour; the slot was released.
    assert alerts.send_alert("db-down", "Database unreachable", "x", background=False) == "sent"
    assert alerts.send_alert("db-down", "Database unreachable", "x", background=False) == "cooldown"


def test_priority_keys_skip_the_global_cap(monkeypatch):
    monkeypatch.setattr(alerts, "_deliver_safe", lambda *a: "sent")
    for i in range(alerts._GLOBAL_CAP_PER_HOUR):
        assert alerts.send_alert(f"exc:Boom:/path{i}", "Boom", "x", background=False) == "sent"
    assert alerts.send_alert("exc:Boom:/one-more", "Boom", "x", background=False) == "capped"
    assert alerts.send_alert("db-down", "Database unreachable", "x", background=False) == "sent"
    assert alerts.send_alert("boot-degraded", "Boot", "x", background=False) == "sent"


def test_student_related_alerts_share_a_daily_budget(monkeypatch):
    """A student walking distinct 404 paths could spend the hourly cap the
    system alerts need (the review of step 0.9; decision 10): dead links,
    sign-in failures and reports share ten a day, and the rest still send."""
    monkeypatch.setattr(alerts, "_deliver_safe", lambda *a: "sent")
    for i in range(alerts._STUDENT_CAP_PER_DAY):
        assert alerts.send_alert(f"404:/x{i}", "Dead link", "x", background=False) == "sent"
    assert alerts.send_alert("404:/one-more", "Dead link", "x", background=False) == "capped"
    assert alerts.send_alert("login:abc123", "Sign-in failing", "x", background=False) == "capped"
    assert alerts.send_alert("report:7", "A student reported a problem", "x", background=False) == "capped"
    assert alerts.send_alert("exc:Boom:/path", "Boom", "x", background=False) == "sent"
    assert alerts.send_alert("tutor-errors", "Tutor failures", "x", background=False) == "sent"
    assert alerts.send_alert("db-down", "Database unreachable", "x", background=False) == "sent"

