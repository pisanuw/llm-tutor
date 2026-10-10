"""The per-student daily rollup (review step 0.9; F37, F42, F43, F46): one
row per student per local day (the class's time zone), folded
from the raw tables, written by the app, read by the Today panel."""

from __future__ import annotations

import datetime
import json

import pytest

from tutor import student_days
from tutor.auth import attempts
from tutor.db.models import (
    ActivityEvent, FeedbackEvent, LoginAttempt, Message, RequestError, Session as LoginSession, Student,
    StudentDay, TurnEvent,
)
from tests.helpers import make_session

CHROME = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15"
SCANNER = "Mozilla/5.0 Proofpoint URLDefense"
# 23:30 PDT on Oct 5 and 00:30 PDT on Oct 6 (the fixture's America/Los_Angeles): a day boundary.
T = datetime.datetime(2026, 10, 6, 6, 30, 0)
T2 = datetime.datetime(2026, 10, 6, 7, 30, 0)
OCT5, OCT6 = datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)


def _event(db, sid, name, ts, seq, **detail):
    course = detail.pop("course", "AST101")
    lecture = detail.pop("lecture_num", 3)
    slide = detail.pop("slide_num", None)
    db.add(ActivityEvent(student_id=sid, visit_id=f"v{sid}", page_view_id=f"p{sid}", seq=seq, name=name, ts=ts,
                         course=course, lecture_num=lecture, slide_num=slide, path="/chat",
                         detail_json=json.dumps(detail) if detail else None))


@pytest.fixture
def db():
    s = make_session()
    s.add_all([Student(id=1, email="ada@example.edu", name="Ada"), Student(id=2, email="bo@example.edu", name="Bo"),
               Student(id=9, email="prof@example.edu", name="Prof", is_instructor=True)])
    s.flush()
    # Ada, late on Oct 5 local time: a question, a deck, two failed replies, a
    # blocked slow turn, a login, a failed sign-in, a scanner tap, both thumbs,
    # an error page, and a bot's error page.
    q = Message(student_id=1, role="user", course="AST101", lecture_num=3, content="why?", created_at=T)
    s.add(q)
    s.flush()
    for seq, (name, detail) in enumerate((("page_view", {}), ("question_sent", {"kind": "typed"}),
                                          ("reply_done", {"outcome": "error"}), ("reply_done", {"outcome": "busy"}),
                                          ("deck_open", {"slides_total": 20, "context": "chat"}),
                                          ("slide_view", {"slide_num": 2, "how": "first"}),
                                          ("page_heartbeat", {})), start=1):
        _event(s, 1, name, T + datetime.timedelta(seconds=seq * 20), seq, **detail)
    s.add(TurnEvent(student_id=1, kind="chat", cost_usd=0.02, latency_ms=9000, ts=T, turn_id="t1",
                    verdicts_json=json.dumps({"verdict": "block", "detector_cut": False})))
    s.add(TurnEvent(student_id=1, kind="classify", cost_usd=0.001, ts=T))
    s.add(TurnEvent(student_id=1, kind="cache_warm", cost_usd=0.5, ts=T))
    s.add(TurnEvent(student_id=1, kind="chat", cost_usd=0.01, latency_ms=1000, ts=T + datetime.timedelta(minutes=1),
                    turn_id="t2", verdicts_json=json.dumps({"verdict": "detector_cut", "detector_cut": True})))
    s.add(LoginSession(student_id=1, jwt_jti="j1", expires_at=T + datetime.timedelta(days=7), method="magic",
                       created_at=T))
    s.add(LoginAttempt(email="ada@example.edu", student_id=None, method=attempts.MAGIC, outcome=attempts.CODE_INVALID,
                       user_agent=CHROME, created_at=T))
    s.add(LoginAttempt(email="ada@example.edu", student_id=1, method=attempts.MAGIC, outcome=attempts.OK,
                       user_agent=CHROME, created_at=T))
    s.add(LoginAttempt(email="ada@example.edu", student_id=1, method=attempts.MAGIC, outcome=attempts.LINK_INVALID,
                       user_agent=SCANNER, created_at=T))
    s.add(FeedbackEvent(message_id=q.id, student_id=1, rating=1, ts=T))
    s.add(FeedbackEvent(message_id=q.id, student_id=1, rating=-1, ts=T + datetime.timedelta(seconds=5)))
    s.add(RequestError(ts=T, status=404, method="GET", path="/slides/AST101/lecture-99/index.html",
                       student_id=1, kind="deck", agent="browser", ref="abc12345"))
    s.add(RequestError(ts=T, status=404, method="GET", path="/x", student_id=1, kind="page", agent="bot",
                       ref="abc12346"))
    # Bo, just after midnight: one question on Oct 6. The professor asks too.
    s.add(Message(student_id=2, role="user", course="AST102", lecture_num=1, content="hi", created_at=T2))
    s.add(Message(student_id=9, role="user", course="AST101", lecture_num=1, content="test", created_at=T2))
    s.commit()
    yield s
    s.close()


def test_compute_folds_each_signal_by_local_day(db):
    rows = student_days.compute(db, T - datetime.timedelta(hours=1), T2 + datetime.timedelta(hours=1), slow_ms=5000)
    by = {(r.student_id, r.day): r for r in rows}
    assert set(by) == {(1, OCT5), (2, OCT6), (9, OCT6)}
    ada = by[(1, OCT5)]
    assert (ada.questions, ada.turns, ada.course) == (1, 2, "AST101")
    assert ada.cost_usd == pytest.approx(0.031)                    # the cache warm-up never counts
    assert (ada.error_outcomes, ada.busy_outcomes, ada.detector_cuts, ada.blocks, ada.slow_turns) == (1, 1, 1, 1, 1)
    assert (ada.logins, ada.failed_logins, ada.user_agents) == (1, 1, 1)   # the scanner's tap is neither
    assert (ada.thumbs_up, ada.thumbs_down, ada.request_errors) == (1, 1, 1)
    assert (ada.visits, ada.lectures_opened, ada.slides_seen) == (1, 1, 1)
    assert ada.active_min > 0 and ada.deck_min >= 0
    assert ada.hours_mask == 1 << 23
    bo = by[(2, OCT6)]
    assert (bo.questions, bo.course, bo.hours_mask, bo.turns) == (1, "AST102", 1 << 0, 0)
    assert bo.empty is False and student_days.DayRow(student_id=2, day=OCT6).empty is True


def test_write_replaces_a_days_rows_and_refresh_is_throttled(db, monkeypatch):
    rows = student_days.compute(db, T - datetime.timedelta(hours=1), T2 + datetime.timedelta(hours=1))
    assert student_days.write(db, rows, [OCT5, OCT6]) == 3
    db.commit()
    assert db.query(StudentDay).count() == 3
    # Yesterday's rows are replaced, not duplicated.
    db.add(Message(student_id=1, role="user", course="AST101", lecture_num=3, content="again", created_at=T + datetime.timedelta(seconds=1)))
    db.commit()
    rows = student_days.compute(db, T - datetime.timedelta(hours=1), T2 + datetime.timedelta(hours=1))
    student_days.write(db, rows, [OCT5, OCT6])
    db.commit()
    stored = {(r.student_id, r.day): r for r in db.query(StudentDay).all()}
    assert len(stored) == 3 and stored[(1, OCT5)].questions == 2
    # refresh: the first pass writes, the second within the throttle does nothing, force recomputes.
    db.query(StudentDay).delete()
    db.commit()
    now = T2 + datetime.timedelta(hours=2)
    assert student_days.refresh(db, now=now) == 3
    assert student_days.refresh(db, now=now + datetime.timedelta(minutes=1)) == 0
    assert student_days.refresh(db, now=now + datetime.timedelta(minutes=1), force=True) == 3
    # A later pass recomputes today and yesterday only; a failure never raises.
    later = now + student_days.REFRESH_EVERY + datetime.timedelta(minutes=1)
    assert student_days.refresh(db, now=later) == 3
    monkeypatch.setattr(student_days, "compute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert student_days.refresh(db, now=later + student_days.REFRESH_EVERY) == 0
    assert db.query(StudentDay).count() == 3


def test_a_day_computed_while_still_today_is_recomputed_later(db):
    """refresh remembered a partial "today" as done, so a day nobody opened a
    page on the day after stayed frozen for the life of the process (the
    review of step 0.9): only a day whose midnight has passed is done."""
    first = T2 + datetime.timedelta(hours=1)                 # Oct 6, 01:30 PDT: Oct 6 is still today
    assert student_days.refresh(db, now=first) == 3
    db.add(Message(student_id=2, role="user", course="AST102", lecture_num=1, content="later that day",
                   created_at=T2 + datetime.timedelta(hours=6)))
    db.commit()
    # Two days on, Oct 6 is no longer today or yesterday: it is recomputed anyway.
    later = first + datetime.timedelta(days=2)
    assert student_days.refresh(db, now=later) >= 1
    (bo,) = [r for r in db.query(StudentDay).all() if (r.student_id, r.day) == (2, OCT6)]
    assert bo.questions == 2


def test_a_strangers_typing_is_not_a_failed_login(db):
    db.add(LoginAttempt(email="bo@example.edu", student_id=None, method=attempts.MAGIC, outcome=attempts.CODE_INVALID,
                        user_agent=CHROME, detail="no live link", created_at=T2))
    db.add(LoginAttempt(email="bo@example.edu", student_id=None, method=attempts.MAGIC, outcome=attempts.RATE_LIMITED,
                        user_agent=CHROME, detail="code", created_at=T2))
    db.commit()
    rows = student_days.compute(db, T - datetime.timedelta(hours=1), T2 + datetime.timedelta(hours=1))
    (bo,) = [r for r in rows if (r.student_id, r.day) == (2, OCT6)]
    assert (bo.failed_logins, bo.user_agents) == (0, 1)


def test_totals_and_extremes_rank_without_judging(db):
    rows = student_days.compute(db, T - datetime.timedelta(hours=1), T2 + datetime.timedelta(hours=1))
    student_days.write(db, rows, [OCT5, OCT6])
    db.commit()
    totals = student_days.totals(db, [1, 2, 3], OCT5, OCT6)
    assert totals[1]["questions"] == 1 and totals[2]["questions"] == 1 and totals[3]["questions"] == 0
    assert totals[1]["hours_mask"] == 1 << 23
    ex = {e.key: e for e in student_days.extremes(db, [1, 2, 3], OCT5, OCT6)}
    assert [k for k, _ in student_days.OUTLIER_METRICS] == list(ex)
    assert ex["questions"].highest == [(1, 1), (2, 1)] and ex["questions"].lowest == [] and ex["questions"].without == 1
    assert ex["errors"].highest == [(1, 3)] and ex["errors"].without == 2
    assert ex["failed_logins"].highest == [(1, 1)]
    assert student_days.extremes(db, [3], OCT5, OCT6) == []


def test_the_backfill_prints_without_writing_on_a_dry_run(db, capsys):
    from tutor.db.models import get_engine, get_session_factory, init_db

    engine = get_engine()                # the suite's temporary SQLite (tests/conftest.py)
    init_db(engine)
    session = get_session_factory(engine)()
    try:
        before = session.query(StudentDay).count()
    finally:
        session.close()
    assert student_days.main(["--days", "3", "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    session = get_session_factory(engine)()
    try:
        assert session.query(StudentDay).count() == before
    finally:
        session.close()
