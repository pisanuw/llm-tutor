"""Topics: what a turn was about, when it changed, and where a student
left off (section 3.8).

A turn's label is (course, lecture, goal): the goal the classifier found
for the turn (``turn_events.goal_ids_json``), the lecture the slides panel
showed with the question, else the lecture of the deck open around it. A
switch is a label change between consecutive turns of one visit. A
disengagement point is the last turn of a visit, annotated with the seconds
from the reply to the last on-screen signal, how the reply ended (its
verdict, an exam), what the student did after it in the visit, and the
first topic of the next visit.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional, Sequence

from tutor.analytics import visits as visits_mod
from tutor.analytics.base import Signal, Store, Thresholds, Window, seconds

TURN_MATCH = datetime.timedelta(seconds=10)   # a chat turn's ledger row is written within this of the question


@dataclass
class Turn:
    student_id: int
    ts: datetime.datetime
    message_id: int
    course: Optional[str] = None
    lecture_num: Optional[int] = None
    goal_ids: list = field(default_factory=list)
    turn_id: Optional[str] = None
    kind: Optional[str] = None            # chat | exam | error ... from the ledger
    verdict: Optional[str] = None
    intent: Optional[str] = None
    reply_ts: Optional[datetime.datetime] = None
    reply_message_id: Optional[int] = None
    deck_lecture: Optional[int] = None    # the deck open around the question

    @property
    def topic(self) -> tuple[Optional[str], Optional[int]]:
        return (self.course, self.lecture_num or self.deck_lecture)

    def label(self, goal_titles: Optional[dict] = None) -> str:
        course, lecture = self.topic
        base = f"{course or '?'}-L{lecture:02d}" if lecture else f"{course or '?'}"
        if self.goal_ids:
            title = (goal_titles or {}).get(self.goal_ids[0], self.goal_ids[0])
            return f"{base} ({title})"
        return base


def turns_labeled(store: Store, student_ids: Sequence[int], window: Window) -> dict[int, list[Turn]]:
    """Each student's questions as labeled turns, in order."""
    out: dict[int, list[Turn]] = {int(s): [] for s in student_ids}
    ledger: dict[int, list[dict]] = {}
    for row in store.turns(student_ids, window):
        if row.get("kind") in ("chat", "exam", "error", "cache_warm"):
            ledger.setdefault(int(row["student_id"]), []).append(row)
    decks: dict[int, list[Signal]] = {}
    for sig in store.events(student_ids, window, ["deck_open", "slide_view"]):
        decks.setdefault(sig.student_id, []).append(sig)
    msgs: dict[int, list[Signal]] = {}
    for sig in store.messages(student_ids, window):
        msgs.setdefault(sig.student_id, []).append(sig)

    for sid, signals in msgs.items():
        rows = ledger.get(sid, [])
        deck_sigs = decks.get(sid, [])
        current: Optional[Turn] = None
        for sig in signals:
            if sig.source == "question":
                t = Turn(student_id=sid, ts=sig.ts, message_id=int(sig.message_id or 0),
                         course=sig.course, lecture_num=sig.lecture_num)
                # The ledger row written for this question: the first chat row
                # at or just after it, before the next question.
                for row in rows:
                    if row["ts"] >= sig.ts - TURN_MATCH and row.get("kind") == "chat":
                        t.goal_ids = list(row.get("goal_ids") or [])
                        t.turn_id = row.get("turn_id")
                        t.kind = row.get("kind")
                        t.verdict = row.get("verdict")
                        t.intent = row.get("intent")
                        rows = [r for r in rows if r is not row]
                        break
                # The deck open around the question (within the visit idle gap).
                near = [d for d in deck_sigs if abs(seconds(d.ts, sig.ts)) <= 30 * 60 and d.lecture_num]
                if near:
                    nearest = min(near, key=lambda d: abs(seconds(d.ts, sig.ts)))
                    t.deck_lecture = int(nearest.lecture_num or 0) or None
                out[sid].append(t)
                current = t
            elif current is not None and current.reply_ts is None:
                current.reply_ts = sig.ts
                current.reply_message_id = int(sig.message_id or 0)
    return out


def switches(turns: Sequence[Turn], thresholds: Thresholds, goal_titles: Optional[dict] = None) -> list[dict]:
    """Label changes between consecutive turns inside one visit."""
    gap = datetime.timedelta(minutes=thresholds.idle_gap_min)
    out = []
    for prev, cur in zip(turns, turns[1:]):
        if cur.ts - prev.ts > gap:
            continue
        if prev.topic != cur.topic:
            kind = "course" if prev.course != cur.course else "lecture"
            out.append({"student_id": cur.student_id, "ts": cur.ts, "kind": kind,
                        "from": prev.label(goal_titles), "to": cur.label(goal_titles),
                        "minutes_on_previous": round(seconds(prev.ts, cur.ts) / 60, 1)})
    return out


@dataclass
class Disengagement:
    student_id: int
    visit_start: datetime.datetime
    visit_end: datetime.datetime
    last_question: datetime.datetime
    label: str
    reply_ts: Optional[datetime.datetime] = None
    verdict: Optional[str] = None
    kind: Optional[str] = None
    seconds_after_reply: Optional[float] = None
    after: str = "nothing"                 # deck | other_page | reading | nothing | no_reply
    left: bool = False
    next_visit: Optional[datetime.datetime] = None
    hours_until_next: Optional[float] = None
    next_label: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "student_id": self.student_id, "visit_start": self.visit_start, "visit_end": self.visit_end,
            "last_question": self.last_question, "topic": self.label, "verdict": self.verdict, "kind": self.kind,
            "seconds_after_reply": None if self.seconds_after_reply is None else round(self.seconds_after_reply),
            "after": self.after, "left": self.left, "next_visit": self.next_visit,
            "hours_until_next": None if self.hours_until_next is None else round(self.hours_until_next, 1),
            "next_topic": self.next_label,
        }


def disengagement_points(store: Store, student_ids: Sequence[int], window: Window,
                         thresholds: Thresholds) -> dict[int, list[Disengagement]]:
    """The last turn of each visit, annotated."""
    out: dict[int, list[Disengagement]] = {int(s): [] for s in student_ids}
    all_turns = turns_labeled(store, student_ids, window)
    titles = store.goal_titles()
    visits = visits_mod.visits_for(store, student_ids, window, thresholds)
    for sid, student_visits in visits.items():
        turns = all_turns.get(sid, [])
        for i, visit in enumerate(student_visits):
            in_visit = [t for t in turns if visit.start <= t.ts <= visit.end]
            if not in_visit:
                continue
            last = in_visit[-1]
            d = Disengagement(student_id=sid, visit_start=visit.start, visit_end=visit.end,
                              last_question=last.ts, label=last.label(titles), reply_ts=last.reply_ts,
                              verdict=last.verdict, kind=last.kind)
            if last.reply_ts is None:
                d.after = "no_reply"
            else:
                # A hide or a leave says the student was still there until then.
                later = [s for s in visit.signals if s.ts > last.reply_ts and (s.on_screen or s.off_screen)]
                if later:
                    d.seconds_after_reply = seconds(last.reply_ts, later[-1].ts)
                    if any(s.is_deck for s in later):
                        d.after = "deck"
                    elif any(s.source == "event" and s.name == "page_view" and s.path and "/chat" not in s.path
                             for s in later):
                        d.after = "other_page"
                    else:
                        d.after = "reading"
                else:
                    d.seconds_after_reply = 0.0
                    d.after = "nothing"
                d.left = d.seconds_after_reply < thresholds.left_after_min * 60
            if i + 1 < len(student_visits):
                nxt = student_visits[i + 1]
                d.next_visit = nxt.start
                d.hours_until_next = seconds(visit.end, nxt.start) / 3600
                first = next((t for t in turns if nxt.start <= t.ts <= nxt.end), None)
                d.next_label = first.label(titles) if first else None
            out[sid].append(d)
    return out


def timeline(store: Store, student_id: int, day: datetime.date, thresholds: Thresholds) -> list[dict]:
    """Every signal of one student's day, in order, in words."""
    start = datetime.datetime.combine(day, datetime.time.min)
    window = Window(start, start + datetime.timedelta(days=1))
    titles = store.goal_titles()
    turns = {t.message_id: t for t in turns_labeled(store, [student_id], window).get(student_id, [])}
    signals = store.signals([student_id], window).get(student_id, [])
    return describe(signals, turns, titles, thresholds)


def describe(signals: Sequence[Signal], turns: dict[int, Turn], titles: dict, thresholds: Thresholds,
             with_date: bool = False) -> list[dict]:
    """Signals in order, grouped into visits, each in words. Pure."""
    rows = []
    runs = visits_mod.split_visits(signals, thresholds)
    for n, run in enumerate(runs, start=1):
        for sig in run:
            where = sig.path or (f"{sig.course}-L{sig.lecture_num:02d}" if sig.course and sig.lecture_num else "")
            what = sig.name
            note = ""
            if sig.source == "question":
                t = turns.get(int(sig.message_id or 0))
                what = "question"
                note = t.label(titles) if t else ""
            elif sig.source == "reply":
                what = "reply"
            elif sig.source == "event":
                if sig.name == "slide_view":
                    note = f"slide {sig.slide_num}, {(sig.value_ms or 0) / 1000:.0f} s on slide {sig.detail.get('prev_slide')}"
                elif sig.name in ("narration_pause", "narration_ended", "narration_seek", "tts_pause", "tts_ended"):
                    note = f"{(sig.value_ms or 0) / 1000:.0f} s played"
                elif sig.name in ("page_hide", "page_heartbeat", "deck_heartbeat", "deck_close", "page_leave"):
                    note = f"{(sig.value_ms or 0) / 1000:.0f} s on screen"
                elif sig.name == "click":
                    note = sig.detail.get("label") or ""
                elif sig.name == "reply_done":
                    note = f"{sig.detail.get('outcome', 'done')}, {(sig.value_ms or 0) / 1000:.1f} s"
                elif sig.detail:
                    note = ", ".join(f"{k}={v}" for k, v in sorted(sig.detail.items()))
            elif sig.source in ("path", "graded"):
                note = str(sig.detail.get("goal_id") or "")
            stamp = sig.ts.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")
            rows.append({"visit": n, "time": stamp, "what": what, "where": where,
                         "slide": sig.slide_num or "", "turn": sig.turn_id or "", "note": note})
    return rows
