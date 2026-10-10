"""The per-student daily rollup (step 0.9; F37, F42, F43, F46).

One ``student_days`` row per student per local calendar day with any
activity, folded from the raw tables (messages, the ledger, the browser's
activity events, sessions, the sign-in trail, the feedback history, the
error pages) by the same pure functions the offline analysis uses, so the
live pages and the local store read the same numbers. A day without
activity has no row: readers treat a missing row as zeros.

The app writes it: :func:`refresh` runs from the admin Today panel and
the instructor dashboard, throttled to one pass per :data:`REFRESH_EVERY`
per process (single-instance state, like the other in-memory limits in
CLAUDE.md), recomputing today and yesterday on every pass (their rows are
still landing) and each older day of its range once. The daily report
workflow stays read-only, which is why the report does not write it.
``python -m tutor.student_days --days N`` backfills by hand.

Rows carry ids and counts only, never text, so they stay under the
pseudonym when a student is anonymized (``state.ANONYMIZE_POLICY``).
"""

from __future__ import annotations

import argparse
import collections
import datetime
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy.orm import Session as DBSession

from tutor.db.models import (
    FeedbackEvent,
    LoginAttempt,
    RequestError,
    Session as LoginSession,
    Student,
    StudentDay,
    safe_json_loads,
)
from tutor.utctime import local_day, to_local, utcnow

log = logging.getLogger("tutor.student_days")

REFRESH_EVERY = datetime.timedelta(minutes=15)
REFRESH_DAYS = 8          # today, yesterday and the six days before: the Today panel's week
RECOMPUTE_DAYS = 2        # today and yesterday are recomputed on every pass

# The signal columns, in the table's order, with what each counts.
METRICS: tuple[tuple[str, str], ...] = (
    ("questions", "questions sent to the tutor"),
    ("turns", "chat turns in the ledger"),
    ("cost_usd", "model cost in dollars, cache warm-ups excluded"),
    ("active_min", "minutes with the site on screen"),
    ("deck_min", "minutes with a slide deck on screen"),
    ("narration_min", "minutes of lecture narration played"),
    ("visits", "runs of activity with no idle gap over the threshold, cut at local midnight"),
    ("slides_seen", "distinct slides shown"),
    ("lectures_opened", "distinct lectures opened"),
    ("error_outcomes", "replies the browser saw end in an error"),
    ("busy_outcomes", "questions refused because a turn was in flight"),
    ("detector_cuts", "replies the detector cut"),
    ("blocks", "replies the judge blocked"),
    ("slow_turns", "chat turns over SLOW_TURN_MS"),
    ("thumbs_up", "thumbs up given"),
    ("thumbs_down", "thumbs down given"),
    ("logins", "sessions started"),
    ("failed_logins", "sign-in failures from a browser (a scanner's never count)"),
    ("user_agents", "distinct browsers that signed in or tried"),
    ("request_errors", "error pages hit"),
    ("hours_mask", "the local hours with any signal, 24 bits, bit 0 midnight"),
)
METRIC_KEYS = tuple(k for k, _ in METRICS)
_AMOUNTS = frozenset({"cost_usd", "active_min", "deck_min", "narration_min"})


@dataclass
class DayRow:
    student_id: int
    day: datetime.date
    course: Optional[str] = None
    questions: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    active_min: float = 0.0
    deck_min: float = 0.0
    narration_min: float = 0.0
    visits: int = 0
    slides_seen: int = 0
    lectures_opened: int = 0
    error_outcomes: int = 0
    busy_outcomes: int = 0
    detector_cuts: int = 0
    blocks: int = 0
    slow_turns: int = 0
    thumbs_up: int = 0
    thumbs_down: int = 0
    logins: int = 0
    failed_logins: int = 0
    user_agents: int = 0
    request_errors: int = 0
    hours_mask: int = 0
    _courses: collections.Counter = field(default_factory=collections.Counter, repr=False)
    _agents: set = field(default_factory=set, repr=False)

    def values(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in METRIC_KEYS}

    @property
    def empty(self) -> bool:
        return not any(getattr(self, k) for k in METRIC_KEYS)


def local_date(ts: datetime.datetime) -> datetime.date:
    """The local calendar day of a naive-UTC instant."""
    return to_local(ts).date()


def day_bounds(day: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    """A local calendar day as naive-UTC ``[since, until)``."""
    return local_day(day.isoformat())


def _student_ids(db: DBSession, student_ids: Optional[Sequence[int]]) -> list[int]:
    if student_ids is not None:
        return [int(s) for s in student_ids]
    return [int(r[0]) for r in db.query(Student.id).all()]


def compute(db: DBSession, since: datetime.datetime, until: datetime.datetime,
            student_ids: Optional[Sequence[int]] = None, slow_ms: Optional[int] = None) -> list[DayRow]:
    """Every (student, local day) with activity in the naive-UTC window,
    from the raw tables. Reads only."""
    from tutor import engagement as engagement_mod
    from tutor.analytics import lectures as lectures_mod
    from tutor.analytics import visits as visits_mod
    from tutor.auth import attempts as attempts_mod
    from tutor.db.models import TurnEvent
    from tutor.logging_config import slow_turn_ms
    from tutor.request_errors import BROWSER, classify_agent

    ids = _student_ids(db, student_ids)
    if not ids:
        return []
    slow = slow_turn_ms() if slow_ms is None else slow_ms
    rows: dict[tuple[int, datetime.date], DayRow] = {}

    def row(sid: int, ts: datetime.datetime) -> DayRow:
        key = (sid, local_date(ts))
        r = rows.get(key)
        if r is None:
            r = rows[key] = DayRow(student_id=sid, day=key[1])
        r.hours_mask |= 1 << to_local(ts).hour
        return r

    # Questions, replies and the browser's events, as the dashboard sees them.
    thresholds = engagement_mod.live_thresholds()
    material = engagement_mod._material()
    for sid, signals in engagement_mod._signals(db, ids, since, until).items():
        by_day: dict[datetime.date, list] = collections.defaultdict(list)
        for sig in signals:
            by_day[local_date(sig.ts)].append(sig)
            r = row(sid, sig.ts)
            if sig.source == "question":
                r.questions += 1
                r._courses[sig.course or ""] += 1
            elif sig.source == "event":
                if sig.course:
                    r._courses[sig.course] += 0       # a course seen, for the tie-break below
                if sig.name == "reply_done":
                    outcome = sig.detail.get("outcome")
                    if outcome == "error":
                        r.error_outcomes += 1
                    elif outcome == "busy":
                        r.busy_outcomes += 1
        for day, sigs in by_day.items():
            r = rows[(sid, day)]
            visits = [visits_mod.build_visit(sid, run, thresholds) for run in visits_mod.split_visits(sigs, thresholds)]
            totals = visits_mod.summarize_visits(visits)
            r.visits = int(totals["visits"])
            r.active_min = round(float(totals["active_min"]), 1)
            r.deck_min = round(float(totals["deck_min"]), 1)
            r.narration_min = round(float(totals["narration_min"]), 1)
            uses = lectures_mod.accumulate(sigs, material)
            r.lectures_opened = sum(1 for u in uses.values() if u.opens or u.catalog_opens)
            r.slides_seen = sum(u.slides_seen for u in uses.values())

    # The ledger: turns, cost, what supervision did, and the slow ones.
    for sid, kind, cost, latency, verdicts_json, ts in (
        db.query(TurnEvent.student_id, TurnEvent.kind, TurnEvent.cost_usd, TurnEvent.latency_ms,
                 TurnEvent.verdicts_json, TurnEvent.ts)
        .filter(TurnEvent.student_id.in_(ids), TurnEvent.ts >= since, TurnEvent.ts < until)
        .all()
    ):
        if ts is None:
            continue
        r = row(sid, ts)
        if kind != "cache_warm":
            r.cost_usd += float(cost or 0.0)
        if kind != "chat":
            continue
        r.turns += 1
        if latency is not None and latency >= slow:
            r.slow_turns += 1
        verdicts = safe_json_loads(verdicts_json, {}) or {}
        if not isinstance(verdicts, dict):
            continue
        if verdicts.get("detector_cut") or verdicts.get("verdict") == "detector_cut":
            r.detector_cuts += 1
        if verdicts.get("verdict") == "block":
            r.blocks += 1

    # Logins, failures and browsers.
    for sid, ts in (
        db.query(LoginSession.student_id, LoginSession.created_at)
        .filter(LoginSession.student_id.in_(ids), LoginSession.created_at >= since,
                LoginSession.created_at < until)
        .all()
    ):
        if ts is not None:
            row(sid, ts).logins += 1
    by_email = {s.email.lower(): s.id for s in db.query(Student).filter(Student.id.in_(ids)).all()}
    for email, sid, outcome, detail, agent, ts in (
        db.query(LoginAttempt.email, LoginAttempt.student_id, LoginAttempt.outcome, LoginAttempt.detail,
                 LoginAttempt.user_agent, LoginAttempt.created_at)
        .filter(LoginAttempt.created_at >= since, LoginAttempt.created_at < until)
        .all()
    ):
        who = sid if sid in by_email.values() else by_email.get((email or "").strip().lower())
        if who is None or ts is None or classify_agent(agent) != BROWSER:
            continue
        r = row(who, ts)
        if agent:
            r._agents.add(agent.strip()[:255])
        # The same rule as the alert and the roster: a failure anyone could
        # produce by typing the address is not the student's.
        if outcome in attempts_mod.FAILED and attempts_mod.attributable(outcome, detail):
            r.failed_logins += 1

    # Every rating given, in order (the history, not the live row).
    for sid, rating, ts in (
        db.query(FeedbackEvent.student_id, FeedbackEvent.rating, FeedbackEvent.ts)
        .filter(FeedbackEvent.student_id.in_(ids), FeedbackEvent.ts >= since, FeedbackEvent.ts < until)
        .all()
    ):
        if ts is None:
            continue
        r = row(sid, ts)
        if (rating or 0) > 0:
            r.thumbs_up += 1
        else:
            r.thumbs_down += 1

    # Error pages a signed-in browser hit (a bot's or a scanner's never count).
    for sid, ts in (
        db.query(RequestError.student_id, RequestError.ts)
        .filter(RequestError.student_id.in_(ids), RequestError.agent == BROWSER,
                RequestError.ts >= since, RequestError.ts < until)
        .all()
    ):
        if ts is not None:
            row(sid, ts).request_errors += 1

    out = []
    for r in rows.values():
        r.user_agents = len(r._agents)
        r.cost_usd = round(r.cost_usd, 4)
        courses = [(n, c) for c, n in r._courses.items() if c]
        r.course = max(courses, key=lambda t: (t[0], t[1]))[1] if courses else None
        if not r.empty:
            out.append(r)
    out.sort(key=lambda r: (r.day, r.student_id))
    return out


def write(db: DBSession, rows: Iterable[DayRow], days: Iterable[datetime.date],
          student_ids: Optional[Sequence[int]] = None) -> int:
    """Replace the stored rows for ``days`` (for ``student_ids`` only, when
    given) with ``rows``. Flushes; the caller commits."""
    days = sorted(set(days))
    if not days:
        return 0
    q = db.query(StudentDay).filter(StudentDay.day.in_(days))
    if student_ids is not None:
        q = q.filter(StudentDay.student_id.in_([int(s) for s in student_ids]))
    q.delete(synchronize_session=False)
    now = utcnow()
    n = 0
    for r in rows:
        if r.day not in days:
            continue
        db.add(StudentDay(student_id=r.student_id, day=r.day, course=r.course, computed_at=now, **r.values()))
        n += 1
    db.flush()
    return n


# ---------------------------------------------------------------------------
# The in-app refresh
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_last_refresh: Optional[datetime.datetime] = None
_done_days: set[datetime.date] = set()


def refresh(db: DBSession, *, now: Optional[datetime.datetime] = None, days: int = REFRESH_DAYS,
            force: bool = False) -> int:
    """Bring the last ``days`` local days up to date, at most once per
    :data:`REFRESH_EVERY` per process unless ``force``. Commits. Never
    raises: a page must render even when the rollup cannot be written.
    Returns the rows written."""
    global _last_refresh
    now = now or utcnow()
    with _lock:
        if not force and _last_refresh is not None and now - _last_refresh < REFRESH_EVERY:
            return 0
        today = local_date(now)
        wanted = [today - datetime.timedelta(days=i) for i in range(max(1, days))]
        todo = [d for i, d in enumerate(wanted) if force or i < RECOMPUTE_DAYS or d not in _done_days]
        if not todo:
            _last_refresh = now
            return 0
        since = day_bounds(min(todo))[0]
        until = min(day_bounds(max(todo))[1], now + datetime.timedelta(minutes=1))
        try:
            rows = compute(db, since, until)
            n = write(db, rows, todo)
            db.commit()
        except Exception:  # noqa: BLE001 - the page renders without the rollup
            db.rollback()
            log.warning("student_days refresh failed", exc_info=True)
            return 0
        # A day computed while it was still today is not finished: only a
        # day whose midnight has passed is remembered as done.
        _done_days.update(d for d in todo if day_bounds(d)[1] <= now)
        _last_refresh = now
        return n


def _reset_for_tests() -> None:
    global _last_refresh
    _last_refresh = None
    _done_days.clear()


# ---------------------------------------------------------------------------
# Reading the rollup
# ---------------------------------------------------------------------------

def rows(db: DBSession, student_ids: Sequence[int], since_day: datetime.date,
         until_day: datetime.date) -> list[StudentDay]:
    """The stored rows for ``student_ids`` over ``[since_day, until_day]``."""
    if not student_ids:
        return []
    return (
        db.query(StudentDay)
        .filter(StudentDay.student_id.in_([int(s) for s in student_ids]),
                StudentDay.day >= since_day, StudentDay.day <= until_day)
        .order_by(StudentDay.day, StudentDay.student_id)
        .all()
    )


def totals(db: DBSession, student_ids: Sequence[int], since_day: datetime.date,
           until_day: datetime.date) -> dict[int, dict[str, float]]:
    """Each student's metrics summed over the days (the hours mask OR-ed);
    a student with no row has every metric at zero."""
    out: dict[int, dict[str, float]] = {int(s): {k: 0 for k in METRIC_KEYS} for s in student_ids}
    for r in rows(db, student_ids, since_day, until_day):
        t = out[r.student_id]
        for k in METRIC_KEYS:
            if k == "hours_mask":
                t[k] = int(t[k]) | int(r.hours_mask)
            else:
                t[k] += getattr(r, k)
    for t in out.values():
        for k in _AMOUNTS:
            t[k] = round(t[k], 2)
    return out


# The Today panel's "where students stand" measures, from the rollup.
OUTLIER_METRICS: tuple[tuple[str, str], ...] = (
    ("questions", "Questions"),
    ("active_min", "Minutes on screen"),
    ("deck_min", "Deck minutes"),
    ("narration_min", "Narration minutes"),
    ("visits", "Visits"),
    ("thumbs_down", "Thumbs down"),
    ("errors", "Errors and busy refusals"),
    ("failed_logins", "Failed sign-ins"),
)


@dataclass
class Extreme:
    key: str
    label: str
    highest: list[tuple[int, float]] = field(default_factory=list)   # (student_id, value), largest first
    lowest: list[tuple[int, float]] = field(default_factory=list)    # among students with any, smallest first
    without: int = 0                                                 # students at zero


def extremes(db: DBSession, student_ids: Sequence[int], since_day: datetime.date,
             until_day: datetime.date, n: int = 2) -> list[Extreme]:
    """Per measure, the ``n`` highest and ``n`` lowest students over the
    days, among those with any; a rank, never a judgement. Empty when no
    student has a row."""
    sums = totals(db, student_ids, since_day, until_day)
    if not any(any(v for k, v in t.items() if k != "hours_mask") for t in sums.values()):
        return []
    out = []
    for key, label in OUTLIER_METRICS:
        def value(t: dict[str, float]) -> float:
            if key == "errors":
                return t["error_outcomes"] + t["busy_outcomes"] + t["request_errors"]
            return t[key]
        scored = sorted(((sid, value(t)) for sid, t in sums.items()), key=lambda p: (-p[1], p[0]))
        with_any = [p for p in scored if p[1] > 0]
        highest = with_any[:n]
        taken = {sid for sid, _ in highest}
        lowest = [p for p in reversed(with_any) if p[0] not in taken][:n]
        out.append(Extreme(key=key, label=label, highest=highest, lowest=lowest,
                           without=len(scored) - len(with_any)))
    return out


# ---------------------------------------------------------------------------
# The backfill
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """``python -m tutor.student_days --days 30``: recompute and store the
    last N local days (today included) against DATABASE_URL."""
    p = argparse.ArgumentParser(description="Recompute the per-student daily rollup")
    p.add_argument("--days", type=int, default=30, help="local days to recompute, today included")
    p.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    args = p.parse_args(argv)
    from tutor.db.models import get_engine, get_session_factory

    db = get_session_factory(get_engine())()
    try:
        today = local_date(utcnow())
        days = [today - datetime.timedelta(days=i) for i in range(max(1, args.days))]
        since, until = day_bounds(min(days))[0], day_bounds(max(days))[1]
        computed = compute(db, since, until)
        per_day = collections.Counter(r.day for r in computed)
        for day in sorted(per_day):
            print(f"{day}: {per_day[day]} student(s)")
        if args.dry_run:
            print(f"dry run: {len(computed)} row(s) over {len(days)} day(s), nothing written")
            return 0
        n = write(db, computed, days)
        db.commit()
        print(f"student_days: wrote {n} row(s) over {len(days)} day(s)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
