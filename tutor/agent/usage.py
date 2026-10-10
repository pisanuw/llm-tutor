"""Usage accounting and spend caps.

Cost is estimated from a small per-model pricing table and the token counts
already recorded on TurnEvent rows by state.py. Caps (0 disables each):

  - per-student daily exchange cap   (env DAILY_EXCHANGE_LIMIT; chat turns)
  - per-student daily review cap     (env DAILY_REVIEW_LIMIT, default 60;
                                      graded spaced-review answers, decision 6)
  - per-student dollar quotas        (tutor/quotas.py: a day, a week, a month,
                                      set on the admin Supervision tab)
  - global month-to-date USD cap     (env MONTHLY_COST_CAP_USD; every ledgered
                                      call, review grading included)

``over_caps`` guards chat turns (inside run_turn); ``over_review_caps`` guards
POST /review/grade. The reads never write, except that a quota stopping a
student is recorded once per period as a zero-cost ``quota`` ledger row.
Days are calendar days in the class's time zone (courses.yaml
``tutor.timezone``; decision 10).
"""

from __future__ import annotations

import datetime
import os
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

# Importing the config module (not calling it) ensures .env is loaded so the
# cap env vars are visible even if usage is the first thing imported.
import tutor.config  # noqa: F401
from tutor.db.models import TurnEvent

# Prices live in the model registry (S21); the API reports input_tokens net
# of cache reads, so the three components are additive.
from tutor.agent.models import pricing as _pricing
from tutor.utctime import utcnow


# Friendly, in-voice limit messages (no em dashes). Each is the start of what
# the student reads; the rest says when the limit lifts (R25: the class caps
# never said).
DAILY_LIMIT_MESSAGE = (
    "That is a solid run for today (you have reached the daily practice limit "
    "with me)."
)
REVIEW_LIMIT_MESSAGE = "That is plenty of review for today."
# Decision 6 default: review grading has its own daily cap rather than eating
# into the chat limit. A deep session is 40 items, so 60 leaves room for one
# deep and one standard session a day.
DEFAULT_DAILY_REVIEW_LIMIT = 60
MONTHLY_CAP_MESSAGE = "The tutor is taking a short rest to stay within budget for the month."
# The daily turn count shows in the budget line from this share of the limit.
TURNS_NOTE_FROM = 0.8


def _day_reset(now: Optional[datetime.datetime] = None) -> tuple[datetime.datetime, datetime.datetime]:
    from tutor import quotas

    return quotas.window_bounds("day", now)


def _month_bounds(now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """The class cap's month: the UTC calendar month, as month_cost_usd sums it."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return start, start.replace(year=year, month=month)


def daily_limit_message(now: Optional[datetime.datetime] = None) -> str:
    from tutor import quotas

    now = now or utcnow()
    when = quotas.when_phrase("day", _day_reset(now)[1], now)
    return (f"{DAILY_LIMIT_MESSAGE} Step away and let it settle: more questions open {when}. "
            "The slides and the book are still open. :-)")


def review_limit_message(now: Optional[datetime.datetime] = None) -> str:
    from tutor import quotas

    now = now or utcnow()
    when = quotas.when_phrase("day", _day_reset(now)[1], now)
    return (f"{REVIEW_LIMIT_MESSAGE} Spaced practice works best in short sessions: the next items "
            f"open {when}. :-)")


def monthly_cap_message(now: Optional[datetime.datetime] = None) -> str:
    from tutor import quotas

    now = now or utcnow()
    when = quotas.when_phrase("month", _month_bounds(now)[1], now)
    return f"{MONTHLY_CAP_MESSAGE} It is back {when}. The slides and the book are still here. :-)"


def estimate_cost_usd(
    model: str,
    in_tokens: int,
    out_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """Estimate the USD cost of one model call from its token counts.

    The API reports input tokens net of cache reads and cache writes, so the
    four parts add up (plan step 2.1: cache writes used to be left out, so a
    cold-cache turn cost about four times what the ledger said).
    """
    from tutor.agent.models import cache_write_price

    p_in, p_out, p_cache = _pricing(model)
    return (
        (in_tokens or 0) * p_in
        + (out_tokens or 0) * p_out
        + (cache_read_tokens or 0) * p_cache
        + (cache_write_tokens or 0) * cache_write_price(model)
        # Of the write, the part that went to the 1-hour cache costs 2x input
        # instead of the 5-minute 1.25x (B1).
        + (cache_write_1h_tokens or 0) * (2 * p_in - cache_write_price(model))
    ) / 1_000_000


def cost_fields(model: str, counts: Optional[dict]) -> dict:
    """TurnEvent keyword arguments for one model call: the model, its token
    counts (from llm_response.usage_counts), and the estimated cost."""
    c = counts or {}
    in_tok = int(c.get("in_tokens", 0) or 0)
    out_tok = int(c.get("out_tokens", 0) or 0)
    read_tok = int(c.get("cache_read_tokens", 0) or 0)
    write_tok = int(c.get("cache_write_tokens", 0) or 0)
    write_1h = int(c.get("cache_write_1h_tokens", 0) or 0)
    return {
        "model": model,
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "cache_read_tokens": read_tok,
        "cache_write_tokens": write_tok,
        "cost_usd": estimate_cost_usd(model, in_tok, out_tok, read_tok, write_tok, write_1h),
    }


def daily_exchange_count(db: DBSession, student_id: int, kind: str = "chat") -> int:
    """Count this student's TurnEvents of ``kind`` since local midnight
    (the day the quotas use too, so the two caps reset together)."""
    from tutor import quotas

    start, _reset = quotas.window_bounds("day")
    return int(
        db.query(func.count())
        .select_from(TurnEvent)
        .filter(
            TurnEvent.student_id == student_id,
            TurnEvent.kind == kind,
            TurnEvent.ts >= start,
        )
        .scalar()
        or 0
    )


def month_cost_usd(db: DBSession) -> float:
    """Sum recorded cost across all students since the 1st of this UTC month."""
    now = utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (
        db.query(func.coalesce(func.sum(TurnEvent.cost_usd), 0.0))
        .filter(TurnEvent.ts >= start)
        .scalar()
    )
    return float(total or 0.0)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw) if raw.strip() else default
    except (TypeError, ValueError):
        return default


def _limits(db: DBSession, student_id: int, *, count: Optional[str], record: bool,
            now: Optional[datetime.datetime] = None) -> list[tuple[datetime.datetime, str]]:
    """Every limit stopping the student now, as (when it lifts, what to say).

    ``count`` is the per-day count cap that applies: "chat" turns
    (DAILY_EXCHANGE_LIMIT), "review" answers (DAILY_REVIEW_LIMIT), or None
    for a call that is neither (a photo). Then the student's dollar quotas
    and the class's monthly budget. With ``record`` each stop is a ledger
    row, once per student, window and period: a hit used to leave nothing
    (review step 0.8, F44), and a review stop still did (R25).
    """
    from tutor import quotas

    # ``now`` passes through as given: each module reads its own clock
    # otherwise (a test pins one).
    found: list[tuple[datetime.datetime, str]] = []
    if count is not None:
        if count == "review":
            limit, window, say = _int_env("DAILY_REVIEW_LIMIT", DEFAULT_DAILY_REVIEW_LIMIT), "reviews-day", review_limit_message
        else:
            limit, window, say = _int_env("DAILY_EXCHANGE_LIMIT", 0), "turns-day", daily_limit_message
        if limit > 0 and daily_exchange_count(db, student_id, kind=count) >= limit:
            start, reset = quotas.window_bounds("day", now)
            if record:
                quotas.record_cap_hit(db, student_id, window, limit=float(limit), start=start, reset=reset)
            found.append((reset, say(now)))
    status = quotas.blocking(db, student_id, now)
    if status is not None:
        if record:
            quotas.record_hit(db, student_id, status)
        found.append((status.resets_at, quotas.message(status, now)))
    monthly_cap = _float_env("MONTHLY_COST_CAP_USD", 0.0)
    if monthly_cap > 0 and month_cost_usd(db) >= monthly_cap:
        start, reset = _month_bounds(now or utcnow())
        if record:
            quotas.record_cap_hit(db, student_id, "class-month", limit=monthly_cap, start=start, reset=reset,
                                  dollars=True)
        found.append((reset, monthly_cap_message(now)))
    return found


def _latest(found: list[tuple[datetime.datetime, str]]) -> Optional[str]:
    """The message of the limit that lifts last: the one that says when the
    student can really ask again. Over the daily turns and the week's
    quota, "midnight" was wrong (the Stage 3 review)."""
    return max(found, key=lambda f: f[0])[1] if found else None


def over_caps(db: DBSession, student_id: int) -> Optional[str]:
    """Return a friendly limit message if a cap stops a chat turn, else None.

    Caps are disabled when their env var is unset or 0.
    """
    return _latest(_limits(db, student_id, count="chat", record=True))


def over_review_caps(db: DBSession, student_id: int) -> Optional[str]:
    """The caps for one graded review answer (plan step 1.6, decision 6):
    the per-student daily review cap, the dollar quotas and the shared
    monthly spend cap."""
    return _latest(_limits(db, student_id, count="review", record=True))


def over_spend_caps(db: DBSession, student_id: int) -> Optional[str]:
    """The caps on a model call that is not a turn (a photo read): the
    student's dollar quotas and the class's monthly budget, not the daily
    turn count (R26: a photo checked only the quotas)."""
    return _latest(_limits(db, student_id, count=None, record=True))


def over_quota(db: DBSession, student_id: int) -> Optional[str]:
    """The student's own dollar quotas (tutor/quotas.py): the message naming
    when the quota comes back, or None. A hit is recorded once per period."""
    from tutor import quotas

    status = quotas.blocking(db, student_id)
    if status is None:
        return None
    quotas.record_hit(db, student_id, status)
    return quotas.message(status)


def cap_indicator(db: DBSession, student_id: int, now: Optional[datetime.datetime] = None) -> dict:
    """What the budget line says, read only: the message of the limit that
    lifts last when any stops a chat turn (the same one the turn's notice
    names), else a note once the daily turns pass TURNS_NOTE_FROM of the
    limit (R25: the line spoke only of the dollar quotas)."""
    from tutor import quotas

    now = now or utcnow()
    blocked = _latest(_limits(db, student_id, count="chat", record=False, now=now))
    if blocked:
        return {"blocked": blocked, "note": None}
    daily_limit = _int_env("DAILY_EXCHANGE_LIMIT", 0)
    used = daily_exchange_count(db, student_id) if daily_limit > 0 else 0
    if daily_limit > 0 and used >= TURNS_NOTE_FROM * daily_limit:
        left = daily_limit - used
        when = quotas.when_phrase("day", _day_reset(now)[1], now)
        return {"blocked": None,
                "note": (f"You have {left} question{'s' if left != 1 else ''} left for today "
                         f"(of {daily_limit}). More open {when}.")}
    return {"blocked": None, "note": None}
