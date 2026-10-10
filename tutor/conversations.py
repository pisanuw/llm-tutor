"""Recent conversations: the list an admin or an instructor scans before
opening a transcript.

A conversation is one student's run of messages with no gap over ``GAP``
between consecutive messages, computed when a page renders from one query
over ``messages``; nothing is stored. Each row carries when and for how
long, who, where (course, lectures), the first question, the counts, the
model spend of the turns in it, and the flags: thumbs-down, detector cuts,
exam turns, a quota hit, and a student's first conversation. Turns join a
conversation by time, since messages carry no turn id; the gap between
conversations is six times the tolerance, so a turn lands in one at most.

The admin tab and the instructor view both read ``recent`` and ``thread``;
the routers only scope (which students, which course) and render.
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from tutor.db.models import ClassEnrollment, CourseClass, Message, MessageFeedback, Student, TurnEvent
from tutor.utctime import to_local, utcnow

GAP = datetime.timedelta(minutes=30)          # a longer silence starts a new conversation
TURN_SLACK = datetime.timedelta(minutes=5)    # a turn this close to a conversation belongs to it
ACTIVE_WITHIN = datetime.timedelta(minutes=5)  # the green dot
SEEN_BURST = datetime.timedelta(minutes=10)   # one sitting, for the "new since your last visit" baseline
DEFAULT_DAYS = 7
DAY_CHOICES = (1, 7, 30)
MAX_ROWS = 200
PREVIEW_CHARS = 80
UNCOUNTED_KINDS = ("cache_warm", "quota")     # ledger rows that are not the student's spend
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

@dataclass
class Msg:
    """The slice of a message the list needs (the content only as a preview)."""
    id: int
    student_id: int
    role: str
    created_at: datetime.datetime
    course: Optional[str]
    lecture_num: Optional[int]
    preview: str


@dataclass
class Conversation:
    student_id: int
    start: datetime.datetime
    end: datetime.datetime
    first_message_id: int
    message_ids: list[int]
    courses: list[str]
    lectures: list[int]
    preview: str
    questions: int
    replies: int
    email: str = ""
    name: str = ""
    classes: list[str] = field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0
    cuts: int = 0
    exams: int = 0
    quota_hits: int = 0
    thumbs_down: int = 0
    thumbs_up: int = 0
    first_ever: bool = False
    active: bool = False
    is_new: bool = False
    links: dict[str, str] = field(default_factory=dict)   # the router's URLs: thread, open, ...
    when: str = ""        # "13:47", "Mon 13:47" or "Oct 2 13:47", local time
    ago: str = ""         # "12 min ago"
    span: str = ""        # "14 min"

    @property
    def duration(self) -> datetime.timedelta:
        return self.end - self.start

    @property
    def course(self) -> str:
        return ", ".join(self.courses)

    @property
    def lecture_labels(self) -> list[str]:
        return [f"L{n:02d}" for n in self.lectures]

    @property
    def class_label(self) -> str:
        return ", ".join(self.classes)

    @property
    def flags(self) -> int:
        """How many kinds of badge the row shows (the sort key for Flags)."""
        return sum(1 for on in (self.thumbs_down, self.cuts, self.exams, self.quota_hits, self.first_ever) if on)

    @property
    def flagged(self) -> bool:
        """What the "flagged only" filter keeps: something went wrong, or was stopped."""
        return bool(self.thumbs_down or self.cuts or self.quota_hits)

    @property
    def start_iso(self) -> str:
        return self.start.isoformat(timespec="seconds")

    @property
    def end_iso(self) -> str:
        return self.end.isoformat(timespec="seconds")


@dataclass
class Listing:
    rows: list[Conversation]
    total: int                 # before the MAX_ROWS cap
    since: datetime.datetime
    until: datetime.datetime

    @property
    def capped(self) -> bool:
        return self.total > len(self.rows)


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------

def split(messages: Sequence[Msg], gap: datetime.timedelta = GAP) -> list[list[Msg]]:
    """Runs of one student's messages (given in time order) with no gap over ``gap``."""
    runs: list[list[Msg]] = []
    for m in messages:
        if runs and m.student_id == runs[-1][-1].student_id and m.created_at - runs[-1][-1].created_at <= gap:
            runs[-1].append(m)
        else:
            runs.append([m])
    return runs


def clean_preview(text: Optional[str], limit: int = PREVIEW_CHARS) -> str:
    text = _WS.sub(" ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build(run: Sequence[Msg]) -> Conversation:
    first_q = next((m for m in run if m.role == "user"), None)
    courses: list[str] = []
    lectures: list[int] = []
    for m in run:
        if m.course and m.course not in courses:
            courses.append(m.course)
        if m.lecture_num and m.lecture_num not in lectures:
            lectures.append(m.lecture_num)
    return Conversation(
        student_id=run[0].student_id,
        start=run[0].created_at,
        end=run[-1].created_at,
        first_message_id=run[0].id,
        message_ids=[m.id for m in run],
        courses=courses,
        lectures=sorted(lectures),
        preview=clean_preview(first_q.preview if first_q else ""),
        questions=sum(1 for m in run if m.role == "user"),
        replies=sum(1 for m in run if m.role == "assistant"),
    )


def _detector_cut(te: TurnEvent) -> bool:
    """The rule the Supervision tab uses: the verdicts record the cut, and a
    det_check of {"cut": false} is not one."""
    def _load(raw: Optional[str]) -> dict:
        try:
            value = json.loads(raw or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return bool(_load(te.verdicts_json).get("detector_cut")) or _load(te.det_check_json).get("cut") is True


def attach_turns(rows: Sequence[Conversation], turns: Iterable[TurnEvent],
                 slack: datetime.timedelta = TURN_SLACK) -> None:
    """Give every turn to the conversation of its student whose span, widened
    by ``slack``, holds its ``ts``; turns outside every span are dropped."""
    by_student: dict[int, list[Conversation]] = {}
    for c in rows:
        by_student.setdefault(c.student_id, []).append(c)
    for convs in by_student.values():
        convs.sort(key=lambda c: c.start)
    for te in turns:
        for c in by_student.get(te.student_id, ()):
            if c.start - slack <= te.ts <= c.end + slack:
                if te.kind == "quota":
                    c.quota_hits += 1
                elif te.kind == "exam":
                    c.exams += 1
                if te.kind not in UNCOUNTED_KINDS:
                    c.turns += 1
                    c.cost_usd += float(te.cost_usd or 0.0)
                    if _detector_cut(te):
                        c.cuts += 1
                break


def attach_feedback(rows: Sequence[Conversation], feedback: Iterable[MessageFeedback]) -> None:
    owner: dict[int, Conversation] = {mid: c for c in rows for mid in c.message_ids}
    for f in feedback:
        c = owner.get(f.message_id)
        if c is None:
            continue
        if f.rating < 0:
            c.thumbs_down += 1
        elif f.rating > 0:
            c.thumbs_up += 1


def attach_first_ever(rows: Sequence[Conversation], first_ids: dict[int, int]) -> None:
    for c in rows:
        c.first_ever = first_ids.get(c.student_id) == c.first_message_id


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def ago(ts: datetime.datetime, now: datetime.datetime) -> str:
    seconds = max(0, int((now - ts).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    return f"{days} d ago"


def span_words(start: datetime.datetime, end: datetime.datetime) -> str:
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 1:
        return "<1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def when_label(ts: datetime.datetime, now: datetime.datetime) -> str:
    """The start in local time: the time alone today, the weekday within a week, else the date."""
    local, today = to_local(ts), to_local(now)
    if local.date() == today.date():
        return f"{local:%H:%M}"
    if (today.date() - local.date()).days < 7:
        return f"{local:%a %H:%M}"
    return f"{local:%b} {local.day} {local:%H:%M}"


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

SORT_KEYS: dict[str, Callable[[Conversation], Any]] = {
    "start": lambda c: c.start,
    "duration": lambda c: c.duration,
    "student": lambda c: c.email.lower(),
    "class": lambda c: (c.class_label.lower(), c.email.lower()),
    "course": lambda c: (c.course, c.lectures),
    "questions": lambda c: c.questions,
    "cost": lambda c: c.cost_usd,
    "flags": lambda c: c.flags,
}
DEFAULT_SORT = ("start", True)


def sort_spec(key: Optional[str], direction: Optional[str]) -> tuple[str, bool]:
    """A valid (key, descending) pair; anything else is the default, newest first."""
    if key not in SORT_KEYS:
        return DEFAULT_SORT
    if direction not in ("asc", "desc"):
        direction = "desc" if key in ("start", "duration", "questions", "cost", "flags") else "asc"
    return key, direction == "desc"


def sort_links(path: str, base: dict[str, object], key: str, desc: bool) -> dict[str, dict[str, object]]:
    """For each sort key the header link: the same filters, that key, and the
    direction flipped when it is already the active one."""
    out: dict[str, dict[str, object]] = {}
    for name in SORT_KEYS:
        active = name == key
        next_desc = (not desc) if active else sort_spec(name, None)[1]
        query = {k: v for k, v in base.items() if v not in (None, "", False)}
        query.update(sort=name, dir="desc" if next_desc else "asc")
        out[name] = {"href": f"{path}?{urllib.parse.urlencode(query)}", "active": active, "desc": desc if active else next_desc}
    return out


def sort_rows(rows: Sequence[Conversation], key: str, desc: bool) -> list[Conversation]:
    """Stable: ties keep the newest first. An unknown key is the default order."""
    if key not in SORT_KEYS:
        key, desc = DEFAULT_SORT
    newest_first = sorted(rows, key=lambda c: c.start, reverse=True)
    return sorted(newest_first, key=SORT_KEYS[key], reverse=desc)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

def clamp_days(value: Optional[str | int]) -> int:
    try:
        days = int(value) if value is not None else DEFAULT_DAYS
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return days if days in DAY_CHOICES else DEFAULT_DAYS


def recent(db: DBSession, since: datetime.datetime, until: Optional[datetime.datetime] = None, *,
           student_ids: Optional[Iterable[int]] = None, course: Optional[str] = None,
           flagged_only: bool = False, now: Optional[datetime.datetime] = None,
           limit: int = MAX_ROWS) -> Listing:
    """The conversations in the window, newest first, capped at ``limit``
    (``total`` says how many there were). ``student_ids`` narrows to a
    roster, ``course`` to one course's messages."""
    now = now or utcnow()
    until = until or now
    scope = None if student_ids is None else sorted(set(student_ids))
    if scope == []:
        return Listing([], 0, since, until)

    q = (
        db.query(Message.id, Message.student_id, Message.role, Message.created_at, Message.course,
                 Message.lecture_num, func.substr(Message.content, 1, PREVIEW_CHARS * 3))
        .filter(Message.created_at >= since, Message.created_at <= until)
    )
    if scope is not None:
        q = q.filter(Message.student_id.in_(scope))
    if course:
        q = q.filter(Message.course == course)
    messages = [Msg(*row) for row in q.order_by(Message.student_id, Message.created_at, Message.id).all()]
    rows = [build(run) for run in split(messages)]
    if not rows:
        return Listing([], 0, since, until)

    ids = sorted({c.student_id for c in rows})
    students = {s.id: s for s in db.query(Student).filter(Student.id.in_(ids)).all()}
    labels: dict[int, list[str]] = {}
    for sid, cls in (db.query(ClassEnrollment.student_id, CourseClass)
                     .join(CourseClass, CourseClass.id == ClassEnrollment.class_id)
                     .filter(ClassEnrollment.student_id.in_(ids)).order_by(CourseClass.id.desc()).all()):
        labels.setdefault(sid, []).append(cls.label)
    turns = (
        db.query(TurnEvent)
        .filter(TurnEvent.student_id.in_(ids), TurnEvent.ts >= since - TURN_SLACK,
                TurnEvent.ts <= until + TURN_SLACK)
        .all()
    )
    lowest = min(c.message_ids[0] for c in rows)
    feedback = (
        db.query(MessageFeedback)
        .filter(MessageFeedback.student_id.in_(ids), MessageFeedback.message_id >= lowest)
        .all()
    )
    first_ids: dict[int, int] = {
        int(sid): int(first) for sid, first in
        db.query(Message.student_id, func.min(Message.id))
        .filter(Message.student_id.in_(ids)).group_by(Message.student_id).all()
    }
    attach_turns(rows, turns)
    attach_feedback(rows, feedback)
    attach_first_ever(rows, first_ids)
    for c in rows:
        s = students.get(c.student_id)
        c.email = s.email if s else f"#{c.student_id}"
        c.name = (s.name or "") if s else ""
        c.classes = labels.get(c.student_id, [])
        c.active = now - c.end < ACTIVE_WITHIN
        c.when, c.ago, c.span = when_label(c.start, now), ago(c.start, now), span_words(c.start, c.end)
    if flagged_only:
        rows = [c for c in rows if c.flagged]
    rows.sort(key=lambda c: (c.start, c.first_message_id), reverse=True)
    return Listing(rows[:limit], len(rows), since, until)


def mark_new(rows: Iterable[Conversation], baseline: Optional[datetime.datetime]) -> int:
    """Flag the rows that started after ``baseline``; returns how many."""
    count = 0
    for c in rows:
        c.is_new = baseline is not None and c.start > baseline
        count += c.is_new
    return count


# ---------------------------------------------------------------------------
# One conversation's messages (the fragment and the transcript pages)
# ---------------------------------------------------------------------------

@dataclass
class ThreadMessage:
    id: int
    role: str
    content: str
    course: Optional[str]
    lecture_num: Optional[int]
    created_at: datetime.datetime
    feedback: Optional[MessageFeedback] = None


def thread(db: DBSession, student_id: int, start: Optional[datetime.datetime] = None,
           end: Optional[datetime.datetime] = None, course: Optional[str] = None,
           limit: Optional[int] = None) -> list[ThreadMessage]:
    """The student's messages between ``start`` and ``end`` (both inclusive;
    either may be open), oldest first, with each reply's thumbs. ``limit``
    keeps the newest that many, as the admin transcript always did."""
    q = db.query(Message).filter(Message.student_id == student_id)
    if start is not None:
        q = q.filter(Message.created_at >= start)
    if end is not None:
        q = q.filter(Message.created_at <= end)
    if course:
        q = q.filter(Message.course == course)
    if limit:
        rows = list(reversed(q.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit).all()))
    else:
        rows = q.order_by(Message.created_at.asc(), Message.id.asc()).all()
    fb = {
        f.message_id: f for f in
        db.query(MessageFeedback).filter(MessageFeedback.message_id.in_([m.id for m in rows] or [0])).all()
    }
    return [ThreadMessage(m.id, m.role, m.content, m.course, m.lecture_num, m.created_at, fb.get(m.id))
            for m in rows]


def thread_around(db: DBSession, student_id: int, message_id: int, course: Optional[str] = None,
                  before: int = 150, after: int = 150) -> list[ThreadMessage]:
    """The messages around one of the student's (by id order): the one a
    flagged-reply link lands on, whatever its age, instead of the newest
    300 with the anchor possibly off the page (review step 0.4, F18)."""
    base = db.query(Message).filter(Message.student_id == student_id)
    if course:
        base = base.filter(Message.course == course)
    earlier = base.filter(Message.id <= message_id).order_by(Message.id.desc()).limit(before + 1).all()
    later = base.filter(Message.id > message_id).order_by(Message.id.asc()).limit(after).all()
    rows = list(reversed(earlier)) + later
    fb = {
        f.message_id: f for f in
        db.query(MessageFeedback).filter(MessageFeedback.message_id.in_([m.id for m in rows] or [0])).all()
    }
    return [ThreadMessage(m.id, m.role, m.content, m.course, m.lecture_num, m.created_at, fb.get(m.id))
            for m in rows]


def parse_ts(value: Optional[str]) -> Optional[datetime.datetime]:
    """A naive UTC instant from a query parameter, or None."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed


# ---------------------------------------------------------------------------
# Newer than your previous visit (a cookie, no table)
# ---------------------------------------------------------------------------

@dataclass
class Seen:
    baseline: Optional[datetime.datetime]   # rows after this are new
    cookie: Optional[str]                   # the value to set, or None to leave it


def seen_state(cookie_value: Optional[str], now: datetime.datetime) -> Seen:
    """The cookie holds ``<previous visit>|<this sitting's start>``. Within
    ``SEEN_BURST`` of the sitting's start nothing changes, so sorting and
    filtering keep their highlights; after a longer silence the sitting
    becomes the previous visit and a new one starts."""
    baseline: Optional[datetime.datetime] = None
    sitting: Optional[datetime.datetime] = None
    if cookie_value and "|" in cookie_value:
        first, second = cookie_value.split("|", 1)
        baseline, sitting = parse_ts(first), parse_ts(second)
    if sitting is not None and now - sitting < SEEN_BURST:
        return Seen(baseline, None)
    new_baseline = sitting if sitting is not None else baseline
    value = f"{new_baseline.isoformat(timespec='seconds') if new_baseline else ''}|{now.isoformat(timespec='seconds')}"
    return Seen(new_baseline, value)
