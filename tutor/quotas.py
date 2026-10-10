"""Per-student dollar quotas on model spend: a day, a week, a month.

The class-wide monthly cap (``MONTHLY_COST_CAP_USD``) stops everyone when the
course's budget is gone; these stop one student whose bad day would spend it.
Each quota is a dollar figure an admin edits on the Supervision tab (the
``settings`` table; 0 turns one off), compared with the sum of every model
call ledgered under the student in the window: the tutor, the classifier,
the judge, the strategist, the summarizer, the exam grader, review grading,
the rubric evaluator, photo transcription. The prompt-cache warm-up is left
out: opening the chat page triggers it, not the student's use.

Windows are calendar periods in the class's time zone (``tutor.timezone`` in
courses.yaml), so the message can say when the quota comes back: the day
resets at local midnight, the week on Monday, the month on the 1st. The
check runs where the other caps run (``usage.over_caps`` inside every turn,
``usage.over_review_caps`` before a graded review, the photo route), admins
are exempt, and a turn already running when the quota is crossed finishes.
A hit is recorded once per student and window as a zero-cost
``turn_events`` row of kind ``quota``, which the admin Today panel, the
Supervision tab, and the roll-up email (tutor/quota_report.py) read.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from tutor.db.models import Setting, Student, TurnEvent
from tutor.utctime import _local_tz, to_local, utcnow

WINDOWS: tuple[str, ...] = ("day", "week", "month")
DEFAULTS: dict[str, float] = {"day": 1.50, "week": 6.00, "month": 15.00}
PERIODS: tuple[str, ...] = ("daily", "weekly", "monthly")
NOTIFY_DEFAULTS: dict[str, bool] = {"daily": False, "weekly": True, "monthly": True}
INDICATOR_FROM = 0.8          # the chat shows the remaining budget from this fraction

# Ledger kinds that never count against a student: not their doing.
EXCLUDED_KINDS: tuple[str, ...] = ("cache_warm", "quota")
HIT_KIND = "quota"

_WINDOW_WORDS = {"day": "today's", "week": "this week's", "month": "this month's"}


def _setting_key(window: str) -> str:
    return f"quota_{window}_usd"


def _notify_key(period: str) -> str:
    return f"quota_notify_{period}"


# ---------------------------------------------------------------------------
# Windows (local calendar periods, as naive-UTC instants like the ledger)
# ---------------------------------------------------------------------------

def _to_utc(local: datetime.datetime) -> datetime.datetime:
    return local.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def window_bounds(window: str, now: Optional[datetime.datetime] = None) -> tuple[datetime.datetime, datetime.datetime]:
    """``(start, reset)`` of the local calendar period holding ``now``, as
    naive UTC: the day from midnight to midnight, the week from Monday, the
    month from the 1st. Across a daylight-saving change the day is 23 or 25
    hours long, as a day on the wall clock is."""
    now = now or utcnow()
    local = to_local(now)
    tz = _local_tz()
    if window == "day":
        start_local = datetime.datetime(local.year, local.month, local.day, tzinfo=tz)
        reset_local = (start_local.replace(tzinfo=None) + datetime.timedelta(days=1)).replace(tzinfo=tz)
    elif window == "week":
        monday = local.date() - datetime.timedelta(days=local.weekday())
        start_local = datetime.datetime(monday.year, monday.month, monday.day, tzinfo=tz)
        reset_local = (start_local.replace(tzinfo=None) + datetime.timedelta(days=7)).replace(tzinfo=tz)
    elif window == "month":
        start_local = datetime.datetime(local.year, local.month, 1, tzinfo=tz)
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        reset_local = datetime.datetime(year, month, 1, tzinfo=tz)
    else:
        raise ValueError(f"unknown window {window!r}")
    return _to_utc(start_local), _to_utc(reset_local)


def previous_period(period: str, now: Optional[datetime.datetime] = None) -> tuple[datetime.datetime, datetime.datetime]:
    """The last completed period before ``now`` (yesterday, last week, last
    month), for the roll-up email."""
    window = {"daily": "day", "weekly": "week", "monthly": "month"}[period]
    start, _reset = window_bounds(window, now)
    before = start - datetime.timedelta(seconds=1)
    return window_bounds(window, before)


def period_is_due(period: str, now: Optional[datetime.datetime] = None) -> bool:
    """Whether a roll-up for ``period`` is due today (local time): every day for
    the daily one, Mondays for the weekly, the 1st for the monthly."""
    local = to_local(now or utcnow())
    if period == "daily":
        return True
    if period == "weekly":
        return local.weekday() == 0
    if period == "monthly":
        return local.day == 1
    raise ValueError(f"unknown period {period!r}")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _float_setting(db: DBSession, key: str, default: float) -> float:
    row = db.get(Setting, key)
    if row is None or not str(row.value).strip():
        return default
    try:
        return max(0.0, float(row.value))
    except (TypeError, ValueError):
        return default


def load_quotas(db: DBSession) -> dict[str, float]:
    """``{"day": usd, "week": usd, "month": usd}``; 0 means that quota is off."""
    return {w: _float_setting(db, _setting_key(w), DEFAULTS[w]) for w in WINDOWS}


def save_quotas(db: DBSession, values: dict[str, float]) -> None:
    for w in WINDOWS:
        if w in values:
            db.merge(Setting(key=_setting_key(w), value=f"{max(0.0, float(values[w])):.2f}"))
    db.flush()


def load_notify(db: DBSession) -> dict[str, bool]:
    """Which roll-up emails are on: ``{"daily": bool, "weekly": bool, "monthly": bool}``."""
    out = {}
    for p in PERIODS:
        row = db.get(Setting, _notify_key(p))
        out[p] = NOTIFY_DEFAULTS[p] if row is None else str(row.value).strip().lower() == "true"
    return out


def save_notify(db: DBSession, flags: dict[str, bool]) -> None:
    for p in PERIODS:
        if p in flags:
            db.merge(Setting(key=_notify_key(p), value="true" if flags[p] else "false"))
    db.flush()


# ---------------------------------------------------------------------------
# Spend and status
# ---------------------------------------------------------------------------

def spent(db: DBSession, student_id: int, since: datetime.datetime,
          until: Optional[datetime.datetime] = None) -> float:
    """Dollars ledgered under the student since ``since`` (warm-ups excluded)."""
    q = (
        db.query(func.coalesce(func.sum(TurnEvent.cost_usd), 0.0))
        .filter(TurnEvent.student_id == student_id, TurnEvent.ts >= since,
                TurnEvent.kind.notin_(EXCLUDED_KINDS))
    )
    if until is not None:
        q = q.filter(TurnEvent.ts < until)
    return float(q.scalar() or 0.0)


def spent_by_student(db: DBSession, since: datetime.datetime) -> dict[int, float]:
    """Every student's spend since ``since`` in one query (the admin table)."""
    rows = (
        db.query(TurnEvent.student_id, func.sum(TurnEvent.cost_usd))
        .filter(TurnEvent.ts >= since, TurnEvent.kind.notin_(EXCLUDED_KINDS))
        .group_by(TurnEvent.student_id)
        .all()
    )
    return {int(sid): float(total or 0.0) for sid, total in rows}


@dataclass
class QuotaStatus:
    window: str
    limit_usd: float
    spent_usd: float
    start: datetime.datetime
    resets_at: datetime.datetime

    @property
    def fraction(self) -> float:
        return (self.spent_usd / self.limit_usd) if self.limit_usd > 0 else 0.0

    @property
    def over(self) -> bool:
        return self.limit_usd > 0 and self.spent_usd >= self.limit_usd

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    def resets_in(self, now: Optional[datetime.datetime] = None) -> datetime.timedelta:
        return max(datetime.timedelta(0), self.resets_at - (now or utcnow()))

    def as_dict(self, now: Optional[datetime.datetime] = None) -> dict[str, Any]:
        now = now or utcnow()
        return {
            "window": self.window, "limit_usd": round(self.limit_usd, 2), "spent_usd": round(self.spent_usd, 4),
            "fraction": round(min(self.fraction, 9.99), 3), "over": self.over,
            "resets_at": self.resets_at.replace(tzinfo=datetime.timezone.utc).isoformat(),
            "resets_in_s": int(self.resets_in(now).total_seconds()), "resets": reset_phrase(self, now),
        }


def statuses(db: DBSession, student_id: int, now: Optional[datetime.datetime] = None,
             quotas: Optional[dict[str, float]] = None) -> list[QuotaStatus]:
    """One status per quota that is on, day first."""
    now = now or utcnow()
    quotas = quotas if quotas is not None else load_quotas(db)
    out = []
    for w in WINDOWS:
        limit = quotas.get(w, 0.0)
        if limit <= 0:
            continue
        start, reset = window_bounds(w, now)
        out.append(QuotaStatus(window=w, limit_usd=limit, spent_usd=spent(db, student_id, start), start=start,
                               resets_at=reset))
    return out


def is_exempt(db: DBSession, student_id: int, email: Optional[str] = None) -> bool:
    """Admins are exempt (ADMIN_EMAILS); everyone else, instructors included, is not."""
    from tutor.auth.access import is_admin

    if email is None:
        row = db.query(Student.email).filter(Student.id == student_id).first()
        email = row[0] if row else None
    return is_admin(email)


def blocking(db: DBSession, student_id: int, now: Optional[datetime.datetime] = None,
             email: Optional[str] = None) -> Optional[QuotaStatus]:
    """The quota the student is over that comes back last, or None; None
    too for an admin. Over both the day and the week, "midnight" would be
    wrong: the week keeps them out until Monday (the Stage 3 review)."""
    if is_exempt(db, student_id, email):
        return None
    return max((s for s in statuses(db, student_id, now) if s.over), key=lambda s: s.resets_at, default=None)


# ---------------------------------------------------------------------------
# What the student reads
# ---------------------------------------------------------------------------

def _in_words(resets_at: datetime.datetime, now: datetime.datetime) -> str:
    """The wait until ``resets_at``: minutes under an hour and a half, hours
    under a day and a half, else local calendar days (a Tuesday afternoon
    is "in 6 days" from Monday, the way a student counts, not 5.5 rounded)."""
    total = max(0, int((resets_at - now).total_seconds()))
    if total < 90 * 60:
        minutes = max(1, round(total / 60))
        return f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = total / 3600
    if hours < 36:
        h = round(hours)
        return f"in about {h} hour{'s' if h != 1 else ''}"
    days = (to_local(resets_at).date() - to_local(now).date()).days
    return f"in {days} day{'s' if days != 1 else ''}"


def reset_phrase(status: QuotaStatus, now: Optional[datetime.datetime] = None) -> str:
    """When the quota comes back, in local time with the wait in words."""
    return when_phrase(status.window, status.resets_at, now)


def when_phrase(window: str, resets_at: datetime.datetime, now: Optional[datetime.datetime] = None) -> str:
    """When a window that ends at ``resets_at`` (naive UTC) comes back, in
    local time (the zone's abbreviation, from courses.yaml) with the wait
    in words: a quota's day, week or month, or a class cap (R25: the class
    caps never said)."""
    now = now or utcnow()
    local = to_local(resets_at)
    wait = _in_words(resets_at, now)
    if window == "day":
        return f"at midnight {local:%Z} ({wait})"
    if window == "rolling":
        hour = local.hour % 12 or 12
        return f"at {hour}:{local.minute:02d} {'PM' if local.hour >= 12 else 'AM'} {local:%Z} ({wait})"
    if window == "week":
        return f"{local:%A} at midnight {local:%Z} ({wait})"
    if local.hour or local.minute:
        # The class's monthly cap runs on the UTC month: it lifts at UTC
        # midnight, whatever local hour that is.
        hour = local.hour % 12 or 12
        return f"on {local:%B} {local.day} at {hour} {'PM' if local.hour >= 12 else 'AM'} {local:%Z} ({wait})"
    return f"on {local:%B} {local.day} ({wait})"


def message(status: QuotaStatus, now: Optional[datetime.datetime] = None) -> str:
    """The in-voice reply a blocked student gets, with the reset time."""
    when = reset_phrase(status, now)
    verb = "comes back" if status.window == "day" else "resets"
    return (f"That is {_WINDOW_WORDS[status.window]} tutor budget used up. It {verb} {when}. "
            f"The slides and the book are still open, and so is the review page's reading. :-)")


def indicator(db: DBSession, student_id: int, now: Optional[datetime.datetime] = None,
              email: Optional[str] = None) -> dict[str, Any]:
    """What the chat page shows: every quota's state, a note once any window
    passes INDICATOR_FROM, and the blocking message when one is over."""
    now = now or utcnow()
    if is_exempt(db, student_id, email):
        return {"windows": [], "note": None, "blocked": None, "exempt": True}
    rows = statuses(db, student_id, now)
    over = max((s for s in rows if s.over), key=lambda s: s.resets_at, default=None)
    nearest = max(rows, key=lambda s: s.fraction, default=None)
    note = None
    if over is not None:
        note = message(over, now)
    elif nearest is not None and nearest.fraction >= INDICATOR_FROM:
        pct = min(99, int(nearest.fraction * 100))
        word = _WINDOW_WORDS[nearest.window]
        note = (f"You have used {pct}% of {word} tutor budget "
                f"(${nearest.remaining_usd:.2f} left). It {'comes back' if nearest.window == 'day' else 'resets'} "
                f"{reset_phrase(nearest, now)}.")
    return {"windows": [s.as_dict(now) for s in rows], "note": note,
            "blocked": message(over, now) if over else None, "exempt": False}


# ---------------------------------------------------------------------------
# Hits: the record of a quota stopping someone
# ---------------------------------------------------------------------------

def record_hit(db: DBSession, student_id: int, status: QuotaStatus,
               now: Optional[datetime.datetime] = None) -> bool:
    """One zero-cost ledger row per student and window per period (a
    student who keeps pressing Send is one hit, not fifty). True when written.
    The row is stamped with ``now`` (the clock the status was read at), so a
    hit lands in its own period, not the process clock's."""
    from tutor.logging_config import activity

    already = (
        db.query(TurnEvent.id)
        .filter(TurnEvent.student_id == student_id, TurnEvent.kind == HIT_KIND, TurnEvent.ts >= status.start)
        .all()
    )
    for (row_id,) in already:
        row = db.get(TurnEvent, row_id)
        try:
            if row is not None and (json.loads(row.verdicts_json or "{}") or {}).get("window") == status.window:
                return False
        except ValueError:
            continue
    from tutor.agent import state

    state.record_turn_event(
        db, student_id=student_id, kind=HIT_KIND, cost_usd=0.0, ts=now or utcnow(),
        verdicts={"window": status.window, "limit_usd": round(status.limit_usd, 2),
                  "spent_usd": round(status.spent_usd, 4),
                  "resets_at": status.resets_at.isoformat(sep=" ", timespec="seconds")},
    )
    activity("quota_hit", level="minimal", student_id=student_id, window=status.window,
             limit_usd=round(status.limit_usd, 2), spent_usd=round(status.spent_usd, 4))
    return True


def record_cap_hit(db: DBSession, student_id: int, window: str, *, limit: float,
                   start: datetime.datetime, reset: datetime.datetime, dollars: bool = False,
                   now: Optional[datetime.datetime] = None) -> bool:
    """A class-wide cap (``turns-day`` for DAILY_EXCHANGE_LIMIT, ``class-month``
    for MONTHLY_COST_CAP_USD) stopping a student: the same zero-cost row as a
    quota hit, once per student, window and period, so the Today panel, the
    Supervision tab and the roll-up see it (review step 0.8, F44)."""
    from tutor.logging_config import activity

    already = (
        db.query(TurnEvent.id)
        .filter(TurnEvent.student_id == student_id, TurnEvent.kind == HIT_KIND, TurnEvent.ts >= start)
        .all()
    )
    for (row_id,) in already:
        row = db.get(TurnEvent, row_id)
        try:
            if row is not None and (json.loads(row.verdicts_json or "{}") or {}).get("window") == window:
                return False
        except ValueError:
            continue
    from tutor.agent import state

    verdicts: dict[str, Any] = {"window": window, "resets_at": reset.isoformat(sep=" ", timespec="seconds")}
    if dollars:
        verdicts["limit_usd"] = round(limit, 2)
    else:
        verdicts["limit"] = int(limit)
    state.record_turn_event(db, student_id=student_id, kind=HIT_KIND, cost_usd=0.0, ts=now or utcnow(),
                            verdicts=verdicts)
    activity("quota_hit", level="minimal", student_id=student_id, window=window, limit=limit)
    return True


@dataclass
class Hit:
    student_id: int
    email: str
    name: str
    ts: datetime.datetime
    window: str
    limit_usd: float
    spent_usd: float


def hits(db: DBSession, since: datetime.datetime, until: Optional[datetime.datetime] = None) -> list[Hit]:
    """Quota hits in a window, newest first, with who."""
    q = (
        db.query(TurnEvent, Student.email, Student.name)
        .join(Student, Student.id == TurnEvent.student_id)
        .filter(TurnEvent.kind == HIT_KIND, TurnEvent.ts >= since)
    )
    if until is not None:
        q = q.filter(TurnEvent.ts < until)
    out = []
    for te, email, name in q.order_by(TurnEvent.ts.desc()).all():
        try:
            detail = json.loads(te.verdicts_json or "{}") or {}
        except ValueError:
            detail = {}
        out.append(Hit(student_id=te.student_id, email=email, name=name or "", ts=te.ts,
                       window=str(detail.get("window") or "?"), limit_usd=float(detail.get("limit_usd") or 0),
                       spent_usd=float(detail.get("spent_usd") or 0)))
    return out


@dataclass
class SpendRow:
    student_id: int
    email: str
    name: str
    day_usd: float
    week_usd: float
    month_usd: float
    day_pct: Optional[int]
    week_pct: Optional[int]
    month_pct: Optional[int]

    @property
    def worst_pct(self) -> int:
        return max((p for p in (self.day_pct, self.week_pct, self.month_pct) if p is not None), default=0)


def spend_table(db: DBSession, now: Optional[datetime.datetime] = None, limit: int = 25) -> list[SpendRow]:
    """Students with any spend this month, highest share of a quota first
    (the Supervision tab's "who is near a quota")."""
    now = now or utcnow()
    quotas = load_quotas(db)
    bounds = {w: window_bounds(w, now)[0] for w in WINDOWS}
    by_window = {w: spent_by_student(db, start) for w, start in bounds.items()}
    ids = set(by_window["month"])
    if not ids:
        return []
    people = {sid: (email, name) for sid, email, name in
              db.query(Student.id, Student.email, Student.name).filter(Student.id.in_(ids)).all()}

    def pct(w: str, sid: int) -> Optional[int]:
        return int(100 * by_window[w].get(sid, 0.0) / quotas[w]) if quotas[w] > 0 else None

    rows: list[SpendRow] = []
    for sid in ids:
        email, name = people.get(sid, ("?", ""))
        rows.append(SpendRow(student_id=sid, email=email, name=name or "",
                             day_usd=by_window["day"].get(sid, 0.0), week_usd=by_window["week"].get(sid, 0.0),
                             month_usd=by_window["month"].get(sid, 0.0),
                             day_pct=pct("day", sid), week_pct=pct("week", sid), month_pct=pct("month", sid)))
    rows.sort(key=lambda r: (-r.worst_pct, -r.month_usd, r.email))
    return rows[:limit]


def describe(quotas: dict[str, float]) -> str:
    parts = []
    for w in WINDOWS:
        parts.append(f"{w}: " + (f"${quotas[w]:.2f}" if quotas[w] > 0 else "off"))
    return ", ".join(parts)


def summarize_hits(rows: Sequence[Hit]) -> list[dict[str, Any]]:
    """One line per student: the windows hit and how often, newest first."""
    by: dict[int, dict[str, Any]] = {}
    for h in rows:
        entry = by.setdefault(h.student_id, {"email": h.email, "name": h.name, "hits": 0,
                                             "windows": {}, "last": h.ts, "spent_usd": 0.0})
        entry["hits"] += 1
        entry["windows"][h.window] = entry["windows"].get(h.window, 0) + 1
        entry["last"] = max(entry["last"], h.ts)
        entry["spent_usd"] = max(entry["spent_usd"], h.spent_usd)
    return sorted(by.values(), key=lambda e: (-e["hits"], e["email"]))
