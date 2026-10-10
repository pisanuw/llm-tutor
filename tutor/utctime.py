"""Centralized UTC clock for the app.

`datetime.utcnow()` is deprecated as of Python 3.12 and returns a *naive*
datetime (no tzinfo). The codebase had ~25 `datetime.datetime.utcnow()` call
sites backing security-relevant expiry comparisons (session, magic-link, JWT)
plus SQLAlchemy column defaults.

This module replaces every one of them with a single helper so the deprecated
API is gone and the clock choice lives in one place.

Why naive UTC (not aware) for now
---------------------------------
The ORM columns are declared as plain ``DateTime`` (no ``timezone=True``), so
the database stores and returns *naive* datetimes, and the existing rows in the
live DB are naive. ``utcnow()`` here returns ``datetime.now(timezone.utc)`` with
the tzinfo stripped, i.e. a naive UTC instant — identical wall-clock semantics
to the old ``datetime.utcnow()``, so:

  * it removes the deprecated call and the future-removal break, and
  * it keeps BOTH sides of every expiry comparison naive, so no
    "can't compare offset-naive and offset-aware" crash is introduced and the
    stored values still compare correctly (no live-DB migration required).

Moving to fully timezone-aware datetimes means flipping the ORM columns to
``DateTime(timezone=True)`` and re-coercing existing rows; that is a schema +
data migration (Alembic) and is intentionally out of scope for this change.
When that happens, switch this one function to return the aware value and the
whole app follows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (tzinfo stripped).

    Drop-in replacement for the deprecated ``datetime.datetime.utcnow()`` with
    identical (naive-UTC) semantics. See module docstring for the rationale.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Report windows in the class's time zone (plan step 4.4, finding B23)
# ---------------------------------------------------------------------------
# The reports run in UTC and used to label a window by its UTC date, so the
# daily email that ends at 1:45 am local time named tomorrow. They now name
# both endpoints in the reader's time zone: ``tutor.timezone`` in
# courses.yaml. The ``tzdata`` package backs zoneinfo where the system has no
# time zone database (a slim image).


def _report_tz() -> str:
    from tutor.courses import IDENTITY

    return IDENTITY.timezone


REPORT_TZ = _report_tz()


def _local_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(REPORT_TZ)


def to_local(when: datetime) -> datetime:
    """A naive UTC instant (as stored) in the class's time zone."""
    return when.replace(tzinfo=timezone.utc).astimezone(_local_tz())


def local_day(day: str) -> tuple[datetime, datetime]:
    """``YYYY-MM-DD`` as a local calendar day: its naive-UTC ``[since,
    until)``, 23 to 25 hours long across a daylight-saving change."""
    from datetime import timedelta

    local = datetime.strptime(day, "%Y-%m-%d")
    start = local.replace(tzinfo=_local_tz())
    end = (local + timedelta(days=1)).replace(tzinfo=_local_tz())   # the next local midnight
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def report_window(day: Optional[str], hours: int) -> tuple[datetime, datetime]:
    """A report's naive-UTC ``[since, until)``: the local calendar ``day``
    (``YYYY-MM-DD``) when given, else the last ``hours`` up to now. The three
    report scripts share it (plan step 5.7, finding B27)."""
    from datetime import timedelta

    if day:
        return local_day(day)
    until = utcnow()
    return until - timedelta(hours=hours), until


def window_label(since: datetime, until: datetime, times: bool = True) -> str:
    """Both endpoints of a naive-UTC window in local time, e.g.
    "Thu Sep 24, 1:45 am to Fri Sep 25, 1:45 am PDT", or with ``times`` off
    "Sep 19 to Sep 26, 2026 (PDT)"."""
    a, b = to_local(since), to_local(until)
    if not times:
        return f"{a:%b %d} to {b:%b %d, %Y} ({b:%Z})"

    def clock(t: datetime) -> str:
        return t.strftime("%I:%M %p").lstrip("0").lower()

    return f"{a:%a %b %d}, {clock(a)} to {b:%a %b %d}, {clock(b)} {b:%Z}"
