"""Daily totals: one value per calendar day for each metric
(docs/analytics-cookbook.md, section 5).

``daily_table`` buckets everything the store holds about a set of students
into the days of a window, on the calendar of a time zone (by default the
class's, ``tutor.timezone`` in courses.yaml, like the quotas): who came,
what the tutor did, what the decks and narration got, and the mastery
events. The on-screen, deck, and narration
minutes come from the visit sessionizer, so they agree with the ``class``
and ``student`` reports. ``as_rows`` is the table for markdown, CSV and JSON;
``daily_page.render`` draws one chart per metric.
"""

from __future__ import annotations

import datetime
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from tutor.analytics import visits as visits_mod
from tutor.analytics.base import Store, Thresholds, Window, parse_ts
from tutor.utctime import REPORT_TZ

UTC = datetime.timezone.utc
DEFAULT_TZ = REPORT_TZ   # courses.yaml tutor.timezone
DEFAULT_DAYS = 90

# key, title, how to read it, unit (int | min | usd | s), group
METRICS: tuple[tuple[str, str, str, str, str], ...] = (
    ("active_students", "Students active",
     "distinct students that day with a question, a login, or any browser activity", "int", "Who came"),
    ("logins", "Logins", "sessions started (magic link or Google)", "int", "Who came"),
    ("new_students", "New students", "accounts created", "int", "Who came"),
    ("visits", "Visits", "runs of activity with no idle gap over the threshold", "int", "Who came"),
    ("questions", "Questions", "messages sent to the tutor, by course", "int", "The tutor"),
    ("cost_usd", "Model cost", "dollars across every model call that day (cache warm-ups excluded)",
     "usd", "The tutor"),
    ("median_latency_s", "Reply latency", "median seconds per chat turn, from the ledger", "s", "The tutor"),
    ("code_evals", "Code evaluations", "code the tutor ran for a student", "int", "The tutor"),
    ("tts_plays", "Voice plays", "replies played in the tutor's voice", "int", "The tutor"),
    ("feedback", "Feedback", "thumbs up and down on replies", "int", "The tutor"),
    ("active_min", "On-screen minutes", "minutes a tab was visible, summed over students", "min", "Lectures"),
    ("deck_min", "Deck minutes", "minutes with a slide deck on screen", "min", "Lectures"),
    ("narration_min", "Narration minutes", "minutes of lecture narration played", "min", "Lectures"),
    ("slides_viewed", "Slides viewed", "slide views inside decks", "int", "Lectures"),
    ("decks_opened", "Decks opened", "slide decks opened", "int", "Lectures"),
    ("lectures_touched", "Lectures touched", "distinct (course, lecture) pairs asked about or opened",
     "int", "Lectures"),
    ("goals_mastered", "Goals mastered", "learning goals that reached mastered", "int", "Mastery"),
    ("exams_completed", "Mini-exams completed", "mastery exams finished", "int", "Mastery"),
    ("probes_graded", "Probes graded", "graded answers (any source)", "int", "Mastery"),
)
METRIC_KEYS = tuple(m[0] for m in METRICS)


@dataclass
class DailySeries:
    name: str
    values: list[Optional[float]]


@dataclass
class DailyMetric:
    key: str
    title: str
    desc: str
    unit: str
    group: str
    kind: str                       # sum | median
    series: list[DailySeries]

    def total(self) -> Optional[float]:
        values = [v for s in self.series for v in s.values if v is not None]
        if not values:
            return None
        if self.kind == "median":
            return statistics.median(values)
        return round(sum(values), 2)


@dataclass
class DailyTable:
    days: list[str]                 # ISO dates, in order
    tz: str
    metrics: list[DailyMetric]
    active_ids: list[list[int]] = field(default_factory=list)   # per day, who was active
    students: int = 0
    first_event: Optional[str] = None                           # the first activity event's day

    def metric(self, key: str) -> DailyMetric:
        for m in self.metrics:
            if m.key == key:
                return m
        raise KeyError(key)

    def distinct_active(self) -> int:
        return len({sid for ids in self.active_ids for sid in ids})

    def columns(self) -> list[str]:
        cols = ["day"]
        for m in self.metrics:
            cols += [m.key] if len(m.series) == 1 else [f"{m.key} {s.name}" for s in m.series]
        return cols

    def as_rows(self) -> list[dict]:
        rows = []
        for i, day in enumerate(self.days):
            row: dict[str, object] = {"day": day}
            for m in self.metrics:
                if len(m.series) == 1:
                    row[m.key] = m.series[0].values[i]
                else:
                    for s in m.series:
                        row[f"{m.key} {s.name}"] = s.values[i]
            rows.append(row)
        return rows

    def totals(self) -> list[dict]:
        return [{"metric": m.title, "value": m.total(), "meaning": m.desc} for m in self.metrics]


def day_span(window: Window, tz: ZoneInfo, days: int = DEFAULT_DAYS,
             now: Optional[datetime.datetime] = None) -> tuple[list[datetime.date], Window]:
    """The calendar days a window covers on ``tz``, and the UTC window that
    holds exactly those days. An open end is today; an open start is
    ``days`` back from the end."""
    now = now or datetime.datetime.now(UTC)
    last = ((window.until - datetime.timedelta(microseconds=1)).replace(tzinfo=UTC).astimezone(tz).date()
            if window.until else now.astimezone(tz).date())        # until is exclusive
    first = (window.since.replace(tzinfo=UTC).astimezone(tz).date() if window.since
             else last - datetime.timedelta(days=days - 1))
    if first > last:
        first = last
    dates = [first + datetime.timedelta(days=i) for i in range((last - first).days + 1)]

    def midnight(d: datetime.date) -> datetime.datetime:
        return datetime.datetime.combine(d, datetime.time.min, tzinfo=tz).astimezone(UTC).replace(tzinfo=None)

    return dates, Window(midnight(first), midnight(last + datetime.timedelta(days=1)))


def daily_table(store: Store, student_ids: Sequence[int], window: Window, thresholds: Thresholds,
                tz: ZoneInfo, days: int = DEFAULT_DAYS, now: Optional[datetime.datetime] = None) -> DailyTable:
    dates, utc_window = day_span(window, tz, days, now)
    keys = [d.isoformat() for d in dates]
    index = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    ids = [int(s) for s in student_ids]
    in_ids = "(" + ",".join(str(i) for i in ids) + ")" if ids else "(-1)"
    where, params = utc_window.sql("x.ts")

    def day_of(value: object) -> Optional[int]:
        ts = parse_ts(value)
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return index.get(ts.astimezone(tz).date().isoformat())

    def query(table: str, column: str, extra: str = "*") -> list[dict]:
        w = where.replace("x.ts", column)
        return store.rows(f"SELECT {extra} FROM {table} x WHERE x.student_id IN {in_ids} AND {w}", params)

    sums: dict[str, list[float]] = defaultdict(lambda: [0.0] * n)
    by_course: dict[str, list[float]] = defaultdict(lambda: [0.0] * n)
    fb_up, fb_down = [0.0] * n, [0.0] * n
    active: list[set[int]] = [set() for _ in range(n)]
    lectures: list[set[tuple[str, int]]] = [set() for _ in range(n)]
    latencies: list[list[float]] = [[] for _ in range(n)]

    for r in query("messages", "x.created_at"):
        i = day_of(r["created_at"])
        if i is None or r["role"] != "user":
            continue
        by_course[r["course"] or "no course"][i] += 1
        active[i].add(int(r["student_id"]))
        if r["course"] and r["lecture_num"] is not None:
            lectures[i].add((str(r["course"]), int(r["lecture_num"])))

    for r in query("sessions", "x.created_at"):
        i = day_of(r["created_at"])
        if i is not None:
            sums["logins"][i] += 1
            active[i].add(int(r["student_id"]))

    w_students = where.replace("x.ts", "x.created_at")
    for r in store.rows(f"SELECT created_at FROM students x WHERE x.id IN {in_ids} AND {w_students}", params):
        i = day_of(r["created_at"])
        if i is not None:
            sums["new_students"][i] += 1

    for r in query("turn_events", "x.ts"):
        i = day_of(r["ts"])
        if i is None:
            continue
        if r["kind"] != "cache_warm":
            sums["cost_usd"][i] += float(r["cost_usd"] or 0)
        if r["kind"] == "evaluate":
            sums["code_evals"][i] += 1
        if r["kind"] == "chat" and r["latency_ms"] is not None:
            latencies[i].append(float(r["latency_ms"]) / 1000)

    for r in query("message_feedback", "x.created_at"):
        i = day_of(r["created_at"])
        if i is not None:
            (fb_up if (r["rating"] or 0) > 0 else fb_down)[i] += 1

    for r in query("activity_events", "x.ts", "student_id, name, course, lecture_num, ts"):
        i = day_of(r["ts"])
        if i is None:
            continue
        active[i].add(int(r["student_id"]))
        if r["name"] == "slide_view":
            sums["slides_viewed"][i] += 1
        elif r["name"] == "deck_open":
            sums["decks_opened"][i] += 1
            if r["course"] and r["lecture_num"] is not None:
                lectures[i].add((str(r["course"]), int(r["lecture_num"])))
        elif r["name"] == "tts_play":
            sums["tts_plays"][i] += 1

    for vs in visits_mod.visits_for(store, ids, utc_window, thresholds).values():
        for v in vs:
            i = day_of(v.start)
            if i is None:
                continue
            sums["visits"][i] += 1
            sums["active_min"][i] += v.active_s / 60
            sums["deck_min"][i] += v.deck_s / 60
            sums["narration_min"][i] += v.narration_s / 60

    for table, column, key in (("student_progress", "x.mastered_at", "goals_mastered"),
                               ("mastery_attempts", "x.completed_at", "exams_completed"),
                               ("graded_events", "x.ts", "probes_graded")):
        for r in query(table, column, column[2:]):
            i = day_of(r[column[2:]])
            if i is not None:
                sums[key][i] += 1

    sums["active_students"] = [float(len(s)) for s in active]
    sums["lectures_touched"] = [float(len(s)) for s in lectures]

    def rounded(values: list[float], nd: int) -> list[Optional[float]]:
        return [round(v, nd) if nd else float(round(v)) for v in values]

    metrics: list[DailyMetric] = []
    for key, title, desc, unit, group in METRICS:
        kind = "sum"
        if key == "questions":
            series = [DailySeries(c, rounded(by_course[c], 0)) for c in sorted(by_course)]
            if not series:
                series = [DailySeries(title, [0.0] * n)]
        elif key == "feedback":
            series = [DailySeries("thumbs up", rounded(fb_up, 0)), DailySeries("thumbs down", rounded(fb_down, 0))]
        elif key == "median_latency_s":
            kind = "median"
            series = [DailySeries(title, [round(statistics.median(ls), 1) if ls else None for ls in latencies])]
        else:
            series = [DailySeries(title, rounded(sums[key], 2 if unit == "usd" else 1 if unit == "min" else 0))]
        metrics.append(DailyMetric(key, title, desc, unit, group, kind, series))

    first = store.one("SELECT MIN(ts) AS ts FROM activity_events")
    first_ts = parse_ts(first["ts"]) if first and first["ts"] else None
    return DailyTable(days=keys, tz=str(tz), metrics=metrics,
                      active_ids=[sorted(s) for s in active], students=len(ids),
                      first_event=(first_ts.replace(tzinfo=UTC).astimezone(tz).date().isoformat()
                                   if first_ts else None))
