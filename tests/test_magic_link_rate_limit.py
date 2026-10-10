"""Regression tests for the magic-link rate limiter (tutor.routers.auth).

Pins the corrected behavior: per-IP limiting within the window, recovery after
the window passes, isolation between IPs, and eviction of stale entries so the
in-memory map cannot grow without bound.
"""
from __future__ import annotations

import datetime

import pytest

import tutor.routers.auth as auth


@pytest.fixture(autouse=True)
def _clean_limiter():
    auth._magic_link_log.clear()
    yield
    auth._magic_link_log.clear()


def test_allows_up_to_limit_then_blocks():
    ip = "203.0.113.7"
    # First _MAGIC_LINK_LIMIT requests pass.
    for _ in range(auth._MAGIC_LINK_LIMIT):
        assert auth._magic_link_rate_check(ip) is True
    # The next one is blocked.
    assert auth._magic_link_rate_check(ip) is False


def test_separate_ips_are_independent():
    a, b = "198.51.100.1", "198.51.100.2"
    for _ in range(auth._MAGIC_LINK_LIMIT):
        assert auth._magic_link_rate_check(a) is True
    # a is now exhausted, but b still has its full allowance.
    assert auth._magic_link_rate_check(a) is False
    assert auth._magic_link_rate_check(b) is True


def test_window_recovery(monkeypatch):
    ip = "203.0.113.9"
    base = datetime.datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(auth, "utcnow", lambda: base)
    for _ in range(auth._MAGIC_LINK_LIMIT):
        assert auth._magic_link_rate_check(ip) is True
    assert auth._magic_link_rate_check(ip) is False

    # Advance past the window: the old hits age out and requests pass again.
    later = base + auth._MAGIC_LINK_WINDOW + datetime.timedelta(seconds=1)
    monkeypatch.setattr(auth, "utcnow", lambda: later)
    assert auth._magic_link_rate_check(ip) is True


def test_stale_entries_are_evicted(monkeypatch):
    """An IP whose window fully aged out must not linger in the map."""
    base = datetime.datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(auth, "utcnow", lambda: base)
    auth._magic_link_rate_check("192.0.2.50")
    assert "192.0.2.50" in auth._magic_link_log

    # A later request from a DIFFERENT ip triggers eviction of the stale one.
    later = base + auth._MAGIC_LINK_WINDOW + datetime.timedelta(seconds=1)
    monkeypatch.setattr(auth, "utcnow", lambda: later)
    auth._magic_link_rate_check("192.0.2.51")
    assert "192.0.2.50" not in auth._magic_link_log
    assert "192.0.2.51" in auth._magic_link_log
