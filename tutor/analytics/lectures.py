"""Lectures: slides shown, dwell, narration listened
(section 3.8).

Per student and lecture: the slides seen and the on-screen dwell on each
(a ``slide_view`` carries the dwell on the slide before it, a ``deck_close``
the dwell on the last one), coverage against the deck's slide count, the
median dwell (reading or flipping), the narration seconds played against
the lecture's narration, and where the deck was opened (the chat panel, the
lecture page, a tab).
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional, Sequence

from tutor.analytics.base import Signal, Store, Thresholds, Window, median

_NARRATION_STOPS = ("narration_pause", "narration_seek", "narration_ended")


@dataclass
class LectureUse:
    student_id: int
    course: str
    lecture_num: int
    title: str = ""
    slides_total: int = 0
    narration_total_s: Optional[float] = None
    opens: int = 0
    contexts: Counter = field(default_factory=Counter)
    dwell_s: dict = field(default_factory=lambda: defaultdict(float))   # slide -> seconds
    views: Counter = field(default_factory=Counter)                       # slide -> times shown
    played_s: dict = field(default_factory=lambda: defaultdict(float))   # slide -> narration seconds
    catalog_opens: int = 0
    cited: int = 0
    first: Optional[datetime.datetime] = None
    last: Optional[datetime.datetime] = None

    @property
    def slides_seen(self) -> int:
        return len(set(self.views) | {s for s, d in self.dwell_s.items() if d > 0})

    @property
    def coverage_pct(self) -> Optional[float]:
        denominator = self.slides_total or (max(self.views) if self.views else 0)
        if not denominator:
            return None
        return min(100.0, 100.0 * self.slides_seen / denominator)

    @property
    def deck_s(self) -> float:
        return sum(self.dwell_s.values())

    @property
    def median_dwell_s(self) -> Optional[float]:
        return median([d for d in self.dwell_s.values() if d > 0])

    @property
    def narration_played_s(self) -> float:
        return sum(self.played_s.values())

    @property
    def listened_pct(self) -> Optional[float]:
        if not self.narration_total_s:
            return None
        return min(100.0, 100.0 * self.narration_played_s / self.narration_total_s)

    def reading(self, thresholds: Thresholds) -> Optional[str]:
        d = self.median_dwell_s
        if d is None:
            return None
        return "read" if d >= thresholds.read_slide_s else "flipped"

    def covered(self, thresholds: Thresholds) -> bool:
        return (self.coverage_pct or 0) >= thresholds.coverage_pct

    def listened(self, thresholds: Thresholds) -> bool:
        return (self.listened_pct or 0) >= thresholds.listened_pct

    def as_dict(self, thresholds: Thresholds) -> dict:
        return {
            "student_id": self.student_id, "course": self.course, "lecture": self.lecture_num,
            "title": self.title, "opens": self.opens, "catalog_opens": self.catalog_opens,
            "contexts": ",".join(f"{k}:{v}" for k, v in sorted(self.contexts.items())),
            "slides_seen": self.slides_seen, "slides_total": self.slides_total,
            "coverage_pct": None if self.coverage_pct is None else round(self.coverage_pct),
            "deck_min": round(self.deck_s / 60, 1),
            "median_dwell_s": None if self.median_dwell_s is None else round(self.median_dwell_s, 1),
            "reading": self.reading(thresholds),
            "narration_min": round(self.narration_played_s / 60, 1),
            "listened_pct": None if self.listened_pct is None else round(self.listened_pct),
            "listened": self.listened(thresholds), "covered": self.covered(thresholds),
            "cited": self.cited, "first": self.first, "last": self.last,
        }


LECTURE_EVENTS = ("deck_open", "slide_view", "deck_close", "lecture_open", "slide_cited", *_NARRATION_STOPS)


def lecture_use(store: Store, student_ids: Sequence[int], window: Window,
                course: Optional[str] = None, lecture: Optional[int] = None) -> dict[tuple[int, str, int], LectureUse]:
    """``{(student_id, course, lecture_num): LectureUse}`` from the deck events."""
    return accumulate(store.events(student_ids, window, LECTURE_EVENTS), store.lectures(), course, lecture)


def material_from_rows(rows: Sequence[dict]) -> dict[tuple[str, int], dict]:
    """The ``lectures`` rows (tutor/analytics/store.py) as the material map
    the accumulation reads; the live dashboard builds them from the decks."""
    import json

    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        per_slide = json.loads(r["narration_s_json"]) if r.get("narration_s_json") else {}
        out[(str(r["course"]), int(r["lecture_num"]))] = {
            "title": r.get("title") or "", "slides_total": int(r.get("slides_total") or 0),
            "narration_total_s": float(r["narration_total_s"]) if r.get("narration_total_s") else None,
            "narration_s": {int(k): float(v) for k, v in per_slide.items()},
        }
    return out


def accumulate(signals: Sequence[Signal], material: dict[tuple[str, int], dict],
               course: Optional[str] = None, lecture: Optional[int] = None) -> dict[tuple[int, str, int], LectureUse]:
    """Fold deck events into one LectureUse per student and lecture. Pure."""
    uses: dict[tuple[int, str, int], LectureUse] = {}

    def use_for(sig: Signal) -> Optional[LectureUse]:
        if not sig.course or not sig.lecture_num:
            return None
        if course and sig.course != course:
            return None
        if lecture and int(sig.lecture_num) != lecture:
            return None
        key = (sig.student_id, sig.course, int(sig.lecture_num))
        if key not in uses:
            meta = material.get((sig.course, int(sig.lecture_num)), {})
            uses[key] = LectureUse(student_id=sig.student_id, course=sig.course, lecture_num=int(sig.lecture_num),
                                   title=meta.get("title", ""), slides_total=int(meta.get("slides_total") or 0),
                                   narration_total_s=meta.get("narration_total_s"))
        u = uses[key]
        u.first = sig.ts if u.first is None or sig.ts < u.first else u.first
        u.last = sig.ts if u.last is None or sig.ts > u.last else u.last
        return u

    for sig in signals:
        if sig.source != "event" or sig.name not in LECTURE_EVENTS:
            continue
        u = use_for(sig)
        if u is None:
            continue
        if sig.name == "deck_open":
            u.opens += 1
            u.contexts[str(sig.detail.get("context") or "tab")] += 1
            if sig.slide_num:
                u.views[int(sig.slide_num)] += 1
        elif sig.name == "slide_view":
            if sig.slide_num:
                u.views[int(sig.slide_num)] += 1
            prev = sig.detail.get("prev_slide")
            if prev and sig.value_ms:
                u.dwell_s[int(prev)] += sig.value_ms / 1000.0
        elif sig.name == "deck_close":
            if sig.slide_num and sig.value_ms:
                u.dwell_s[int(sig.slide_num)] += sig.value_ms / 1000.0
        elif sig.name == "lecture_open":
            u.catalog_opens += 1
        elif sig.name == "slide_cited":
            u.cited += 1
        elif sig.name in _NARRATION_STOPS and sig.value_ms:
            u.played_s[int(sig.slide_num or 0)] += sig.value_ms / 1000.0
    return uses


def by_student(uses: dict[tuple[int, str, int], LectureUse]) -> dict[int, list[LectureUse]]:
    out: dict[int, list[LectureUse]] = defaultdict(list)
    for (sid, _c, _n), u in sorted(uses.items()):
        out[sid].append(u)
    return out


def class_lecture_table(uses: dict[tuple[int, str, int], LectureUse], roster: Sequence[int], course: str,
                        thresholds: Thresholds, lecture_numbers: Optional[Sequence[int]] = None) -> list[dict]:
    """One row per lecture: how many of the roster opened it, covered it,
    listened to it, and the median coverage and listened fraction among
    those who opened it."""
    numbers = sorted(lecture_numbers or {n for (_s, c, n) in uses if c == course})
    rows = []
    for n in numbers:
        opened = [uses[(sid, course, n)] for sid in roster if (sid, course, n) in uses]
        rows.append({
            "lecture": n,
            "title": next((u.title for u in opened if u.title), ""),
            "opened": len(opened), "roster": len(roster),
            "covered": sum(1 for u in opened if u.covered(thresholds)),
            "listened": sum(1 for u in opened if u.listened(thresholds)),
            "median_coverage_pct": _round(median([u.coverage_pct for u in opened if u.coverage_pct is not None])),
            "median_listened_pct": _round(median([u.listened_pct for u in opened if u.listened_pct is not None])),
            "median_deck_min": _round(median([u.deck_s / 60 for u in opened]), 1),
            "read": sum(1 for u in opened if u.reading(thresholds) == "read"),
            "flipped": sum(1 for u in opened if u.reading(thresholds) == "flipped"),
        })
    return rows


def student_lecture_matrix(uses: dict[tuple[int, str, int], LectureUse], roster: Sequence[int], course: str,
                           labels: dict[int, str], lecture_numbers: Sequence[int]) -> list[dict]:
    """Roster x lectures: ``coverage%/listened%`` per cell, blank when never opened."""
    rows = []
    for sid in roster:
        row: dict = {"student": labels.get(sid, f"S{sid}")}
        for n in lecture_numbers:
            u = uses.get((sid, course, n))
            if u is None:
                row[f"L{n:02d}"] = ""
            else:
                cov = "" if u.coverage_pct is None else f"{round(u.coverage_pct)}%"
                lis = "" if u.listened_pct is None else f"{round(u.listened_pct)}%"
                row[f"L{n:02d}"] = f"{cov}/{lis}" if lis else cov
        rows.append(row)
    return rows


def _round(v: Optional[float], digits: int = 0) -> Optional[float]:
    if v is None:
        return None
    return round(v, digits) if digits else round(v)
