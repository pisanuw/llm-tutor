"""Per-class engagement: how a roster is using (or not using) the tutor.

One computation, three consumers: the instructor dashboard renders it, the CSV
and JSON exports dump it, and the weekly email summarises it. Keeping it in
one place means the number an instructor reads on the page is the number in
their inbox.

Course scoping is deliberate and per source:

* messages / replies / lectures  — ``Message.course`` matches the class course.
* goals, exams, reviews          — the goal's ``LearningGoal.course`` matches.
* exercises                      — bank exercise ids are ``"<course>-L.."``
  (curriculum/exercises.json), so the prefix scopes them. An ad-hoc code
  submission with no bank id is not attributed to a course and is left out of
  the per-course counts rather than guessed at.
* logins                         — not course-scoped; a login is a login.

The tutor is not a grading tool, so nothing here produces a grade. The mastery
tiers carry the same meaning as the daily report (pedagogy item 9):
``confirmed`` means retrievals spanning distinct days, ``mastered`` is the
quiz tier and is reported as provisional.
"""

from __future__ import annotations

import collections
import datetime
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from tutor.db.models import (
    CourseClass,
    ExerciseAttempt,
    GradedEvent,
    LearningGoal,
    MasteryAttempt,
    Message,
    MessageFeedback,
    Student,
    StudentProgress,
)
from tutor.agent.state import review_is_due
from tutor.utctime import utcnow

# The status a roster row shows, from questions, the browser's events,
# logins and sign-in failures together (step 0.9, F38): asked this window;
# came and read without asking; could not sign in and never got in after;
# asked before but not this window; signed in but never asked anything;
# never signed in.
STATUS_ACTIVE = "active"
STATUS_READING = "reading"
STATUS_LOCKED_OUT = "locked_out"
STATUS_DORMANT = "dormant"
STATUS_SIGNED_IN = "signed_in"
STATUS_NEVER = "never"
STATUS_LABELS = {
    STATUS_ACTIVE: "active",
    STATUS_READING: "reading only",
    STATUS_LOCKED_OUT: "could not sign in",
    STATUS_DORMANT: "went quiet",
    STATUS_SIGNED_IN: "signed in, never asked",
    STATUS_NEVER: "never signed in",
}
# Statuses worth an instructor's glance, in the order the lists show them.
ATTENTION_STATUSES = (STATUS_LOCKED_OUT, STATUS_DORMANT, STATUS_SIGNED_IN, STATUS_NEVER)


@dataclass
class StudentEngagement:
    """One student's use of the tutor in one class over one window."""

    student_id: int
    email: str
    name: str = ""
    # Usage
    status: str = STATUS_NEVER
    login_count: int = 0
    last_login: Optional[datetime.datetime] = None
    last_active: Optional[datetime.datetime] = None   # newest message, any window
    messages: int = 0          # student messages in the window, this course
    replies: int = 0           # tutor replies in the window, this course
    active_days: int = 0       # distinct UTC days with a message in the window
    # Learning
    goals_seen: int = 0
    goals_mastered: int = 0
    goals_confirmed: int = 0
    exercises: int = 0
    avg_score: Optional[float] = None
    exams_passed: int = 0
    exams_failed: int = 0
    reviews_due: int = 0
    thumbs_down: int = 0
    # Sign-in failures in the window and whether a sign-in followed the last
    # one (review step 0.6, F35: the instructor is who the student asks).
    login_failures: int = 0
    login_failed_recently: bool = False
    # Any browser activity in the window (a page opened, a deck read), from
    # activity_events; and whether the row is an admin or instructor, who
    # stays out of every baseline (step 0.9).
    site_activity: bool = False
    is_staff: bool = False

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def needs_attention(self) -> bool:
        """Worth an instructor's glance: locked out, gone quiet, never asked, never came."""
        return self.status in ATTENTION_STATUSES

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass
class ClassSummary:
    """The class-level roll-up shown above the roster and in the email."""

    class_id: int
    name: str
    course: str
    term: str = ""
    since: Optional[datetime.datetime] = None
    until: Optional[datetime.datetime] = None
    students: int = 0
    active: int = 0
    reading: int = 0
    locked_out: int = 0
    dormant: int = 0
    signed_in: int = 0
    never: int = 0
    messages: int = 0
    replies: int = 0
    exercises: int = 0
    avg_score: Optional[float] = None
    exams_passed: int = 0
    exams_failed: int = 0
    goals_mastered: int = 0
    goals_confirmed: int = 0
    thumbs_down: int = 0
    median_messages: float = 0.0
    top_lectures: list[tuple[int, int]] = field(default_factory=list)  # (lecture, messages)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.term})" if self.term else self.name

    @property
    def participation(self) -> Optional[float]:
        """Share of the roster active in the window, or None for an empty class."""
        return (self.active / self.students) if self.students else None


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def course_goal_ids(db: DBSession, course: str) -> set[str]:
    return {r[0] for r in db.query(LearningGoal.id).filter_by(course=course).all()}


def compute_engagement(
    db: DBSession,
    course_class: CourseClass,
    roster: list[Student],
    since: datetime.datetime,
    until: datetime.datetime,
    now: Optional[datetime.datetime] = None,
) -> list[StudentEngagement]:
    """One :class:`StudentEngagement` per roster member, least active first.

    Least active first is on purpose: the students an instructor needs to act
    on are the ones at the top of the page, not buried under the keen ones.
    """
    from tutor.auth.access import is_staff

    now = now or utcnow()
    course = course_class.course
    ids = [s.id for s in roster]
    rows = {
        s.id: StudentEngagement(
            student_id=s.id, email=s.email, name=s.name or "",
            login_count=s.login_count or 0, last_login=s.last_login, is_staff=is_staff(s),
        )
        for s in roster
    }
    if not ids:
        return []

    # --- any browser activity in the window (step 0.9: "reading only") -----
    from tutor.db.models import ActivityEvent

    for (sid,) in (
        db.query(ActivityEvent.student_id)
        .filter(ActivityEvent.student_id.in_(ids), ActivityEvent.ts >= since, ActivityEvent.ts < until)
        .distinct()
        .all()
    ):
        if sid in rows:
            rows[sid].site_activity = True

    goal_ids = course_goal_ids(db, course)

    # --- sign-in failures in the window, by address (review step 0.6) -------
    from tutor.auth import attempts as attempts_mod
    from tutor.db.models import LoginAttempt

    from tutor.request_errors import BROWSER, classify_agent

    by_email = {s.email.lower(): s.id for s in roster}
    last_failure: dict[int, datetime.datetime] = {}
    last_ok: dict[int, datetime.datetime] = {}
    for email, outcome, detail, agent, ts in (
        db.query(LoginAttempt.email, LoginAttempt.outcome, LoginAttempt.detail, LoginAttempt.user_agent,
                 LoginAttempt.created_at)
        .filter(LoginAttempt.email.in_(list(by_email) or ["-"]),
                LoginAttempt.created_at >= since, LoginAttempt.created_at < until)
        .all()
    ):
        sid = by_email.get((email or "").lower())
        if sid is None or ts is None:
            continue
        if outcome in attempts_mod.FAILED:
            # A mail scanner's tap and a stranger's typing are not the
            # student's failures (the same rule as the alert and the rollup).
            if classify_agent(agent) != BROWSER or not attempts_mod.attributable(outcome, detail):
                continue
            rows[sid].login_failures += 1
            last_failure[sid] = max(last_failure.get(sid, ts), ts)
        elif outcome == attempts_mod.OK:
            last_ok[sid] = max(last_ok.get(sid, ts), ts)
    for sid, failed_at in last_failure.items():
        rows[sid].login_failed_recently = last_ok.get(sid) is None or last_ok[sid] < failed_at

    # --- messages in window (course-scoped) --------------------------------
    for sid, role, ts in (
        db.query(Message.student_id, Message.role, Message.created_at)
        .filter(Message.student_id.in_(ids), Message.course == course,
                Message.created_at >= since, Message.created_at < until)
        .all()
    ):
        row = rows.get(sid)
        if row is None:
            continue
        if role == "user":
            row.messages += 1
        else:
            row.replies += 1

    # Distinct active days, from student messages only.
    days: dict[int, set] = collections.defaultdict(set)
    for sid, ts in (
        db.query(Message.student_id, Message.created_at)
        .filter(Message.student_id.in_(ids), Message.course == course,
                Message.role == "user",
                Message.created_at >= since, Message.created_at < until)
        .all()
    ):
        if ts is not None:
            days[sid].add(ts.date())
    for sid, dayset in days.items():
        if sid in rows:
            rows[sid].active_days = len(dayset)

    # --- last activity ever (not window-bound), for the dormancy call ------
    for sid, last_ts in (
        db.query(Message.student_id, func.max(Message.created_at))
        .filter(Message.student_id.in_(ids), Message.course == course)
        .group_by(Message.student_id)
        .all()
    ):
        if sid in rows:
            rows[sid].last_active = last_ts

    # --- progress (cumulative, course-scoped) ------------------------------
    if goal_ids:
        for sid, status, seen, mastered_at, confirmed_at, fsrs_due in (
            db.query(StudentProgress.student_id, StudentProgress.status,
                     StudentProgress.seen, StudentProgress.mastered_at,
                     StudentProgress.confirmed_at, StudentProgress.fsrs_due)
            .filter(StudentProgress.student_id.in_(ids),
                    StudentProgress.goal_id.in_(goal_ids))
            .all()
        ):
            row = rows.get(sid)
            if row is None:
                continue
            if seen or status in ("unlocked", "mastered"):
                row.goals_seen += 1
            if mastered_at is not None or status == "mastered":
                row.goals_mastered += 1
            if confirmed_at is not None:
                row.goals_confirmed += 1
            if review_is_due(fsrs_due, now):          # the one review clock (B20)
                row.reviews_due += 1

        # --- exams in window -----------------------------------------------
        for sid, status in (
            db.query(MasteryAttempt.student_id, MasteryAttempt.status)
            .filter(MasteryAttempt.student_id.in_(ids),
                    MasteryAttempt.goal_id.in_(goal_ids),
                    MasteryAttempt.completed_at.isnot(None),
                    MasteryAttempt.completed_at >= since,
                    MasteryAttempt.completed_at < until)
            .all()
        ):
            row = rows.get(sid)
            if row is None:
                continue
            if status == "passed":
                row.exams_passed += 1
            elif status == "failed":
                row.exams_failed += 1

    # --- exercises in window (bank ids carry the course prefix) ------------
    scores: dict[int, list[float]] = collections.defaultdict(list)
    for sid, score in (
        db.query(ExerciseAttempt.student_id, ExerciseAttempt.score)
        .filter(ExerciseAttempt.student_id.in_(ids),
                ExerciseAttempt.exercise_id.like(f"{course}-%"),
                ExerciseAttempt.created_at >= since,
                ExerciseAttempt.created_at < until)
        .all()
    ):
        row = rows.get(sid)
        if row is None:
            continue
        row.exercises += 1
        if score is not None:
            scores[sid].append(float(score))
    for sid, vals in scores.items():
        if vals and sid in rows:
            rows[sid].avg_score = round(sum(vals) / len(vals), 2)

    # --- flagged replies in window (a signal the tutor is not landing) -----
    for sid, n in (
        db.query(MessageFeedback.student_id, func.count(MessageFeedback.id))
        .join(Message, Message.id == MessageFeedback.message_id)
        .filter(MessageFeedback.student_id.in_(ids),
                MessageFeedback.rating < 0,
                Message.course == course,
                MessageFeedback.updated_at >= since,
                MessageFeedback.updated_at < until)
        .group_by(MessageFeedback.student_id)
        .all()
    ):
        if sid in rows:
            rows[sid].thumbs_down = n

    for row in rows.values():
        row.status = _status(row)

    out = list(rows.values())
    out.sort(key=lambda r: (r.messages, r.active_days, r.login_count, r.email.lower()))
    return out


def _status(row: StudentEngagement) -> str:
    if row.messages or row.replies or row.active_days:
        return STATUS_ACTIVE
    if row.site_activity:
        return STATUS_READING
    if row.login_failed_recently:
        return STATUS_LOCKED_OUT
    if row.last_active is not None:
        return STATUS_DORMANT
    if row.login_count:
        return STATUS_SIGNED_IN
    return STATUS_NEVER


def summarize(
    course_class: CourseClass,
    rows: list[StudentEngagement],
    since: datetime.datetime,
    until: datetime.datetime,
    top_lectures: Optional[list[tuple[int, int]]] = None,
) -> ClassSummary:
    """Fold per-student rows into the class roll-up. Pure."""
    scored = [r.avg_score for r in rows if r.avg_score is not None]
    return ClassSummary(
        class_id=course_class.id,
        name=course_class.name,
        course=course_class.course,
        term=course_class.term,
        since=since,
        until=until,
        students=len(rows),
        active=sum(1 for r in rows if r.status == STATUS_ACTIVE),
        reading=sum(1 for r in rows if r.status == STATUS_READING),
        locked_out=sum(1 for r in rows if r.status == STATUS_LOCKED_OUT),
        dormant=sum(1 for r in rows if r.status == STATUS_DORMANT),
        signed_in=sum(1 for r in rows if r.status == STATUS_SIGNED_IN),
        never=sum(1 for r in rows if r.status == STATUS_NEVER),
        messages=sum(r.messages for r in rows),
        replies=sum(r.replies for r in rows),
        exercises=sum(r.exercises for r in rows),
        avg_score=round(sum(scored) / len(scored), 2) if scored else None,
        exams_passed=sum(r.exams_passed for r in rows),
        exams_failed=sum(r.exams_failed for r in rows),
        goals_mastered=sum(r.goals_mastered for r in rows),
        goals_confirmed=sum(r.goals_confirmed for r in rows),
        thumbs_down=sum(r.thumbs_down for r in rows),
        median_messages=_median([r.messages for r in rows if not r.is_staff]),
        top_lectures=top_lectures or [],
    )


# ---------------------------------------------------------------------------
# Where each student stands within the class: a percentile, not a judgement
# (step 0.9; decision 8's metrics). The population is the roster minus staff,
# and nothing is ranked in a class under RANK_MIN_STUDENTS.
# ---------------------------------------------------------------------------

PERCENTILE_METRICS: tuple[tuple[str, str], ...] = (
    ("messages", "questions"),
    ("active_days", "days used"),
    ("active_min", "on screen (min)"),
    ("deck_min", "deck (min)"),
    ("narration_min", "narration (min)"),
    ("visits", "visits"),
)
RANK_MIN_STUDENTS = 5


@dataclass
class Standing:
    student_id: int
    email: str
    name: str = ""
    is_staff: bool = False
    values: dict[str, float] = field(default_factory=dict)
    percentiles: dict[str, Optional[float]] = field(default_factory=dict)   # None for staff

    def cell(self, key: str) -> str:
        """``value (pNN)`` for a table, the value alone for staff."""
        v = self.values.get(key, 0)
        text = f"{v:g}"
        pct = self.percentiles.get(key)
        return text if pct is None else f"{text} (p{round(pct)})"


def _percentile_rank(value: float, population: list[float]) -> Optional[float]:
    """The share of the population at or below ``value`` (0 to 100)."""
    if not population:
        return None
    return 100.0 * sum(1 for v in population if v <= value) / len(population)


def standings(rows: list[StudentEngagement], activity_rows: dict) -> list[Standing]:
    """One :class:`Standing` per roster row, by name, each metric's
    percentile among the non-staff rows; ``[]`` for a class with fewer than
    :data:`RANK_MIN_STUDENTS` of them. ``activity_rows`` is
    ``ClassActivity.rows`` (student id to its activity engagement)."""
    population = [r for r in rows if not r.is_staff]
    if len(population) < RANK_MIN_STUDENTS:
        return []

    def value(r: StudentEngagement, key: str) -> float:
        if key in ("messages", "active_days"):
            return float(getattr(r, key))
        a = activity_rows.get(r.student_id)
        return float(getattr(a, key, 0) or 0) if a is not None else 0.0

    pools = {key: [value(r, key) for r in population] for key, _ in PERCENTILE_METRICS}
    out = []
    for r in rows:
        st = Standing(student_id=r.student_id, email=r.email, name=r.name, is_staff=r.is_staff)
        for key, _ in PERCENTILE_METRICS:
            st.values[key] = value(r, key)
            st.percentiles[key] = None if r.is_staff else _percentile_rank(st.values[key], pools[key])
        out.append(st)
    out.sort(key=lambda st: ((st.name or st.email).lower(), st.email.lower()))
    return out


def top_lectures(
    db: DBSession,
    course_class: CourseClass,
    student_ids: list[int],
    since: datetime.datetime,
    until: datetime.datetime,
    n: int = 3,
) -> list[tuple[int, int]]:
    """The lectures the class asked about most in the window: ``[(lecture, messages)]``."""
    if not student_ids:
        return []
    counts = (
        db.query(Message.lecture_num, func.count(Message.id))
        .filter(Message.student_id.in_(student_ids),
                Message.course == course_class.course,
                Message.role == "user",
                Message.lecture_num.isnot(None),
                Message.created_at >= since, Message.created_at < until)
        .group_by(Message.lecture_num)
        .all()
    )
    ranked = sorted(((int(lec), int(c)) for lec, c in counts if lec),
                    key=lambda t: (-t[1], t[0]))
    return ranked[:n]


def class_report(
    db: DBSession,
    course_class: CourseClass,
    roster: list[Student],
    since: datetime.datetime,
    until: datetime.datetime,
    now: Optional[datetime.datetime] = None,
) -> tuple[ClassSummary, list[StudentEngagement]]:
    """The whole picture for one class: ``(summary, rows)``."""
    rows = compute_engagement(db, course_class, roster, since, until, now=now)
    lectures = top_lectures(db, course_class, [s.id for s in roster], since, until)
    return summarize(course_class, rows, since, until, lectures), rows


def confirmed_goal_titles(
    db: DBSession, course: str, student_ids: list[int],
    since: datetime.datetime, until: datetime.datetime, n: int = 5,
) -> list[tuple[str, int]]:
    """Goals the class confirmed (spaced evidence) in the window, most first."""
    if not student_ids:
        return []
    rows = (
        db.query(LearningGoal.title, func.count(StudentProgress.id))
        .join(StudentProgress, StudentProgress.goal_id == LearningGoal.id)
        .filter(StudentProgress.student_id.in_(student_ids),
                LearningGoal.course == course,
                StudentProgress.confirmed_at.isnot(None),
                StudentProgress.confirmed_at >= since,
                StudentProgress.confirmed_at < until)
        .group_by(LearningGoal.title)
        .all()
    )
    return sorted(((t, int(c)) for t, c in rows), key=lambda x: (-x[1], x[0]))[:n]


def recent_graded(
    db: DBSession, course: str, student_ids: list[int],
    since: datetime.datetime, until: datetime.datetime,
) -> tuple[int, Optional[float]]:
    """``(n, mean_success)`` over graded events in the window for this course."""
    if not student_ids:
        return 0, None
    rows = (
        db.query(GradedEvent.success)
        .join(LearningGoal, LearningGoal.id == GradedEvent.goal_id)
        .filter(GradedEvent.student_id.in_(student_ids),
                LearningGoal.course == course,
                GradedEvent.ts >= since, GradedEvent.ts < until)
        .all()
    )
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if not vals:
        return 0, None
    return len(vals), round(sum(vals) / len(vals), 2)
