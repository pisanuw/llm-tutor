"""Visits: sessionization and on-screen time (analytics plan,
section 3.8).

A visit is a maximal run of one student's signals with no gap over the idle
threshold. Active time is the union of the on-screen spans: each on-screen
signal keeps the student active until the next signal, for at most the
heartbeat interval plus a grace; a hide, a leave, or a deck close ends the
span at once. Deck time is the same rule over the deck's own signals;
narration and voice time are the spans the clips reported as played, with
an unfinished clip credited up to its last heartbeat.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from tutor.analytics.base import Signal, Store, Thresholds, Window, seconds


@dataclass
class Visit:
    student_id: int
    start: datetime.datetime
    end: datetime.datetime
    signals: list[Signal] = field(default_factory=list)
    active_s: float = 0.0
    deck_s: float = 0.0
    narration_s: float = 0.0
    tts_s: float = 0.0
    questions: int = 0
    replies: int = 0
    pages: int = 0
    paths: list[str] = field(default_factory=list)
    courses: set = field(default_factory=set)
    lectures: set = field(default_factory=set)       # (course, lecture_num)
    slides_seen: int = 0
    lost_batches: int = 0
    first_question: Optional[datetime.datetime] = None
    last_question: Optional[datetime.datetime] = None
    last_reply: Optional[datetime.datetime] = None

    @property
    def wall_s(self) -> float:
        return seconds(self.start, self.end)

    @property
    def day(self) -> datetime.date:
        return self.start.date()

    def as_dict(self) -> dict:
        return {
            "student_id": self.student_id, "start": self.start, "end": self.end,
            "wall_min": round(self.wall_s / 60, 1), "active_min": round(self.active_s / 60, 1),
            "deck_min": round(self.deck_s / 60, 1), "narration_min": round(self.narration_s / 60, 1),
            "tts_min": round(self.tts_s / 60, 1), "questions": self.questions, "replies": self.replies,
            "pages": self.pages, "paths": " ".join(self.paths), "courses": ",".join(sorted(self.courses)),
            "lectures": ",".join(f"{c}-L{n:02d}" for c, n in sorted(self.lectures)),
            "slides_seen": self.slides_seen, "lost_batches": self.lost_batches,
        }


def split_visits(signals: Sequence[Signal], thresholds: Thresholds) -> list[list[Signal]]:
    """Runs of signals with no gap over the idle threshold."""
    gap = datetime.timedelta(minutes=thresholds.idle_gap_min)
    runs: list[list[Signal]] = []
    for sig in signals:
        if runs and sig.ts - runs[-1][-1].ts <= gap:
            runs[-1].append(sig)
        else:
            runs.append([sig])
    return runs


def active_seconds(signals: Sequence[Signal], thresholds: Thresholds, pick=lambda s: True) -> float:
    """The union of on-screen spans over the signals ``pick`` keeps: an
    on-screen signal holds until the next signal of any kind, for at most
    heartbeat + grace; an off-screen one ends the span."""
    extent = thresholds.active_extent()
    total = 0.0
    span_start: Optional[datetime.datetime] = None
    span_end: Optional[datetime.datetime] = None
    for i, sig in enumerate(signals):
        if not pick(sig):
            continue
        nxt = signals[i + 1].ts if i + 1 < len(signals) else None
        if sig.off_screen:
            if span_start is not None and span_end is not None:
                end = min(span_end, sig.ts)
                total += max(0.0, seconds(span_start, end))
            span_start = span_end = None
            continue
        if not sig.on_screen:
            continue
        hold_until = sig.ts + extent
        if nxt is not None:
            hold_until = min(hold_until, nxt)
        if span_start is not None and span_end is not None and sig.ts <= span_end:
            span_end = max(span_end, hold_until)      # overlapping: extend
        else:
            if span_start is not None and span_end is not None:
                total += max(0.0, seconds(span_start, span_end))
            span_start, span_end = sig.ts, hold_until
    if span_start is not None and span_end is not None:
        total += max(0.0, seconds(span_start, span_end))
    return total


def played_seconds(signals: Iterable[Signal], play: str, stops: Iterable[str], playing_flag: Optional[str] = None) -> float:
    """Seconds a clip played: the spans the stop events report, plus, for a
    play never stopped (the tab died), the time to the last heartbeat that
    still saw it playing."""
    stop_names = set(stops)
    total = 0.0
    playing_since: Optional[datetime.datetime] = None
    last_seen_playing: Optional[datetime.datetime] = None
    for sig in signals:
        if sig.source != "event":
            continue
        if sig.name == play:
            playing_since = sig.ts
            last_seen_playing = sig.ts
        elif sig.name in stop_names:
            total += (sig.value_ms or 0) / 1000.0
            playing_since = sig.ts if sig.name.endswith("_seek") else None
        elif playing_flag and sig.name == "deck_heartbeat" and playing_since is not None:
            if sig.detail.get(playing_flag):
                last_seen_playing = sig.ts
    if playing_since is not None and last_seen_playing is not None and last_seen_playing > playing_since:
        total += seconds(playing_since, last_seen_playing)
    return total


def lost_batches(signals: Iterable[Signal]) -> int:
    """Page views whose sequence numbers have a gap (a batch never arrived)."""
    seen: dict[str, set] = {}
    for sig in signals:
        if sig.source == "event" and sig.page_view_id and sig.seq:
            seen.setdefault(sig.page_view_id, set()).add(sig.seq)
    return sum(1 for seqs in seen.values() if max(seqs) != len(seqs))


def build_visit(student_id: int, run: Sequence[Signal], thresholds: Thresholds) -> Visit:
    v = Visit(student_id=student_id, start=run[0].ts, end=run[-1].ts, signals=list(run))
    v.active_s = active_seconds(run, thresholds)
    v.deck_s = active_seconds(run, thresholds, pick=lambda s: s.is_deck or (s.off_screen and s.name == "deck_close"))
    v.narration_s = played_seconds(run, "narration_play", ("narration_pause", "narration_seek", "narration_ended"),
                                   playing_flag="narration_playing")
    v.tts_s = played_seconds(run, "tts_play", ("tts_pause", "tts_ended"))
    v.lost_batches = lost_batches(run)
    page_views: set[str] = set()
    slides: set[tuple] = set()
    for sig in run:
        if sig.course:
            v.courses.add(sig.course)
        if sig.course and sig.lecture_num:
            v.lectures.add((sig.course, int(sig.lecture_num)))
        if sig.source == "question":
            v.questions += 1
            v.first_question = v.first_question or sig.ts
            v.last_question = sig.ts
        elif sig.source == "reply":
            v.replies += 1
            v.last_reply = sig.ts
        elif sig.source == "event":
            if sig.name == "page_view" and sig.page_view_id:
                page_views.add(sig.page_view_id)
                if sig.path and (not v.paths or v.paths[-1] != sig.path):
                    v.paths.append(sig.path)
            if sig.name in ("deck_open", "slide_view") and sig.slide_num and sig.lecture_num:
                slides.add((sig.course, sig.lecture_num, sig.slide_num))
    v.pages = len(page_views)
    v.slides_seen = len(slides)
    return v


def visits_for(store: Store, student_ids: Sequence[int], window: Window, thresholds: Thresholds) -> dict[int, list[Visit]]:
    """Each student's visits in the window."""
    out: dict[int, list[Visit]] = {}
    for sid, signals in store.signals(student_ids, window).items():
        out[sid] = [build_visit(sid, run, thresholds) for run in split_visits(signals, thresholds)]
    return out


def summarize_visits(visits: Sequence[Visit]) -> dict:
    """Totals over a list of visits."""
    return {
        "visits": len(visits),
        "active_days": len({v.day for v in visits}),
        "active_min": round(sum(v.active_s for v in visits) / 60, 1),
        "deck_min": round(sum(v.deck_s for v in visits) / 60, 1),
        "narration_min": round(sum(v.narration_s for v in visits) / 60, 1),
        "tts_min": round(sum(v.tts_s for v in visits) / 60, 1),
        "questions": sum(v.questions for v in visits),
        "slides_seen": sum(v.slides_seen for v in visits),
        "lost_batches": sum(v.lost_batches for v in visits),
        "last_active": max((v.end for v in visits), default=None),
    }
