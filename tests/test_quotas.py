"""Per-student dollar quotas (tutor/quotas.py): calendar windows in the
class's time zone (the fixture's America/Los_Angeles),
settings-backed limits, spend that leaves the warm-up out, the admin
exemption, the in-voice message with the reset time, one recorded hit per
period, the chat's indicator from 80 percent, the admin form and tables,
and the roll-up email with its daily, weekly and monthly schedule.
"""
from __future__ import annotations

import datetime
import json

import pytest
from fastapi.testclient import TestClient

from tests.helpers import login_cookie, make_session
from tutor import quota_report, quotas
from tutor.agent import state, usage
from tutor.db.models import Student, TurnEvent, WhitelistEntry

# A Tuesday, 1:00 pm PDT (20:00 UTC), October 2026.
NOW = datetime.datetime(2026, 10, 6, 20, 0, 0)


@pytest.fixture
def db(monkeypatch):
    import tutor.config as config

    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    s = make_session(
        Student(id=1, email="stu@example.edu", name="Stu"),
        Student(id=2, email="prof@example.edu", name="Prof"),
        Student(id=3, email="bo@example.edu", name="Bo"),
        WhitelistEntry(email="stu@example.edu", added_by="t"),
    )
    yield s
    s.close()


def _spend(db, sid, usd, when=NOW, kind="chat"):
    db.add(TurnEvent(student_id=sid, kind=kind, cost_usd=usd, ts=when))
    db.commit()


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def test_windows_are_local_calendar_periods():
    day = quotas.window_bounds("day", NOW)
    assert day == (datetime.datetime(2026, 10, 6, 7, 0), datetime.datetime(2026, 10, 7, 7, 0))   # midnight PDT
    week = quotas.window_bounds("week", NOW)
    assert week == (datetime.datetime(2026, 10, 5, 7, 0), datetime.datetime(2026, 10, 12, 7, 0))  # Monday to Monday
    month = quotas.window_bounds("month", NOW)
    assert month == (datetime.datetime(2026, 10, 1, 7, 0), datetime.datetime(2026, 11, 1, 7, 0))
    # Early morning UTC is still the previous local day.
    assert quotas.window_bounds("day", datetime.datetime(2026, 10, 7, 3, 0))[0] == datetime.datetime(2026, 10, 6, 7, 0)


def test_the_day_the_clocks_fall_back_is_25_hours_long():
    start, reset = quotas.window_bounds("day", datetime.datetime(2026, 11, 1, 12, 0))   # Nov 1 2026, DST ends
    assert reset - start == datetime.timedelta(hours=25)
    dec = quotas.window_bounds("month", datetime.datetime(2026, 12, 15, 12, 0))
    assert dec[1] == datetime.datetime(2027, 1, 1, 8, 0)                                   # midnight PST


def test_previous_period_and_what_is_due():
    assert quotas.previous_period("daily", NOW) == (datetime.datetime(2026, 10, 5, 7, 0), datetime.datetime(2026, 10, 6, 7, 0))
    assert quotas.previous_period("weekly", NOW) == (datetime.datetime(2026, 9, 28, 7, 0), datetime.datetime(2026, 10, 5, 7, 0))
    assert quotas.previous_period("monthly", NOW) == (datetime.datetime(2026, 9, 1, 7, 0), datetime.datetime(2026, 10, 1, 7, 0))
    assert quotas.period_is_due("daily", NOW) and not quotas.period_is_due("weekly", NOW)
    assert quotas.period_is_due("weekly", datetime.datetime(2026, 10, 5, 15, 0))          # Monday
    assert quotas.period_is_due("monthly", datetime.datetime(2026, 11, 1, 15, 0))
    assert not quotas.period_is_due("monthly", datetime.datetime(2026, 11, 1, 5, 0))      # still Oct 31 locally


# ---------------------------------------------------------------------------
# Settings, spend, status
# ---------------------------------------------------------------------------

def test_settings_default_and_round_trip(db):
    assert quotas.load_quotas(db) == {"day": 1.5, "week": 6.0, "month": 15.0}
    assert quotas.load_notify(db) == {"daily": False, "weekly": True, "monthly": True}
    quotas.save_quotas(db, {"day": 0.75, "week": 0, "month": 20})
    quotas.save_notify(db, {"daily": True, "monthly": False})
    db.commit()
    assert quotas.load_quotas(db) == {"day": 0.75, "week": 0.0, "month": 20.0}
    assert quotas.load_notify(db) == {"daily": True, "weekly": True, "monthly": False}
    assert [s.window for s in quotas.statuses(db, 1, NOW)] == ["day", "month"]      # the week is off


def test_spend_counts_every_call_but_the_warm_up(db):
    _spend(db, 1, 0.40)
    _spend(db, 1, 0.10, kind="classify")
    _spend(db, 1, 0.05, kind="review")
    _spend(db, 1, 0.27, kind="cache_warm")                        # opening the page, not the student
    _spend(db, 1, 0.50, when=NOW - datetime.timedelta(days=1))   # Monday: this week, not today
    _spend(db, 1, 0.70, when=NOW - datetime.timedelta(days=2))   # Sunday: last week, this month
    _spend(db, 3, 9.00)
    day, week, month = quotas.statuses(db, 1, NOW)
    assert (day.spent_usd, week.spent_usd, month.spent_usd) == pytest.approx((0.55, 1.05, 1.75))
    assert day.fraction == pytest.approx(0.55 / 1.5) and not day.over and day.remaining_usd == pytest.approx(0.95)
    assert quotas.spent_by_student(db, quotas.window_bounds("day", NOW)[0]) == pytest.approx({1: 0.55, 3: 9.0})


def test_blocking_picks_the_day_first_and_spares_admins(db):
    _spend(db, 1, 1.50)
    _spend(db, 2, 99.0)
    hit = quotas.blocking(db, 1, NOW)
    assert hit is not None and hit.window == "day" and hit.over
    assert quotas.blocking(db, 2, NOW) is None                          # an admin
    assert quotas.blocking(db, 3, NOW) is None
    quotas.save_quotas(db, {"day": 0, "week": 1.0})
    db.commit()
    assert quotas.blocking(db, 1, NOW).window == "week"


def test_the_message_says_when_the_quota_comes_back():
    day = quotas.QuotaStatus("day", 1.5, 1.6, *quotas.window_bounds("day", NOW))
    assert quotas.message(day, NOW) == (
        "That is today's tutor budget used up. It comes back at midnight PDT (in about 11 hours). "
        "The slides and the book are still open, and so is the review page's reading. :-)")
    week = quotas.QuotaStatus("week", 6.0, 6.0, *quotas.window_bounds("week", NOW))
    assert "resets Monday at midnight PDT (in 6 days)" in quotas.message(week, NOW)
    month = quotas.QuotaStatus("month", 15.0, 15.2, *quotas.window_bounds("month", NOW))
    assert "resets on November 1 (in 26 days)" in quotas.message(month, NOW)
    soon = quotas.QuotaStatus("day", 1.5, 1.6, NOW - datetime.timedelta(hours=23), NOW + datetime.timedelta(minutes=40))
    assert quotas.reset_phrase(soon, NOW) == "at midnight PDT (in about 40 minutes)"


def test_a_hit_is_recorded_once_per_window_and_period(db, monkeypatch):
    monkeypatch.setattr(quotas, "utcnow", lambda: NOW)
    _spend(db, 1, 2.0)
    status = quotas.blocking(db, 1, NOW)
    assert quotas.record_hit(db, 1, status) is True
    assert quotas.record_hit(db, 1, status) is False               # pressing Send again
    week = quotas.QuotaStatus("week", 6.0, 6.5, *quotas.window_bounds("week", NOW))
    assert quotas.record_hit(db, 1, week) is True                  # another window is another hit
    db.commit()
    rows = db.query(TurnEvent).filter_by(kind="quota").all()
    assert len(rows) == 2 and all(r.cost_usd == 0 for r in rows)
    detail = json.loads(rows[0].verdicts_json)
    assert (detail["window"], detail["limit_usd"], detail["spent_usd"]) == ("day", 1.5, 2.0)
    hits = quotas.hits(db, NOW - datetime.timedelta(days=1))
    assert {h.window for h in hits} == {"day", "week"} and hits[0].email == "stu@example.edu"
    (summary,) = quotas.summarize_hits(hits)
    assert summary["hits"] == 2 and summary["windows"] == {"day": 1, "week": 1}
    # The spend table ranks by the worst share of a quota.
    _spend(db, 3, 0.30)
    table = quotas.spend_table(db, NOW)
    assert [r.email for r in table] == ["stu@example.edu", "bo@example.edu"]
    assert table[0].day_pct == 133 and table[1].day_pct == 20 and table[1].month_pct == 2


def test_the_indicator_speaks_from_80_percent(db):
    assert quotas.indicator(db, 1, NOW)["note"] is None
    _spend(db, 1, 1.25)
    shown = quotas.indicator(db, 1, NOW)
    assert shown["note"].startswith("You have used 83% of today's tutor budget ($0.25 left). It comes back at midnight PDT")
    assert shown["blocked"] is None and shown["windows"][0]["fraction"] == pytest.approx(0.833, abs=0.001)
    _spend(db, 1, 0.30)
    blocked = quotas.indicator(db, 1, NOW)
    assert blocked["blocked"] and blocked["note"] == blocked["blocked"]
    assert quotas.indicator(db, 2, NOW) == {"windows": [], "note": None, "blocked": None, "exempt": True}


# ---------------------------------------------------------------------------
# Enforcement: the caps, the photo route, the chat endpoint
# ---------------------------------------------------------------------------

def test_over_caps_blocks_with_the_quota_message_and_records_the_hit(db, monkeypatch):
    monkeypatch.delenv("DAILY_EXCHANGE_LIMIT", raising=False)
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    monkeypatch.setattr(quotas, "utcnow", lambda: NOW)
    assert usage.over_caps(db, 1) is None
    _spend(db, 1, 1.50)
    message = usage.over_caps(db, 1)
    assert message and message.startswith("That is today's tutor budget used up.")
    assert usage.over_review_caps(db, 1) == message
    assert db.query(TurnEvent).filter_by(kind="quota", student_id=1).count() == 1
    assert usage.over_caps(db, 2) is None                              # the admin


def test_the_daily_exchange_count_uses_the_local_day(db):
    before_midnight = datetime.datetime(2026, 10, 6, 6, 30)   # 11:30 pm PDT the day before
    state.record_turn_event(db, student_id=1, kind="chat")
    db.query(TurnEvent).update({TurnEvent.ts: before_midnight})
    db.commit()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(quotas, "utcnow", lambda: NOW)
        assert usage.daily_exchange_count(db, 1) == 0
        mp.setattr(quotas, "utcnow", lambda: datetime.datetime(2026, 10, 6, 6, 45))
        assert usage.daily_exchange_count(db, 1) == 1


@pytest.fixture
def student(db):
    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set("session_token", login_cookie(db, student_id=1))
    return client


def test_the_chat_quota_endpoint_and_the_photo_route(db, student, monkeypatch):
    monkeypatch.setattr(quotas, "utcnow", lambda: NOW)
    quiet = student.get("/chat/quota").json()
    assert quiet["note"] is None and [w["window"] for w in quiet["windows"]] == ["day", "week", "month"]
    assert quiet["windows"][0]["resets"] == "at midnight PDT (in about 11 hours)"
    _spend(db, 1, 1.5)
    assert student.get("/chat/quota").json()["blocked"].startswith("That is today's tutor budget used up.")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    resp = student.post("/chat/transcribe-photo", files={"image": ("p.png", png, "image/png")})
    assert resp.status_code == 429 and "tutor budget used up" in resp.json()["error"]
    assert db.query(TurnEvent).filter_by(kind="quota").count() == 1


# ---------------------------------------------------------------------------
# The admin page
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(db, monkeypatch):
    import tutor.config as config
    from tutor.auth.deps import get_db
    from tutor.main import app

    monkeypatch.setenv("ADMIN_TOKEN", "tkn-quota")
    config.get_config.cache_clear()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, base_url="http://localhost")
    assert client.post("/admin/login", data={"token": "tkn-quota"}, follow_redirects=False).status_code == 303
    return client


def test_the_admin_form_saves_quotas_and_notifications(admin, db):
    page = admin.get("/admin?tab=supervision").text
    assert 'data-quotas' in page and 'value="1.50"' in page and 'value="6.00"' in page
    resp = admin.post("/admin/quotas", data={"day": "$2", "week": "", "month": "25.5", "notify_daily": "on"},
                      follow_redirects=False)
    assert resp.status_code == 303
    assert quotas.load_quotas(db) == {"day": 2.0, "week": 0.0, "month": 25.5}
    assert quotas.load_notify(db) == {"daily": True, "weekly": False, "monthly": False}
    assert admin.post("/admin/quotas", data={"day": "lots"}).status_code == 422
    assert admin.post("/admin/quotas", data={"day": "-1"}).status_code == 422
    page = admin.get("/admin?tab=supervision").text
    assert 'value="2.00"' in page and 'value="0.00"' in page and 'name="notify_daily" checked' in page


def test_the_admin_page_shows_spend_and_hits(admin, db, monkeypatch):
    import tutor.routers.admin as admin_mod

    monkeypatch.setattr(quotas, "utcnow", lambda: NOW)
    monkeypatch.setattr(admin_mod, "utcnow", lambda: NOW)
    _spend(db, 1, 1.60)
    _spend(db, 3, 0.20)
    usage.over_caps(db, 1)
    db.commit()
    page = admin.get("/admin?tab=supervision").text
    assert "data-quota-spend" in page and "stu@example.edu" in page and "(106%)" in page
    assert "data-quota-hits" in page and "day quota ($1.50), spend $1.60" in page
    today = admin.get("/admin?tab=today").text
    assert "data-today-quotas" in page or "data-today-quotas" in today
    assert "Students who hit a tutor quota, last 24 hours (1)" in today and "Stu" in today


def test_a_student_cannot_set_quotas(student):
    assert student.post("/admin/quotas", data={"day": "100"}, follow_redirects=False).status_code in (303, 403)


# ---------------------------------------------------------------------------
# The roll-up email
# ---------------------------------------------------------------------------

def test_the_roll_up_gathers_last_period_and_knows_when_it_is_due(db, capsys):
    yesterday = datetime.datetime(2026, 10, 5, 20, 0)
    for sid in (1, 3):
        status = quotas.QuotaStatus("day", 1.5, 1.7, *quotas.window_bounds("day", yesterday))
        quotas.record_hit(db, sid, status)
    week_hit = quotas.QuotaStatus("week", 6.0, 6.1, *quotas.window_bounds("week", yesterday))
    quotas.record_hit(db, 1, week_hit)
    db.query(TurnEvent).filter_by(kind="quota").update({TurnEvent.ts: yesterday})
    db.commit()
    roll = quota_report.gather(db, "daily", NOW)
    assert (len(roll.students), roll.total_hits) == (2, 3)
    assert roll.students[0]["email"] == "stu@example.edu" and roll.students[0]["windows"] == {"day": 1, "week": 1}
    assert roll.subject == "Astra: 2 students hit a tutor quota (yesterday, Oct 05 to Oct 06, 2026 (PDT))"
    md = quota_report.render_markdown(roll, "https://tutor.example.edu")
    assert "**Stu** (stu@example.edu): day, week" in md and "/admin?tab=supervision" in md
    html = quota_report.render_html(roll, "https://tutor.example.edu")
    assert "<td style='padding:4px 8px'>Bo<br>" in html
    assert quota_report.due_periods(db, NOW) == []                            # a Tuesday: daily is off by default
    quotas.save_notify(db, {"daily": True})
    db.commit()
    assert quota_report.due_periods(db, NOW) == ["daily"]
    assert quota_report.due_periods(db, datetime.datetime(2026, 10, 5, 15, 0)) == ["daily", "weekly"]
    assert quota_report.due_periods(db, NOW, force="monthly") == ["monthly"]
    assert quota_report.gather(db, "monthly", NOW).students == []


def test_the_cli_prints_without_sending(db, monkeypatch, capsys):
    from scripts import quota_report as cli

    monkeypatch.setattr("tutor.db.models.get_session_factory", lambda engine=None: (lambda: db))
    monkeypatch.setattr("tutor.db.models.get_engine", lambda db_path=None: db.get_bind())
    monkeypatch.setattr(quotas, "utcnow", lambda: NOW)
    status = quotas.QuotaStatus("day", 1.5, 1.7, *quotas.window_bounds("day", NOW - datetime.timedelta(days=1)))
    quotas.record_hit(db, 1, status)
    db.query(TurnEvent).filter_by(kind="quota").update({TurnEvent.ts: NOW - datetime.timedelta(days=1)})
    db.commit()
    assert cli.main(["--period", "daily", "--no-email"]) == 0
    out = capsys.readouterr().out
    assert "quota_report: daily" in out and "**Stu** (stu@example.edu): day" in out
    assert cli.main(["--no-email"]) == 0                       # Tuesday, daily off: nothing due
    assert "no roll-up is due" in capsys.readouterr().out
