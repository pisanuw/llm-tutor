"""The vocabulary of browser activity events, and the cleaner for a batch.

Every page a signed-in student opens runs ``track.js`` (and every Reveal
deck ``deck-track.js``); they queue what the student does and post it in
batches to ``POST /api/events`` (tutor/routers/api.py), which cleans each
batch here and writes it through ``state.record_activity_events`` into the
``activity_events`` table.

The vocabulary is the contract between the browser and the analysis: an
event whose name is not in :data:`EVENTS` is dropped, a field the event does
not declare is dropped, and every kept value is clipped and typed. Nothing
free-form reaches the table: ``label`` is the static text of a control, and
``detail_json`` holds only the keys the event declares, so the row carries
no student text and the one-year anonymization has only ``label`` to blank.

A batch is ``{"sent_at": <ms since the epoch>, "events": [ {...}, ... ]}``.
Each event carries the common columns below plus its own detail keys. The
server stamps ``ts`` from its own clock, shifted by the browser's offset
(``sent_at`` against the arrival time), so ``ts`` compares with every other
table's timestamps while ``client_ts`` keeps the browser's reading.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import threading
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_BATCH = 100               # events per POST; a larger batch is a client bug and is dropped whole
MAX_BODY_BYTES = 64 * 1024    # the request body
RATE_PER_MINUTE = 600         # rows per student per minute; over it, batches are dropped
PAST_SKEW = datetime.timedelta(days=1)       # a corrected ts older than this is distrusted
FUTURE_SKEW = datetime.timedelta(seconds=60)  # ... or further ahead than this

# ---------------------------------------------------------------------------
# The vocabulary: event name -> the detail keys it may carry, each with a
# spec: ("int", lo, hi), ("str", max_len), ("enum", {...}) or ("bool",).
# The common columns (section below) are accepted on every event.
# ---------------------------------------------------------------------------


def _int(lo: int, hi: int) -> tuple:
    return ("int", lo, hi)


def _str(n: int) -> tuple:
    return ("str", n)


def _enum(*values: str) -> tuple:
    return ("enum", frozenset(values))


_BOOL: tuple = ("bool",)
_POSITION = {"position_s": _int(0, 86_400)}
_GOAL = {"goal_id": _str(64)}
_SLUG = {"slug": _str(80)}

EVENTS: dict[str, dict[str, tuple]] = {
    # Pages (track.js). value_ms: active time on the page view.
    "page_view": {"ref": _str(200), "vw": _int(0, 20_000), "vh": _int(0, 20_000)},
    "page_hide": {},
    "page_show": {},
    "page_heartbeat": {},     # value_ms: active time since the last beat
    "page_leave": {},
    # href: the same-origin path a clicked link opens (review step 0.2, F6),
    # so a dead link joins the 404 it caused.
    "click": {"href": _str(200)},
    # A script failure in the page: the error's name, the script's path and
    # the line, never the message (it could quote the student). At most a
    # few per page view; track.js caps them.
    "client_error": {"error": _str(40), "source": _str(80), "line": _int(0, 10_000_000)},
    # Chat (chat.js). value_ms: ms since the question was sent.
    "question_sent": {"chars": _int(0, 1_000_000),
                      "kind": _enum("typed", "starter", "teachme", "code", "photo", "not_now")},
    "reply_started": {},
    "reply_first_token": {},
    "reply_done": {"chars": _int(0, 1_000_000),
                   "outcome": _enum("done", "error", "busy", "resumed", "expired", "limited")},
    "slide_cited": {},
    "slide_chip_click": {},
    # The slides panel found no deck for course + lecture_num (chat.js
    # relayoutDeck): the sentence the student read, now a row too.
    "deck_missing": {},
    "lecture_select": {"from": _int(0, 99), "to": _int(0, 99)},
    "course_switch": {"from": _str(8), "to": _str(8)},
    "panel_toggle": {"to": _enum("chat", "slides")},
    "tts_play": _POSITION,       # value_ms: played since the last tts event
    "tts_pause": _POSITION,
    "tts_ended": _POSITION,
    "feedback": {"rating": _int(-1, 1)},
    "code_submit": {"chars": _int(0, 1_000_000), "predicted": _int(0, 4)},
    "teachme_start": {},
    "starter_click": {"index": _int(0, 99)},
    # Decks (deck-track.js). value_ms: dwell on the previous slide, active
    # time since the last beat, or narration played since the last event.
    "deck_open": {"slides_total": _int(0, 999), "context": _enum("chat", "lecture", "tab"),
                  "has_audio": _BOOL},
    "slide_view": {"prev_slide": _int(0, 999), "how": _enum("first", "next", "prev", "jump", "auto")},
    "deck_heartbeat": {"narration_playing": _BOOL},
    "narration_play": _POSITION,
    "narration_pause": _POSITION,
    "narration_seek": _POSITION,
    "narration_ended": _POSITION,
    "deck_close": {"active_ms": _int(0, 86_400_000), "slides_seen": _int(0, 999)},
    # Other pages.
    "lecture_open": {},
    "goal_click": {"goal_id": _str(64), "modality": _str(32)},
    "review_start": _GOAL,
    "review_answer": _GOAL,
    "book_narration_play": _SLUG,
    "book_narration_ended": _SLUG,
}

# The columns every event may set, besides name/seq/client_ts and the ids.
_COLUMN_SPECS: dict[str, tuple] = {
    "course": _str(8),
    "lecture_num": _int(0, 99),
    "slide_num": _int(0, 999),
    "message_id": _int(0, 2_147_483_647),
    "path": _str(200),
    "target": _str(80),
    "label": _str(80),
    "value_ms": _int(0, 86_400_000),
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_TURN_RE = re.compile(r"^[0-9a-f]{1,32}$")


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def _coerce(spec: tuple, value: Any) -> Any:
    """``value`` as the spec allows, or None when it does not fit."""
    kind = spec[0]
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value):  # NaN, or 1e400 parsed as infinity (int() would raise)
            return None
        number = int(value)
        return number if spec[1] <= number <= spec[2] else None
    if kind == "str":
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text[: spec[1]] if text else None
    if kind == "enum":
        return value if isinstance(value, str) and value in spec[1] else None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    return None


def _from_epoch_ms(value: Any) -> Optional[datetime.datetime]:
    """A browser timestamp (ms since the epoch) as a naive UTC datetime,
    like every other timestamp the models store (tutor.utctime.utcnow)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.datetime.fromtimestamp(value / 1000.0, tz=datetime.timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def clean_event(raw: Any, now: datetime.datetime, offset: Optional[datetime.timedelta]) -> Optional[dict]:
    """One event as a row dict for ``activity_events``, or None to drop it.

    ``offset`` is the browser's clock offset for the batch (server arrival
    minus ``sent_at``); the row's ``ts`` is ``client_ts + offset`` when that
    lands within a day behind or a minute ahead of now, else ``now``.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or name not in EVENTS:
        return None
    visit_id = raw.get("visit_id")
    page_view_id = raw.get("page_view_id")
    if not (isinstance(visit_id, str) and _ID_RE.match(visit_id)):
        return None
    if not (isinstance(page_view_id, str) and _ID_RE.match(page_view_id)):
        return None

    client_ts = _from_epoch_ms(raw.get("client_ts"))
    ts = now
    if client_ts is not None and offset is not None:
        candidate = client_ts + offset
        if now - PAST_SKEW <= candidate <= now + FUTURE_SKEW:
            ts = candidate

    row: dict[str, Any] = {
        "name": name,
        "visit_id": visit_id,
        "page_view_id": page_view_id,
        "seq": _coerce(_int(0, 1_000_000), raw.get("seq")) or 0,
        "ts": ts,
        "client_ts": client_ts,
    }
    for column, spec in _COLUMN_SPECS.items():
        row[column] = _coerce(spec, raw.get(column)) if column in raw else None
    turn_id = raw.get("turn_id")
    row["turn_id"] = turn_id if isinstance(turn_id, str) and _TURN_RE.match(turn_id) else None

    detail: dict[str, Any] = {}
    for key, spec in EVENTS[name].items():
        if key in raw:
            value = _coerce(spec, raw[key])
            if value is not None:
                detail[key] = value
    row["detail_json"] = json.dumps(detail, separators=(",", ":"), sort_keys=True) if detail else None
    return row


def clean_batch(payload: Any, now: datetime.datetime) -> list[dict]:
    """The rows of one posted batch, in order; an invalid batch is empty.

    A batch over :data:`MAX_BATCH` is dropped whole (the browser never sends
    one; a larger one is a bug worth seeing as missing data, not trimming).
    """
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    if not isinstance(events, list) or not events or len(events) > MAX_BATCH:
        return []
    sent_at = _from_epoch_ms(payload.get("sent_at"))
    offset = (now - sent_at) if sent_at is not None else None
    rows = []
    for raw in events:
        row = clean_event(raw, now, offset)
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# The per-student cap (process memory; the single-instance note in CLAUDE.md)
# ---------------------------------------------------------------------------

_WINDOW_SECONDS = 60.0
_counts: dict[int, tuple[float, int]] = {}
_lock = threading.Lock()


def allow(student_id: int, n: int, now: Optional[float] = None) -> bool:
    """Whether ``n`` more rows for the student fit under RATE_PER_MINUTE in
    the current minute. A refused batch is not counted, so a page that
    floods keeps being refused only while it floods."""
    now = time.monotonic() if now is None else now
    with _lock:
        start, count = _counts.get(student_id, (now, 0))
        if now - start >= _WINDOW_SECONDS:
            start, count = now, 0
        if count + n > RATE_PER_MINUTE:
            _counts[student_id] = (start, count)
            return False
        _counts[student_id] = (start, count + n)
        if len(_counts) > 10_000:
            stale = [sid for sid, (s, _c) in _counts.items() if now - s >= _WINDOW_SECONDS]
            for sid in stale:
                del _counts[sid]
        return True


def _reset_for_tests() -> None:
    with _lock:
        _counts.clear()


# ---------------------------------------------------------------------------
# The stdout mirror (the ``tutor.activity`` stream, verbose tier)
# ---------------------------------------------------------------------------

_MIRRORED = ("name", "visit_id", "page_view_id", "seq", "course", "lecture_num", "slide_num",
             "turn_id", "message_id", "path", "target", "label", "value_ms")


def mirror(student_id: int, rows: list[dict]) -> None:
    """One ``ui_event`` line per row on the activity stream, at the verbose
    tier, so a live tail still shows clicks while the table is the record."""
    from tutor.logging_config import activity

    for row in rows:
        fields = {k: row.get(k) for k in _MIRRORED}
        if row.get("detail_json"):
            fields["detail"] = json.loads(row["detail_json"])
        activity("ui_event", level="verbose", student_id=student_id, **fields)
