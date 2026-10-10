"""Task 3 tests: usage cost estimation and spend caps.

Network-free, in-memory SQLite. Cap env vars are set via monkeypatch.
"""

from __future__ import annotations

import pytest

from tutor.agent import state, usage
from tutor.db.models import Student
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="a@example.edu", name="A"))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def test_estimate_cost_known_model():
    from tutor.agent import models as _models
    # sonnet: 3 / 15 / 0.30 per 1M
    cost = usage.estimate_cost_usd(_models.ACTOR.id, 1_000_000, 1_000_000, 1_000_000)
    assert cost == pytest.approx(3.0 + 15.0 + 0.30)


def test_estimate_cost_unknown_model_uses_default():
    cost = usage.estimate_cost_usd("some-future-model", 1_000_000, 0, 0)
    assert cost == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Ledger reads
# ---------------------------------------------------------------------------

def test_daily_exchange_count_counts_today_chat(db):
    state.record_turn_event(db, student_id=1, kind="chat")
    state.record_turn_event(db, student_id=1, kind="chat")
    state.record_turn_event(db, student_id=1, kind="exam")  # not a chat exchange
    assert usage.daily_exchange_count(db, 1) == 2


def test_month_cost_sums_costs(db):
    state.record_turn_event(db, student_id=1, kind="chat", cost_usd=0.01)
    state.record_turn_event(db, student_id=1, kind="chat", cost_usd=0.02)
    assert usage.month_cost_usd(db) == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

def test_over_caps_disabled_by_default(db, monkeypatch):
    """The two environment caps are off until set. The per-student dollar
    quotas (tutor/quotas.py) are on by default, so they are switched off
    here; tests/test_quotas.py covers them."""
    from tutor import quotas

    monkeypatch.delenv("DAILY_EXCHANGE_LIMIT", raising=False)
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    quotas.save_quotas(db, {"day": 0, "week": 0, "month": 0})
    db.commit()
    for _ in range(5):
        state.record_turn_event(db, student_id=1, kind="chat", cost_usd=100.0)
    assert usage.over_caps(db, 1) is None


def test_daily_exchange_cap_triggers(db, monkeypatch):
    monkeypatch.setenv("DAILY_EXCHANGE_LIMIT", "2")
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    state.record_turn_event(db, student_id=1, kind="chat")
    assert usage.over_caps(db, 1) is None  # 1 < 2
    state.record_turn_event(db, student_id=1, kind="chat")
    assert (usage.over_caps(db, 1) or "").startswith(usage.DAILY_LIMIT_MESSAGE)  # 2 >= 2


def test_monthly_cost_cap_triggers(db, monkeypatch):
    monkeypatch.delenv("DAILY_EXCHANGE_LIMIT", raising=False)
    monkeypatch.setenv("MONTHLY_COST_CAP_USD", "0.05")
    state.record_turn_event(db, student_id=1, kind="chat", cost_usd=0.04)
    assert usage.over_caps(db, 1) is None
    state.record_turn_event(db, student_id=1, kind="chat", cost_usd=0.02)
    assert (usage.over_caps(db, 1) or "").startswith(usage.MONTHLY_CAP_MESSAGE)  # 0.06 >= 0.05


def test_daily_cap_is_per_student(db, monkeypatch):
    monkeypatch.setenv("DAILY_EXCHANGE_LIMIT", "1")
    db.add(Student(id=2, email="b@example.edu", name="B"))
    db.commit()
    state.record_turn_event(db, student_id=1, kind="chat")
    assert (usage.over_caps(db, 1) or "").startswith(usage.DAILY_LIMIT_MESSAGE)
    assert usage.over_caps(db, 2) is None  # student 2 has no exchanges


def test_a_class_wide_cap_hit_is_recorded_once_a_day(db, monkeypatch):
    """DAILY_EXCHANGE_LIMIT and MONTHLY_COST_CAP_USD hits left nothing
    (finding F44): one zero-cost quota row per
    student and period, read by the Today panel like a quota hit."""
    from tutor import quotas

    monkeypatch.setenv("DAILY_EXCHANGE_LIMIT", "1")
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    state.record_turn_event(db, student_id=1, kind="chat")
    db.commit()
    assert (usage.over_caps(db, 1) or "").startswith(usage.DAILY_LIMIT_MESSAGE)
    assert (usage.over_caps(db, 1) or "").startswith(usage.DAILY_LIMIT_MESSAGE)      # pressing Send again
    hits = quotas.hits(db, usage.utcnow() - __import__("datetime").timedelta(days=1))
    assert [(h.window, h.limit_usd) for h in hits] == [("turns-day", 0.0)]
    assert usage.daily_exchange_count(db, 1) == 1                    # the hit row is not a turn

    monkeypatch.delenv("DAILY_EXCHANGE_LIMIT", raising=False)
    monkeypatch.setenv("MONTHLY_COST_CAP_USD", "0.05")
    state.record_turn_event(db, student_id=1, kind="chat", cost_usd=0.06)
    db.commit()
    assert (usage.over_caps(db, 1) or "").startswith(usage.MONTHLY_CAP_MESSAGE)
    assert (usage.over_caps(db, 1) or "").startswith(usage.MONTHLY_CAP_MESSAGE)
    hits = quotas.hits(db, usage.utcnow() - __import__("datetime").timedelta(days=1))
    assert sorted((h.window, h.limit_usd) for h in hits) == [("class-month", 0.05), ("turns-day", 0.0)]
