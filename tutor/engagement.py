"""Live activity engagement for the instructor dashboard, the weekly email,
and the admin timeline (phase 4).

The offline analysis (tutor/analytics) runs over the local store; these
run the same pure functions over the production rows for a roster and a
short window, so an instructor's page and the email show the same numbers
the offline reports would: on-screen minutes, visits, deck minutes,
narration minutes, lectures opened, covered and listened, and when the
student was last seen. One query per table, bounded by the window.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy.orm import Session as DBSession

from tutor.analytics import lectures as lectures_mod
from tutor.analytics import topics as topics_mod
from tutor.analytics import visits as visits_mod
from tutor.analytics.base import Signal, Thresholds, median
from tutor.db.models import ActivityEvent, Message, TurnEvent

THRESHOLDS = Thresholds()


def live_thresholds() -> Thresholds:
    """The thresholds the live pages use: the offline defaults with the
    heartbeat the browser is actually told (``ACTIVITY_HEARTBEAT_S``), so a
    longer heartbeat does not undercount on-screen minutes (step 0.9, F41)."""
    from tutor.config import get_config

    try:
        heartbeat = int(get_config().get("activity_heartbeat_s") or THRESHOLDS.heartbeat_s)
    except Exception:  # noqa: BLE001 - config unreadable: the default heartbeat
        heartbeat = THRESHOLDS.heartbeat_s
    return THRESHOLDS.with_overrides(heartbeat_s=heartbeat)


@dataclass
class ActivityEngagement:
    """One student's activity in a window, from the browser's events."""

    student_id: int
    visits: int = 0
    active_min: float = 0.0
    deck_min: float = 0.0
    narration_min: float = 0.0
    lectures_opened: int = 0
    lectures_covered: int = 0
    lectures_listened: int = 0
    slides_seen: int = 0
    last_seen: Optional[datetime.datetime] = None
    lectures: list = field(default_factory=list)      # LectureUse rows, by lecture
    visit_rows: list = field(default_factory=list)    # Visit objects, oldest first

    @property
    def lectures_label(self) -> str:
        """``opened (covered)`` for a table cell."""
        if not self.lectures_opened:
            return "0"
        return f"{self.lectures_opened} ({self.lectures_covered} ≥ half)"


@dataclass
class ClassActivity:
    rows: dict = field(default_factory=dict)          # student_id -> ActivityEngagement
    events: int = 0                                    # activity events seen: none means nothing to say
    total_active_min: float = 0.0
    total_visits: int = 0
    students_with_deck: int = 0
    lectures_listened: int = 0
    median_active_min: Optional[float] = None         # over the students who came


def _signals(db: DBSession, student_ids: Sequence[int], since: datetime.datetime,
             until: datetime.datetime) -> dict[int, list[Signal]]:
    """Each student's activity events and questions in the window, sorted."""
    by: dict[int, list[Signal]] = {int(s): [] for s in student_ids}
    if not student_ids:
        return by
    events = (
        db.query(ActivityEvent)
        .filter(ActivityEvent.student_id.in_(list(student_ids)),
                ActivityEvent.ts >= since, ActivityEvent.ts < until)
        .order_by(ActivityEvent.ts, ActivityEvent.seq, ActivityEvent.id)
        .all()
    )
    for e in events:
        detail = json.loads(e.detail_json) if e.detail_json else {}
        by.setdefault(e.student_id, []).append(Signal(
            ts=e.ts, source="event", name=e.name, student_id=e.student_id, course=e.course,
            lecture_num=e.lecture_num, slide_num=e.slide_num, turn_id=e.turn_id, message_id=e.message_id,
            value_ms=e.value_ms, path=e.path, detail=detail if isinstance(detail, dict) else {},
            row_id=e.id, visit_id=e.visit_id, page_view_id=e.page_view_id, seq=e.seq))
    messages = (
        db.query(Message)
        .filter(Message.student_id.in_(list(student_ids)),
                Message.created_at >= since, Message.created_at < until)
        .order_by(Message.created_at, Message.id)
        .all()
    )
    for m in messages:
        source = "question" if m.role == "user" else "reply"
        by.setdefault(m.student_id, []).append(Signal(
            ts=m.created_at, source=source, name=source, student_id=m.student_id, course=m.course,
            lecture_num=m.lecture_num, slide_num=m.slide_num, message_id=m.id, row_id=m.id))
    for sigs in by.values():
        sigs.sort(key=lambda s: (s.ts, s.seq or 0, s.row_id or 0))
    return by


def _material() -> dict:
    from tutor.analytics import store as store_mod

    try:
        return lectures_mod.material_from_rows(store_mod.lectures_rows())
    except Exception:  # the decks are not there (a fresh checkout): coverage stays unknown
        return {}


def class_activity(db: DBSession, course: str, student_ids: Sequence[int], since: datetime.datetime,
                   until: datetime.datetime, thresholds: Optional[Thresholds] = None,
                   staff_ids: Sequence[int] = ()) -> ClassActivity:
    """The roster's activity engagement for one course over the window.
    ``staff_ids`` (admins, instructors) get rows but stay out of the median."""
    thresholds = thresholds or live_thresholds()
    out = ClassActivity()
    signals = _signals(db, student_ids, since, until)
    out.events = sum(1 for sigs in signals.values() for sig in sigs if sig.source == "event")
    material = _material()
    for sid in student_ids:
        sigs = signals.get(int(sid), [])
        visits = [visits_mod.build_visit(int(sid), run, thresholds) for run in visits_mod.split_visits(sigs, thresholds)]
        totals = visits_mod.summarize_visits(visits)
        uses = lectures_mod.accumulate(sigs, material, course=course)
        lectures = [u for (_s, _c, _n), u in sorted(uses.items())]
        row = ActivityEngagement(
            student_id=int(sid), visits=totals["visits"], active_min=totals["active_min"],
            deck_min=totals["deck_min"], narration_min=totals["narration_min"],
            lectures_opened=sum(1 for u in lectures if u.opens or u.catalog_opens),
            lectures_covered=sum(1 for u in lectures if u.covered(thresholds)),
            lectures_listened=sum(1 for u in lectures if u.listened(thresholds)),
            slides_seen=sum(u.slides_seen for u in lectures),
            last_seen=totals["last_active"], lectures=lectures, visit_rows=visits,
        )
        out.rows[int(sid)] = row
        out.total_active_min += row.active_min
        out.total_visits += row.visits
        out.students_with_deck += 1 if row.deck_min > 0 or row.lectures_opened else 0
        out.lectures_listened += row.lectures_listened
    staff = {int(s) for s in staff_ids}
    came = [r.active_min for r in out.rows.values() if r.visits and r.student_id not in staff]
    middle = median(came)
    out.median_active_min = round(middle, 1) if middle is not None else None
    out.total_active_min = round(out.total_active_min, 1)
    return out


def recent_timeline(db: DBSession, student_id: int, hours: int = 48,
                    now: Optional[datetime.datetime] = None,
                    thresholds: Optional[Thresholds] = None) -> list[dict]:
    """The student's last ``hours`` of signals, newest visit last, in words
    (the admin Timeline tab)."""
    from tutor.utctime import utcnow

    thresholds = thresholds or live_thresholds()
    now = now or utcnow()
    since = now - datetime.timedelta(hours=hours)
    signals = _signals(db, [student_id], since, now).get(student_id, [])
    turns: dict[int, topics_mod.Turn] = {}
    ledger = (
        db.query(TurnEvent)
        .filter(TurnEvent.student_id == student_id, TurnEvent.kind == "chat",
                TurnEvent.ts >= since, TurnEvent.ts < now)
        .order_by(TurnEvent.ts)
        .all()
    )
    rows = list(ledger)
    for sig in signals:
        if sig.source != "question":
            continue
        t = topics_mod.Turn(student_id=student_id, ts=sig.ts, message_id=int(sig.message_id or 0),
                            course=sig.course, lecture_num=sig.lecture_num)
        for te in rows:
            if te.ts >= sig.ts - topics_mod.TURN_MATCH:
                try:
                    t.goal_ids = list(json.loads(te.goal_ids_json or "[]") or [])
                except ValueError:
                    t.goal_ids = []
                t.turn_id = te.turn_id
                rows = [r for r in rows if r is not te]
                break
        turns[t.message_id] = t
    from tutor.db.models import LearningGoal

    titles = {g.id: g.title for g in db.query(LearningGoal.id, LearningGoal.title).all()}
    return topics_mod.describe(signals, turns, titles, thresholds, with_date=True)
