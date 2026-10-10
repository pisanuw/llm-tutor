"""Session 23: cost report from the telemetry ledger.

Pure aggregation, cap recommendation, and markdown render. No DB, no network.
"""
from __future__ import annotations

import datetime

from pipeline import cost_report as C


def _row(kind, cost, in_tok=0, cache=0, student_id=1, day=None):
    return {"kind": kind, "cost": cost, "in_tokens": in_tok, "cache_tokens": cache,
            "student_id": student_id, "day": day or datetime.date(2026, 6, 12)}


def test_aggregate_splits_actor_supervisor_other():
    rows = [
        _row("chat", 0.030, in_tok=1000, cache=4000),
        _row("classify", 0.001),
        _row("judge", 0.002),
        _row("strategist", 0.001),
        _row("exercise", 0.000),
    ]
    st = C.aggregate(rows, window_days=30)
    assert st.n_turns == 5
    assert round(st.total_cost, 4) == 0.034
    assert round(st.actor_cost, 4) == 0.030
    assert round(st.supervisor_cost, 4) == 0.004
    assert round(st.other_cost, 4) == 0.0
    # supervisor overhead share = 0.004 / 0.034
    assert abs(st.supervisor_share - (0.004 / 0.034)) < 1e-9
    # cache share = 4000 / (1000 + 4000)
    assert abs(st.cache_share - 0.8) < 1e-9


def test_aggregate_counts_student_days_distinct():
    d1, d2 = datetime.date(2026, 6, 11), datetime.date(2026, 6, 12)
    rows = [
        _row("chat", 0.01, student_id=1, day=d1),
        _row("chat", 0.01, student_id=1, day=d1),  # same student-day
        _row("chat", 0.01, student_id=1, day=d2),  # new day
        _row("chat", 0.01, student_id=2, day=d1),  # new student
    ]
    st = C.aggregate(rows, window_days=7)
    assert st.student_days == 3
    assert abs(st.cost_per_student_day - (0.04 / 3)) < 1e-9


def test_aggregate_by_kind():
    rows = [_row("chat", 0.03), _row("chat", 0.05), _row("judge", 0.002)]
    st = C.aggregate(rows, window_days=30)
    assert st.by_kind["chat"]["n"] == 2
    assert round(st.by_kind["chat"]["cost"], 4) == 0.08
    assert st.by_kind["judge"]["n"] == 1


def test_empty_ledger_is_safe():
    st = C.aggregate([], window_days=30)
    assert st.n_turns == 0
    assert st.per_turn_avg == 0.0
    assert st.cache_share == 0.0
    assert st.supervisor_share == 0.0
    assert st.cost_per_student_day == 0.0


def test_recommend_caps_math():
    rec = C.recommend_caps(0.03, students=30, turns_per_active_day=15,
                           active_days_per_quarter=30, basis="baseline")
    assert abs(rec.per_student_quarter_usd - 13.5) < 1e-9
    assert rec.daily_exchange_limit == 30
    # 13.5 * 30 / 3 * 1.3 = 175.5 -> round up to nearest 5 -> 180
    assert rec.monthly_cost_cap_usd == 180


def test_recommend_caps_daily_floor():
    rec = C.recommend_caps(0.03, students=5, turns_per_active_day=5)
    assert rec.daily_exchange_limit == 20  # floor


def test_render_report_has_sections_and_recs():
    rows = [_row("chat", 0.03, in_tok=1000, cache=4000), _row("judge", 0.002)]
    st = C.aggregate(rows, window_days=30)
    rec = C.recommend_caps(0.03, students=30, basis="baseline")
    md = C.render_report(st, rec, generated_at="2026-06-12")
    assert "# Cost report" in md
    assert "supervisor overhead share" in md
    assert "Cost by kind" in md
    assert "DAILY_EXCHANGE_LIMIT" in md
    assert "MONTHLY_COST_CAP_USD" in md
    assert "180" in md


def test_thin_ledger_note_present():
    st = C.aggregate([_row("chat", 0.03)], window_days=30)
    md = C.render_report(st, C.recommend_caps(0.03, basis="baseline"))
    assert "fewer than 50 turns" in md
