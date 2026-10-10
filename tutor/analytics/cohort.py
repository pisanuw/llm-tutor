"""Cohorts: per-student metrics, class summaries, and comparisons
(section 3.8).

The per-student metric table over a window is the one computation; a class
summary takes its median and quartiles over the roster (a student who never
came counts as zeros and is listed), a comparison puts classes side by
side, and a student report shows one student's numbers with their
percentile in each class they are in. The weekly trend is the same table
per ISO week.
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

from tutor.analytics import lectures as lectures_mod
from tutor.analytics import questions as questions_mod
from tutor.analytics import topics as topics_mod
from tutor.analytics import visits as visits_mod
from tutor.analytics.base import Store, Thresholds, Window, median, percentile_rank, quartiles

# (key, heading, how to read it)
METRICS: tuple[tuple[str, str, str], ...] = (
    ("active_days", "days", "days with any activity"),
    ("visits", "visits", "runs of activity with no idle gap over the threshold"),
    ("active_min", "active min", "minutes on screen"),
    ("questions", "questions", "messages sent to the tutor"),
    ("median_think_s", "think s", "median seconds from a reply to the next question"),
    ("follow_up_rate", "follow-up", "share of questions asked within the follow-up window"),
    ("abandoned_rate", "abandoned", "share of questions whose reply the browser never saw finish"),
    ("deck_min", "deck min", "minutes with a deck on screen"),
    ("slides_seen", "slides", "distinct slides shown"),
    ("lectures_covered", "covered", "lectures with enough slides seen"),
    ("narration_min", "narration min", "minutes of lecture narration played"),
    ("lectures_listened", "listened", "lectures with enough narration played"),
    ("tts_plays", "voice plays", "replies played in the tutor's voice"),
    ("switches_per_visit", "switches/visit", "topic changes per visit"),
)
METRIC_KEYS = tuple(m[0] for m in METRICS)
ZERO = {k: 0 for k in METRIC_KEYS}


@dataclass
class StudentRow:
    student_id: int
    label: str
    metrics: dict
    last_active: Optional[datetime.datetime] = None

    def as_dict(self) -> dict:
        return {"student": self.label, **self.metrics, "last_active": self.last_active}


def student_metrics(store: Store, student_ids: Sequence[int], window: Window, thresholds: Thresholds,
                    course: Optional[str] = None) -> dict[int, StudentRow]:
    """The metric table: every student in ``student_ids``, zeros for one
    with nothing in the window."""
    ids = [int(s) for s in student_ids]
    visits = visits_mod.visits_for(store, ids, window, thresholds)
    uses = lectures_mod.by_student(lectures_mod.lecture_use(store, ids, window, course=course))
    gaps = questions_mod.question_gaps(store, ids, window, thresholds, course=course)
    abandoned = questions_mod.abandoned_turns(store, ids, window)
    turns = topics_mod.turns_labeled(store, ids, window)
    tts_plays = Counter(s.student_id for s in store.events(ids, window, ["tts_play"]))
    out: dict[int, StudentRow] = {}
    for sid in ids:
        v = visits.get(sid, [])
        totals = visits_mod.summarize_visits(v)
        q = questions_mod.summarize_questions(gaps.get(sid, []), abandoned.get(sid))
        uses_for = uses.get(sid, [])
        n_switches = len(topics_mod.switches(turns.get(sid, []), thresholds))
        metrics = {
            "active_days": totals["active_days"],
            "visits": totals["visits"],
            "active_min": totals["active_min"],
            "questions": q["questions"] if course else totals["questions"],
            "median_think_s": q["median_think_s"],
            "follow_up_rate": q["follow_up_rate"],
            "abandoned_rate": q["abandoned_rate"],
            "deck_min": totals["deck_min"],
            "slides_seen": sum(u.slides_seen for u in uses_for),
            "lectures_covered": sum(1 for u in uses_for if u.covered(thresholds)),
            "narration_min": totals["narration_min"],
            "lectures_listened": sum(1 for u in uses_for if u.listened(thresholds)),
            "tts_plays": tts_plays.get(sid, 0),
            "switches_per_visit": round(n_switches / len(v), 2) if v else 0,
        }
        out[sid] = StudentRow(student_id=sid, label=store.student_label(sid), metrics=metrics,
                              last_active=totals["last_active"])
    return out


def _numbers(rows: Sequence[StudentRow], key: str) -> list[float]:
    return [float(r.metrics[key]) for r in rows if r.metrics.get(key) is not None]


def summarize(rows: Sequence[StudentRow]) -> list[dict]:
    """One line per metric: median, quartiles, mean, and how many had a value."""
    out = []
    for key, heading, meaning in METRICS:
        values = _numbers(rows, key)
        q1, q2, q3 = quartiles(values)
        out.append({
            "metric": heading, "key": key, "n": len(values),
            "median": _fmt(q2), "q1": _fmt(q1), "q3": _fmt(q3),
            "mean": _fmt(sum(values) / len(values)) if values else None,
            "meaning": meaning,
        })
    return out


def _fmt(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return round(v, 2) if abs(v) < 10 else round(v, 1)


@dataclass
class ClassSummary:
    class_id: int
    label: str
    course: str
    roster: list
    rows: list
    summary: list

    @property
    def n_enrolled(self) -> int:
        return len(self.roster)

    @property
    def n_active(self) -> int:
        return sum(1 for r in self.rows if r.metrics["active_days"] > 0)

    @property
    def never_active(self) -> list:
        return [r.label for r in self.rows if r.metrics["active_days"] == 0]


def class_summary(store: Store, class_id: int, window: Window, thresholds: Thresholds) -> ClassSummary:
    roster = store.roster(class_id)
    course = store.class_course(class_id)
    table = student_metrics(store, roster, window, thresholds, course=course)
    rows = [table[sid] for sid in roster]
    return ClassSummary(class_id=class_id, label=store.class_label(class_id), course=course, roster=roster,
                        rows=rows, summary=summarize(rows))


def compare_classes(store: Store, class_ids: Sequence[int], window: Window, thresholds: Thresholds) -> dict:
    """Class summaries side by side: the median of each metric per class,
    with the enrolled and active counts."""
    summaries = [class_summary(store, cid, window, thresholds) for cid in class_ids]
    header = [{"metric": "enrolled", **{s.label: s.n_enrolled for s in summaries}},
              {"metric": "active", **{s.label: s.n_active for s in summaries}},
              {"metric": "active share", **{s.label: round(s.n_active / s.n_enrolled, 2) if s.n_enrolled else None
                                             for s in summaries}}]
    rows = []
    for key, heading, _meaning in METRICS:
        row: dict[str, object] = {"metric": f"{heading} (median)"}
        for s in summaries:
            row[s.label] = next(line["median"] for line in s.summary if line["key"] == key)
        rows.append(row)
        row_q: dict[str, object] = {"metric": f"{heading} (q1..q3)"}
        for s in summaries:
            line = next(line for line in s.summary if line["key"] == key)
            row_q[s.label] = f"{line['q1']}..{line['q3']}" if line["q1"] is not None else None
        rows.append(row_q)
    return {"summaries": summaries, "table": header + rows}


def student_report(store: Store, student_id: int, window: Window, thresholds: Thresholds) -> dict:
    """One student's metrics, and their percentile within each of their classes."""
    own = student_metrics(store, [student_id], window, thresholds)[student_id]
    percentiles = []
    for cid in store.classes_of(student_id):
        roster = store.roster(cid)
        course = store.class_course(cid)
        table = student_metrics(store, roster, window, thresholds, course=course)
        mine = table[student_id].metrics
        row: dict[str, object] = {"class": store.class_label(cid)}
        for key, heading, _m in METRICS:
            population = [float(table[s].metrics[key]) for s in roster if table[s].metrics.get(key) is not None]
            value = mine.get(key)
            pct = percentile_rank(float(value), population) if value is not None else None
            row[heading] = f"{value} (p{round(pct)})" if pct is not None else ("" if value is None else str(value))
        percentiles.append(row)
    return {"row": own, "percentiles": percentiles}


def weekly_trend(store: Store, student_ids: Sequence[int], window: Window, thresholds: Thresholds) -> list[dict]:
    """Per ISO week: active students, visits, questions, on-screen and deck minutes."""
    visits = visits_mod.visits_for(store, [int(s) for s in student_ids], window, thresholds)
    weeks: dict[str, dict] = {}
    for sid, vs in visits.items():
        for v in vs:
            iso = v.start.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
            w = weeks.setdefault(key, {"week": key, "students": set(), "visits": 0, "questions": 0,
                                       "active_min": 0.0, "deck_min": 0.0, "narration_min": 0.0})
            w["students"].add(sid)
            w["visits"] += 1
            w["questions"] += v.questions
            w["active_min"] += v.active_s / 60
            w["deck_min"] += v.deck_s / 60
            w["narration_min"] += v.narration_s / 60
    out = []
    for key in sorted(weeks):
        w = weeks[key]
        out.append({"week": key, "active_students": len(w["students"]), "visits": w["visits"],
                    "questions": w["questions"], "active_min": round(w["active_min"]),
                    "deck_min": round(w["deck_min"]), "narration_min": round(w["narration_min"])})
    return out


def median_of(rows: Sequence[StudentRow], key: str) -> Optional[float]:
    return median(_numbers(rows, key))
