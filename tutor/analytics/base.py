"""What every analysis module shares: the local store, the window, the
thresholds, and the signals a student left (analytics plan, section 3.8).

A *signal* is one thing that places a student at a moment: an activity
event, a question or a reply, a turn, a path choice, a graded probe. The
modules sessionize and measure over the sorted signals of one student, so
the definitions below are the only place a timestamp is parsed.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


# ---------------------------------------------------------------------------
# Thresholds (every report prints the ones it used)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Thresholds:
    idle_gap_min: float = 30.0      # a longer silence starts a new visit
    heartbeat_s: int = 60           # how long one on-screen signal keeps the student "active"
    grace_s: int = 15               # ... plus this
    follow_up_s: float = 60.0       # think time under this is a follow-up
    reading_max_min: float = 10.0   # think time up to this is reading; longer is away
    read_slide_s: float = 3.0       # median dwell at or over this is reading, under it flipping
    coverage_pct: float = 50.0      # slides seen over slides in the deck, to count a lecture covered
    listened_pct: float = 80.0      # narration played over the lecture's narration, to count it listened
    left_after_min: float = 10.0    # no on-screen signal this long after a reply: the student left

    def active_extent(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=self.heartbeat_s + self.grace_s)

    def describe(self) -> str:
        return (f"visit idle gap {self.idle_gap_min:g} min; follow-up under {self.follow_up_s:g} s; "
                f"reading up to {self.reading_max_min:g} min; a slide read at {self.read_slide_s:g} s; "
                f"a lecture covered at {self.coverage_pct:g}% of its slides, listened at "
                f"{self.listened_pct:g}% of its narration; left after {self.left_after_min:g} min")

    def with_overrides(self, **values: Any) -> "Thresholds":
        return replace(self, **{k: v for k, v in values.items() if v is not None})


@dataclass(frozen=True)
class Window:
    since: Optional[datetime.datetime] = None
    until: Optional[datetime.datetime] = None

    def contains(self, ts: datetime.datetime) -> bool:
        return (self.since is None or ts >= self.since) and (self.until is None or ts < self.until)

    def sql(self, column: str) -> tuple[str, list[str]]:
        clauses, params = [], []
        if self.since is not None:
            clauses.append(f"{column} >= ?")
            params.append(fmt_ts(self.since))
        if self.until is not None:
            clauses.append(f"{column} < ?")
            params.append(fmt_ts(self.until))
        return (" AND ".join(clauses) if clauses else "1=1"), params

    def label(self) -> str:
        a = self.since.date().isoformat() if self.since else "the start"
        b = self.until.date().isoformat() if self.until else "now"
        return f"{a} to {b}"


def window_days(days: Optional[int], since: Optional[str] = None, until: Optional[str] = None,
                now: Optional[datetime.datetime] = None) -> Window:
    """``--days N`` or ``--since``/``--until`` (YYYY-MM-DD) as a Window."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if since or until:
        return Window(parse_ts(since) if since else None, parse_ts(until) if until else None)
    if days:
        return Window(now - datetime.timedelta(days=days), None)
    return Window()


# ---------------------------------------------------------------------------
# Timestamps and small statistics
# ---------------------------------------------------------------------------

def parse_ts(value: Any) -> Optional[datetime.datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def fmt_ts(ts: datetime.datetime) -> str:
    return ts.isoformat(sep=" ", timespec="seconds")


def seconds(a: datetime.datetime, b: datetime.datetime) -> float:
    return (b - a).total_seconds()


def median(values: Sequence[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def quartiles(values: Sequence[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(q1, median, q3), or Nones for an empty list."""
    if not values:
        return None, None, None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0], ordered[0], ordered[0]
    q1, q2, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    return q1, q2, q3


def percentile_rank(value: float, population: Sequence[float]) -> Optional[float]:
    """The share of the population at or below ``value`` (0 to 100)."""
    if not population:
        return None
    return 100.0 * sum(1 for v in population if v <= value) / len(population)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

# Activity events that place the student on screen at that moment.
ON_SCREEN = frozenset({
    "page_view", "page_show", "page_heartbeat", "click", "question_sent", "reply_started",
    "reply_first_token", "reply_done", "slide_cited", "slide_chip_click", "lecture_select",
    "course_switch", "panel_toggle", "tts_play", "tts_pause", "tts_ended", "feedback", "code_submit",
    "teachme_start", "starter_click", "deck_open", "slide_view", "deck_heartbeat", "narration_play",
    "narration_pause", "narration_seek", "narration_ended", "lecture_open", "goal_click",
    "review_start", "review_answer", "book_narration_play", "book_narration_ended",
})
# ... and those that end an on-screen span.
OFF_SCREEN = frozenset({"page_hide", "page_leave", "deck_close"})
DECK_SIGNALS = frozenset({"deck_open", "slide_view", "deck_heartbeat", "narration_play", "narration_pause",
                          "narration_seek", "narration_ended"})


@dataclass
class Signal:
    ts: datetime.datetime
    source: str                 # event | question | reply | turn | path | graded
    name: str                   # the event name, or the source again
    student_id: int
    course: Optional[str] = None
    lecture_num: Optional[int] = None
    slide_num: Optional[int] = None
    turn_id: Optional[str] = None
    message_id: Optional[int] = None
    value_ms: Optional[int] = None
    path: Optional[str] = None
    detail: dict = field(default_factory=dict)
    row_id: Optional[int] = None
    visit_id: Optional[str] = None
    page_view_id: Optional[str] = None
    seq: Optional[int] = None

    @property
    def on_screen(self) -> bool:
        return self.source == "question" or (self.source == "event" and self.name in ON_SCREEN)

    @property
    def off_screen(self) -> bool:
        return self.source == "event" and self.name in OFF_SCREEN

    @property
    def is_deck(self) -> bool:
        return self.source == "event" and self.name in DECK_SIGNALS


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class Store:
    """A read-only view of the local SQLite file (tutor/analytics/store.py)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"no store at {self.path}; run `python -m tutor.analytics export` first")
        self.con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self._lectures: Optional[dict[tuple[str, int], dict]] = None

    def close(self) -> None:
        self.con.close()

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, list(params))]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        row = self.con.execute(sql, list(params)).fetchone()
        return dict(row) if row else None

    # --- students and classes ---------------------------------------------------

    def has_column(self, table: str, column: str) -> bool:
        return any(r["name"] == column for r in self.rows(f"PRAGMA table_info({table})"))

    def student_label(self, student_id: int) -> str:
        row = self.one("SELECT * FROM students WHERE id = ?", (student_id,))
        if row is None:
            return f"S{student_id}"
        if row.get("email") and not row.get("anonymized_at"):
            return str(row["email"])
        return str(row.get("pseudonym") or f"S{student_id}")

    def resolve_student(self, arg: str) -> int:
        """An id, a pseudonym (S12), or an address (in a store with emails)."""
        text = str(arg).strip()
        if text.isdigit():
            sid = int(text)
        elif text.upper().startswith("S") and text[1:].isdigit():
            sid = int(text[1:])
        elif "@" in text and self.has_column("students", "email"):
            row = self.one("SELECT id FROM students WHERE lower(email) = lower(?)", (text,))
            if row is None:
                raise LookupError(f"no student with address {text}")
            sid = int(row["id"])
        else:
            raise LookupError(f"name a student by id, pseudonym (S12), or address: {text!r}")
        if self.one("SELECT id FROM students WHERE id = ?", (sid,)) is None:
            raise LookupError(f"no student {sid} in the store")
        return sid

    def class_label(self, class_id: int) -> str:
        row = self.one("SELECT name, term, course FROM classes WHERE id = ?", (class_id,))
        if row is None:
            raise LookupError(f"no class {class_id} in the store")
        return f"{row['name']} ({row['term']})" if row["term"] else str(row["name"])

    def class_course(self, class_id: int) -> str:
        row = self.one("SELECT course FROM classes WHERE id = ?", (class_id,))
        if row is None:
            raise LookupError(f"no class {class_id} in the store")
        return str(row["course"])

    def roster(self, class_id: int) -> list[int]:
        return [int(r["student_id"]) for r in self.rows(
            "SELECT student_id FROM class_enrollments WHERE class_id = ? ORDER BY student_id", (class_id,))]

    def classes_of(self, student_id: int) -> list[int]:
        return [int(r["class_id"]) for r in self.rows(
            "SELECT class_id FROM class_enrollments WHERE student_id = ? ORDER BY class_id", (student_id,))]

    def all_students(self) -> list[int]:
        return [int(r["id"]) for r in self.rows("SELECT id FROM students WHERE is_instructor = 0 ORDER BY id")]

    # --- the material -----------------------------------------------------------

    def lectures(self) -> dict[tuple[str, int], dict]:
        """``{(course, lecture_num): {title, slides_total, narration_total_s, narration_s: {slide: s}}}``."""
        if self._lectures is None:
            out: dict[tuple[str, int], dict] = {}
            for r in self.rows("SELECT * FROM lectures"):
                per_slide = json.loads(r["narration_s_json"]) if r.get("narration_s_json") else {}
                out[(str(r["course"]), int(r["lecture_num"]))] = {
                    "title": r.get("title") or "", "slides_total": int(r.get("slides_total") or 0),
                    "narration_total_s": float(r["narration_total_s"]) if r.get("narration_total_s") else None,
                    "narration_s": {int(k): float(v) for k, v in per_slide.items()},
                }
            self._lectures = out
        return self._lectures

    def goal_titles(self) -> dict[str, str]:
        return {str(r["id"]): str(r["title"]) for r in self.rows("SELECT id, title FROM learning_goals")}

    def goal_lectures(self) -> dict[str, tuple[str, int]]:
        return {str(r["id"]): (str(r["course"]), int(r["lecture_num"]))
                for r in self.rows("SELECT id, course, lecture_num FROM learning_goals")}

    # --- signals ----------------------------------------------------------------

    def _in(self, ids: Sequence[int]) -> str:
        return "(" + ",".join(str(int(i)) for i in ids) + ")" if ids else "(-1)"

    def events(self, student_ids: Sequence[int], window: Window, names: Optional[Iterable[str]] = None) -> list[Signal]:
        where, params = window.sql("ts")
        sql = f"SELECT * FROM activity_events WHERE student_id IN {self._in(student_ids)} AND {where}"
        if names is not None:
            wanted = list(names)
            sql += " AND name IN (" + ",".join("?" for _ in wanted) + ")"
            params = params + wanted
        sql += " ORDER BY ts, student_id, seq, id"
        out = []
        for r in self.rows(sql, params):
            ts = parse_ts(r["ts"])
            if ts is None:
                continue
            detail = json.loads(r["detail_json"]) if r.get("detail_json") else {}
            out.append(Signal(ts=ts, source="event", name=str(r["name"]), student_id=int(r["student_id"]),
                              course=r.get("course"), lecture_num=r.get("lecture_num"), slide_num=r.get("slide_num"),
                              turn_id=r.get("turn_id"), message_id=r.get("message_id"), value_ms=r.get("value_ms"),
                              path=r.get("path"), detail=detail if isinstance(detail, dict) else {},
                              row_id=int(r["id"]), visit_id=r.get("visit_id"), page_view_id=r.get("page_view_id"),
                              seq=r.get("seq")))
        return out

    def messages(self, student_ids: Sequence[int], window: Window, course: Optional[str] = None) -> list[Signal]:
        where, params = window.sql("created_at")
        sql = f"SELECT * FROM messages WHERE student_id IN {self._in(student_ids)} AND {where}"
        if course:
            sql += " AND course = ?"
            params = params + [course]
        sql += " ORDER BY created_at, id"
        out = []
        for r in self.rows(sql, params):
            ts = parse_ts(r["created_at"])
            if ts is None:
                continue
            source = "question" if r["role"] == "user" else "reply"
            out.append(Signal(ts=ts, source=source, name=source, student_id=int(r["student_id"]),
                              course=r.get("course"), lecture_num=r.get("lecture_num"), slide_num=r.get("slide_num"),
                              message_id=int(r["id"]), row_id=int(r["id"]),
                              detail={"chars": r.get("chars")}))
        return out

    def turns(self, student_ids: Sequence[int], window: Window) -> list[dict]:
        where, params = window.sql("ts")
        rows = self.rows(f"SELECT * FROM turn_events WHERE student_id IN {self._in(student_ids)} AND {where} "
                         "ORDER BY ts, id", params)
        for r in rows:
            r["ts"] = parse_ts(r["ts"])
            r["goal_ids"] = json.loads(r["goal_ids_json"]) if r.get("goal_ids_json") else []
        return [r for r in rows if r["ts"] is not None]

    def other_signals(self, student_ids: Sequence[int], window: Window) -> list[Signal]:
        """Path choices and graded probes: the student was there too."""
        out = []
        where, params = window.sql("ts")
        for r in self.rows(f"SELECT * FROM path_events WHERE student_id IN {self._in(student_ids)} AND {where}", params):
            ts = parse_ts(r["ts"])
            if ts is not None:
                out.append(Signal(ts=ts, source="path", name=str(r.get("modality") or "path"),
                                  student_id=int(r["student_id"]), course=r.get("course"),
                                  detail={"goal_id": r.get("goal_id")}, row_id=int(r["id"])))
        for r in self.rows(f"SELECT * FROM graded_events WHERE student_id IN {self._in(student_ids)} AND {where}", params):
            ts = parse_ts(r["ts"])
            if ts is not None:
                out.append(Signal(ts=ts, source="graded", name=str(r.get("source") or "graded"),
                                  student_id=int(r["student_id"]),
                                  detail={"goal_id": r.get("goal_id"), "success": r.get("success")},
                                  row_id=int(r["id"])))
        return out

    def signals(self, student_ids: Sequence[int], window: Window) -> dict[int, list[Signal]]:
        """Every signal of each student in the window, sorted by time."""
        by: dict[int, list[Signal]] = {int(s): [] for s in student_ids}
        for sig in self.events(student_ids, window) + self.messages(student_ids, window) + self.other_signals(student_ids, window):
            by.setdefault(sig.student_id, []).append(sig)
        for sigs in by.values():
            sigs.sort(key=lambda s: (s.ts, s.seq or 0, s.row_id or 0))
        return by


def open_store(path: Path | str) -> Store:
    return Store(path)
