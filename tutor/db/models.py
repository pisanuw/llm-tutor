"""SQLAlchemy ORM models for the tutor."""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Any, List, Optional

from sqlalchemy import (
    Date,
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
    create_engine,
)
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from tutor.config import get_config
from tutor.utctime import utcnow
from tutor.courses import DEFAULT_COURSE


def safe_json_loads(raw: Optional[str], default: Any = None) -> Any:
    """Parse a JSON text column, returning ``default`` on NULL/empty/malformed.

    The anchor/track JSON columns are written by offline pipeline scripts, so a
    single malformed value should degrade that one field, not 500 the whole
    page/endpoint that renders many rows.
    """
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


class Base(DeclarativeBase):
    pass


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )
    last_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    # Login tracking (admin observability): bumped on every successful session
    # creation in auth._set_session_cookie. last_login is NULL until the first
    # login; the per-login audit trail (with method) lives in the sessions table.
    last_login: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Retention: when the 1-year sweep de-identified this student (PII + free text
    # stripped, de-identified telemetry kept under this row as a pseudonym). NULL
    # = still identifiable. See docs/retention-policy.md + state.anonymize_student_data.
    anonymized_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    # Conduct restriction (Session 10, S24): set by the conduct ladder's
    # second strike, caps the envelope at H1 until the instructor clears it
    # (admin button in Phase F; scripts/clear_restriction.py until then).
    restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Instructor role. Granted at runtime by an admin, so it cannot live in
    # ADMIN_EMAILS. An instructor sees /instructor for the classes they own;
    # admins are treated as instructors everywhere.
    # Orthogonal to admin: a student row can be both, neither, or either.
    is_instructor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The instructor's own switch for the weekly email (/instructor, "Weekly
    # email"). Default on; an admin's per-class weekly_report is the other gate.
    weekly_email_opt_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    magic_links: Mapped[List[MagicLink]] = relationship(back_populates="student", cascade="all, delete-orphan")
    sessions: Mapped[List[Session]] = relationship(back_populates="student", cascade="all, delete-orphan")
    messages: Mapped[List[Message]] = relationship(back_populates="student")
    exercises: Mapped[List[ExerciseAttempt]] = relationship(back_populates="student")
    topics: Mapped[List[TopicAccess]] = relationship(back_populates="student")
    progress: Mapped[List[StudentProgress]] = relationship(back_populates="student")
    mastery_attempts: Mapped[List[MasteryAttempt]] = relationship(back_populates="student", cascade="all, delete-orphan")
    paths: Mapped[List[StudentPath]] = relationship(back_populates="student")
    path_events: Mapped[List["PathEvent"]] = relationship(back_populates="student")
    taught_classes: Mapped[List["CourseClass"]] = relationship(back_populates="instructor")
    enrollments: Mapped[List["ClassEnrollment"]] = relationship(back_populates="student")
    activity_events: Mapped[List["ActivityEvent"]] = relationship(back_populates="student")


class MagicLink(Base):
    __tablename__ = "magic_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Set when the link is used. A link carries the email it was sent to, and
    # the students row is created only by a successful login (plan step 2.9,
    # finding C21): requesting a link no longer creates one.
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("students.id"), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    # The 6-digit code the same email carries (2026-10-02). Some university
    # mail systems rewrite every link through a URL scanner that opens it on
    # delivery, so the code, typed on the login page, is the sign-in that no
    # scanner can spend; the link still works through a confirmation click.
    # ``code_attempts`` counts wrong guesses against this row; auth refuses
    # the row after a few.
    code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    code_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )

    student: Mapped[Optional[Student]] = relationship(back_populates="magic_links")


class LoginAttempt(Base):
    """One row per step of a sign-in, successful or not (the admin Logins tab).

    ``sessions`` records only the logins that succeeded, and the process log
    carries no address and no user agent, so a student who "cannot log in"
    left no trail an admin could read (2026-10-02). Each row names the method
    (``google`` or ``magic``), the outcome (the constants in
    tutor.auth.attempts), the host the request arrived on, the browser that
    made it, and a short detail: the next path or an exception class, never a
    token or a code. The user agent is what tells a student's browser from the
    mail scanner that opened their link first.

    ``email`` is set as soon as it is known (a Google callback that fails its
    state check has none); ``student_id`` once the row exists. Both make the
    table part of the erasure scope (state.erasure_scope), and the retention
    sweep prunes rows older than its window.
    """

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("students.id"), nullable=True)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False)
    jwt_jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # How the login happened: "google" | "magic" | "dev". Nullable: legacy rows
    # predate the column. One sessions row per login = the login audit trail.
    method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    student: Mapped[Student] = relationship(back_populates="sessions")


class Message(Base):
    __tablename__ = "messages"
    # History, the voice and feedback lookups, class metrics, and admin counts
    # all filter on the student (plan step 2.8, finding C7).
    __table_args__ = (
        Index("ix_messages_student_course_created", "student_id", "course", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    lecture_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slide_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Course the message belongs to ("AST101"/"AST102"); history is filtered by it.
    # Nullable: legacy rows predate the column (Session 7).
    course: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # Compact JSON note of the tools the assistant used this turn (tool name,
    # query, top source, slide shown) so the actor remembers what it showed.
    tool_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )
    # The turn this message belongs to (turn_events.turn_id), so a flagged
    # reply reaches its ledger row without a join by time (review step 0.4).
    # Nullable: rows from before 2026-10-07, and replies saved outside a turn.
    turn_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)

    student: Mapped[Student] = relationship(back_populates="messages")


class MessageFeedback(Base):
    """A student's thumbs up/down on one tutor reply, with an optional reason.

    One row per assistant message (``message_id`` is unique); re-rating
    overwrites in place, so the table holds the student's latest opinion, not a
    click log. ``rating`` is +1 (up) or -1 (down). ``comment`` is the free-text
    answer to "what was wrong?", asked only after a thumbs down. Written only by
    POST /chat/feedback; read by the admin Conversations tab, the transcript
    export, and scripts/feedback_report.py (the daily thumbs-down email).
    """

    __tablename__ = "message_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id"), nullable=False, unique=True, index=True
    )
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # +1 or -1
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # When the instructor marked the flag seen (Admin → Today); a new rating
    # clears it (review step 0.4).
    seen_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    message: Mapped[Message] = relationship()


class FeedbackEvent(Base):
    """Every rating a student gave, append-only (review step 0.4, F22):
    ``message_feedback`` keeps the latest opinion, this keeps the history,
    so a thumbs-up after a thumbs-down no longer erases the complaint.
    Written only by ``state.record_message_feedback``."""

    __tablename__ = "feedback_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), nullable=False, index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # +1 or -1
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class SessionSummary(Base):
    """Rolling per-course conversation summary (Session 7 memory).

    One row per (student, course). ``last_message_id`` marks how far the
    summary covers; history sent to the actor is summary + the last few
    verbatim messages.
    """

    __tablename__ = "session_summaries"
    __table_args__ = (UniqueConstraint("student_id", "course", name="uq_session_summary"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    course: Mapped[str] = mapped_column(String(8), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_message_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )


class ExerciseAttempt(Base):
    __tablename__ = "exercise_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True,   # plan step 2.8, C7
    )
    exercise_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lecture_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    submission: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0.0 – 4.0
    feedback: Mapped[str] = mapped_column(Text, nullable=False, default="")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )

    student: Mapped[Student] = relationship(back_populates="exercises")


class WhitelistEntry(Base):
    """An email allowed to log in when the whitelist is enabled."""

    __tablename__ = "whitelist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    added_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )


class CourseClass(Base):
    """One instructor's class: a named roster of students in one course.

    Named ``CourseClass`` because ``class`` is a Python keyword; the UI calls it
    a class. Created by an admin, who also sets the instructor and the roster
    (see tutor/classes.py — the only module that writes these rows). The
    ``course`` field ("AST101"/"AST102") scopes every progress metric the
    instructor dashboard shows, so an AST102 class never reports AST101 goals.

    ``instructor_id`` is the class's lead instructor, the one it was created
    for; every instructor of the class, the lead included, has a
    ``class_instructors`` row (ClassInstructor), and that table is what the
    scoping rules read. A co-instructor sees the class on /instructor and
    gets its weekly email; removing the lead promotes the longest-standing
    co-instructor. ``archived`` hides a finished quarter without deleting its
    history; ``weekly_report`` is the per-class switch for the Monday email.
    """

    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    course: Mapped[str] = mapped_column(String(8), nullable=False)   # "AST101" | "AST102"
    term: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    instructor_id: Mapped[int] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weekly_report: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The quarter the class runs, entered when it is created: the weekly email
    # goes out only on days inside it (inclusive). NULL on both means no limit.
    quarter_start: Mapped[Optional[datetime.date]] = mapped_column(Date, nullable=True)
    quarter_end: Mapped[Optional[datetime.date]] = mapped_column(Date, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    instructor: Mapped[Student] = relationship(back_populates="taught_classes")

    def in_quarter(self, day: datetime.date) -> bool:
        """Whether ``day`` falls inside the class's quarter (an unset end or
        start is open)."""
        if self.quarter_start and day < self.quarter_start:
            return False
        if self.quarter_end and day > self.quarter_end:
            return False
        return True
    enrollments: Mapped[List["ClassEnrollment"]] = relationship(
        back_populates="course_class", cascade="all, delete-orphan"
    )
    instructors: Mapped[List["ClassInstructor"]] = relationship(
        back_populates="course_class", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        """Display name including the term, e.g. "AST 101 A (Autumn 2026)"."""
        return f"{self.name} ({self.term})" if self.term else self.name


class ClassEnrollment(Base):
    """One student's membership in one class.

    Plain many-to-many: a student can be in several classes, including across
    courses (AST101 and AST102 in the same quarter). Unique on (class,
    student) so a double add is a no-op rather than a duplicate roster row.
    """

    __tablename__ = "class_enrollments"
    __table_args__ = (UniqueConstraint("class_id", "student_id", name="uq_class_enrollment"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(
        ForeignKey("classes.id"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    course_class: Mapped[CourseClass] = relationship(back_populates="enrollments")
    student: Mapped[Student] = relationship(back_populates="enrollments")


class ClassInstructor(Base):
    """One instructor of one class (2026-10-06; before, a class had exactly
    one, ``classes.instructor_id``, and a co-taught section was two classes).

    The lead instructor has a row too, so "the instructors of a class" and
    "the classes of an instructor" are one join (tutor/classes.py keeps the
    two in step: creating a class writes both, and removing the lead moves
    ``instructor_id`` to the longest-standing remaining row). ``added_by`` is
    the admin or instructor who added them. Unique on (class, student).
    """

    __tablename__ = "class_instructors"
    __table_args__ = (UniqueConstraint("class_id", "student_id", name="uq_class_instructor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(
        ForeignKey("classes.id"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    added_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    course_class: Mapped[CourseClass] = relationship(back_populates="instructors")
    student: Mapped[Student] = relationship()


class Setting(Base):
    """Generic key/value store for runtime-editable app settings."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False, default="")


class LearningGoal(Base):
    """A single learnable skill extracted from the curriculum plan."""

    __tablename__ = "learning_goals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # slug e.g. "AST101-L02-explain-moon-phases"
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    course: Mapped[str] = mapped_column(String(8), nullable=False)   # "AST101" or "AST102"
    lecture_num: Mapped[int] = mapped_column(Integer, nullable=False)
    slide_anchor: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON {"lecture":N,"slide":M,"title":"..."}
    book_anchor: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON {"chapter":"...","section_id":"...","title":"..."}
    hook: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # one-line "why this matters"
    # A "threshold" concept worth a guided interactive explorable (Engram P4).
    # An explorable is served when explorables/<goal_id>.html exists; this flag
    # marks which goals are worth authoring one for.
    threshold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    prerequisites: Mapped[List[GoalPrerequisite]] = relationship(
        "GoalPrerequisite", foreign_keys="GoalPrerequisite.to_goal_id", back_populates="to_goal"
    )
    dependents: Mapped[List[GoalPrerequisite]] = relationship(
        "GoalPrerequisite", foreign_keys="GoalPrerequisite.from_goal_id", back_populates="from_goal"
    )
    progress_records: Mapped[List[StudentProgress]] = relationship(back_populates="goal")

    def slide_anchor_data(self) -> Optional[dict]:
        """Parsed slide_anchor JSON, or None when absent/malformed."""
        return safe_json_loads(self.slide_anchor)

    def book_anchor_data(self) -> Optional[dict]:
        """Parsed book_anchor JSON, or None when absent/malformed."""
        return safe_json_loads(self.book_anchor)


class GoalPrerequisite(Base):
    """Directed edge: from_goal_id must be mastered before to_goal_id."""

    __tablename__ = "goal_prerequisites"
    __table_args__ = (UniqueConstraint("from_goal_id", "to_goal_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_goal_id: Mapped[str] = mapped_column(ForeignKey("learning_goals.id"), nullable=False)
    to_goal_id: Mapped[str] = mapped_column(ForeignKey("learning_goals.id"), nullable=False)

    from_goal: Mapped[LearningGoal] = relationship(
        "LearningGoal", foreign_keys=[from_goal_id], back_populates="dependents"
    )
    to_goal: Mapped[LearningGoal] = relationship(
        "LearningGoal", foreign_keys=[to_goal_id], back_populates="prerequisites"
    )


class StudentProgress(Base):
    """Tracks a student's status on a specific learning goal."""

    __tablename__ = "student_progress"
    __table_args__ = (UniqueConstraint("student_id", "goal_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False)
    goal_id: Mapped[str] = mapped_column(ForeignKey("learning_goals.id"), nullable=False)
    # "locked" | "unlocked" | "mastered"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="locked")
    # Windowed success rate over recent graded events (Task 5). NULL until the
    # student has any graded event for this goal. Prod Postgres needs a one-time
    # ALTER TABLE student_progress ADD COLUMN mastery_score FLOAT (no Alembic).
    mastery_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # When real graded evidence last arrived (Session 7, S19): the decay
    # clock reads THIS, not updated_at, so status flips and other bookkeeping
    # writes stop resetting decay. Nullable: legacy rows fall back to
    # updated_at.
    last_evidence_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    # Evidence-based mastery (pedagogy item 9). mastered_at: first time the
    # status reached 'mastered' (the progression tier: a quiz pass still unlocks
    # the path). confirmed_at: set only when two successful retrievals are at
    # least state.CONFIRM_MIN_GAP_HOURS apart (the evidence tier: what the
    # report and the agent treat as real mastery). Permanent once set.
    mastered_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    evidence_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # "chat" | "exercise"
    # Session 17 (S9): True when the student has discussed this goal in chat
    # (lecture accessed). Distinct from "unlocked" (prerequisites met).
    seen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # FSRS-4.5 spaced-repetition state (Engram integration, Phase 1). Maintained
    # by state.record_graded_event / record_review_rating from the student's own
    # rating history; drives the retrievability-based decay in envelope.py and
    # the /review due queue. NULL until the goal's first graded event. Prod
    # Postgres needs the ALTER TABLEs in startup._ADDED_COLUMNS (no Alembic).
    fsrs_stability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fsrs_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fsrs_due: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    fsrs_reps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fsrs_lapses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    student: Mapped[Student] = relationship(back_populates="progress")
    goal: Mapped[LearningGoal] = relationship(back_populates="progress_records")


class Misconception(Base):
    """A durable record of a wrong mental model a student holds about a goal.

    Engram integration, Phase 3. Written ONLY through tutor/agent/state.py.
    Created when a high-confidence answer is graded wrong (confident-but-wrong is
    a misconception, not a gap); resolved (``resolved_at`` set) when the student
    later demonstrates the goal. Unresolved rows for the goals in scope are fed
    into the per-turn contract so the actor can address the specific observed
    wrong model instead of generic review. At most one unresolved row per
    (student, goal) at a time (state.record_misconception upserts).
    """

    __tablename__ = "misconceptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("learning_goals.id"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


class GradedEvent(Base):
    """One graded outcome for a (student, goal): exam item, exercise, or test.

    Written ONLY through tutor/agent/state.py. The windowed mean of recent
    rows is the student's mastery_score for the goal (Task 5).
    """

    __tablename__ = "graded_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("learning_goals.id"), nullable=False, index=True)
    success: Mapped[float] = mapped_column(Float, nullable=False)  # 0.0 .. 1.0
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")  # exam | exercise | test
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MasteryAttempt(Base):
    """Tracks a student's mini-mastery exam attempt for a specific learning goal."""

    __tablename__ = "mastery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("learning_goals.id"), nullable=False)
    questions_json: Mapped[str] = mapped_column(Text, nullable=False)
    # "pending" = waiting for student answer | "passed" | "failed" | "deferred"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The user Message whose gated access started this exam; replayed after a
    # pass so the gate stops swallowing questions. Nullable (legacy attempts).
    trigger_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    # When the student last asked to postpone this exam. The prerequisite gate
    # leaves a deferred goal alone for mastery.DEFER_SNOOZE_HOURS after this,
    # so "can we talk about eclipses instead?" actually changes the subject.
    deferred_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    student: Mapped[Student] = relationship(back_populates="mastery_attempts")
    goal: Mapped[LearningGoal] = relationship("LearningGoal")


class GoalMisconception(Base):
    """Cohort-level misconception library entry for a goal (pedagogy item 12).

    Mined weekly by tutor/agent/misconception_mining.py from the per-student
    :class:`Misconception` notes (the actor's distilled record of a wrong model)
    for goals with enough distinct students. Rows are born ``candidate``; only
    ``approved`` rows reach prompts (the envelope's misconception note and the
    mastery-exam grader), so auto-mined text never shapes teaching unreviewed.
    Curate with scripts/mine_misconceptions.py --list/--approve/--reject.
    """

    __tablename__ = "goal_misconceptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("learning_goals.id"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    distractor: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    examples_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate", index=True)
    run_label: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class ScorePrediction(Base):
    """One predict-your-score data point on a code evaluation (pedagogy item 11).

    Before evaluate_code's result is revealed, the student predicts their 0-4
    rubric score. Distinct from the Engram confidence system (TurnEvent
    .confidence, 0-100 on review/exam answers, Brier-scored in the admin
    calibration report): this is an ordinal score prediction, error =
    predicted - actual. The prediction is recorded server-side only and never
    enters tutor/agent at all, so the grader stays blind by construction.
    Written only by the chat route's turn wrapper; item 13 aggregates it.
    """

    __tablename__ = "score_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    course: Mapped[str] = mapped_column(String(8), nullable=False, default=lambda: DEFAULT_COURSE)
    predicted: Mapped[int] = mapped_column(Integer, nullable=False)
    actual: Mapped[int] = mapped_column(Integer, nullable=False)
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # First 200 chars of the submitted message: enough to group by exercise
    # later without duplicating the code blob.
    context: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class TurnEvent(Base):
    """One row per supervised-tutor transition (P6: everything observable).

    Written ONLY through tutor/agent/state.py (the single writer). Captures the
    envelope, classified intent, rung used, judge verdicts, deterministic
    checks, and token/cost/latency for each turn or state transition.
    """

    __tablename__ = "turn_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # chat | exam | exercise | ...
    goal_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    envelope_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    rung_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    verdicts_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    grounding_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # JSON list of the sources the actor retrieved this turn (what the live judge
    # saw). Lets the offline auditor re-judge grounding against the real sources
    # instead of an empty list. Nullable: legacy rows predate the column.
    sources_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    det_check_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Pre-reveal stated confidence 0-100 (Engram integration, Phase 2). Set on
    # review/exam turns where the student picked a confidence band before seeing
    # the grade (90/70/50/25); NULL when skipped or not applicable (never
    # inferred). Feeds the admin calibration report and the high-confidence-wrong
    # miscalibration note. Prod Postgres needs the ALTER in startup._ADDED_COLUMNS.
    confidence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    in_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    out_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Tokens written to the prompt cache (billed at 1.25x input); plan step
    # 2.1. Nullable so the boot-time ALTER needs no default on old rows.
    cache_write_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The turn's id, the same one the chat stream sends as its resume handle
    # and the browser's activity events carry, so a click joins its turn
    # (step 1.2). Nullable: rows before October 2026 have none, and only chat
    # turns get one.
    turn_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)

    samples: Mapped[List[JudgeSample]] = relationship(
        back_populates="turn_event", cascade="all, delete-orphan"
    )


class JudgeSample(Base):
    """One judge vote on a turn, kept for auditor disagreement analysis."""

    __tablename__ = "judge_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn_event_id: Mapped[int] = mapped_column(ForeignKey("turn_events.id"), nullable=False, index=True)
    judge: Mapped[str] = mapped_column(String(64), nullable=False)   # model/tier label
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)  # allow | revise | block
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    turn_event: Mapped[TurnEvent] = relationship(back_populates="samples")


class TopicAccess(Base):
    __tablename__ = "topics_accessed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True,   # plan step 2.8, C7
    )
    lecture_num: Mapped[int] = mapped_column(Integer, nullable=False)
    slide_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topic_tag: Mapped[str] = mapped_column(String(128), nullable=False)
    accessed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )

    student: Mapped[Student] = relationship(back_populates="topics")


class Track(Base):
    """An ordered curriculum path through the learning goals of one course."""

    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "AST101-lecture-order"
    course: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    goal_ids_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON ordered list of goal ids
    graph_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    student_paths: Mapped[List[StudentPath]] = relationship(back_populates="track")

    def goal_ids(self) -> list:
        """Parsed ordered goal-id list, or [] when absent/malformed."""
        return safe_json_loads(self.goal_ids_json, []) or []


class StudentPath(Base):
    """A student's active track choice for a course."""

    __tablename__ = "student_paths"
    __table_args__ = (UniqueConstraint("student_id", "course"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False)
    course: Mapped[str] = mapped_column(String(8), nullable=False)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id"), nullable=False)
    modality_prefs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    student: Mapped[Student] = relationship(back_populates="paths")
    track: Mapped[Track] = relationship(back_populates="student_paths")


class PathEvent(Base):
    """One intentional navigation action by a student (Session 15, S25).

    Records goal chosen, modality selected, and whether the student or the
    system drove the choice. This is the primary data feed for adaptive pacing
    research and any future study of how students navigate the curriculum.

    modality values: see tutor.agent.state.PATH_MODALITIES
    source values  : student | system
    """

    __tablename__ = "path_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    course: Mapped[str] = mapped_column(String(8), nullable=False)
    goal_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("learning_goals.id"), nullable=True, index=True
    )
    modality: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="student")
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, index=True
    )

    student: Mapped["Student"] = relationship(back_populates="path_events")


class ActivityEvent(Base):
    """One thing a student did in the browser: a page opened or left, a click,
    a question sent, a reply finished, a slide shown with its on-screen dwell,
    a narration clip played.

    Rows arrive in batches from ``POST /api/events`` and are written only
    through ``state.record_activity_events``. The vocabulary of ``name`` and
    the fields each event may carry live in ``tutor/activity_events.py``, and
    nothing here is free text: ``label`` is the static text of a control
    (blanked at anonymization) and ``detail_json`` holds only the typed fields
    the vocabulary allows. ``login_session_id`` and ``message_id`` are plain
    integers, not foreign keys, because anonymization deletes ``sessions`` and
    ``messages`` while this table stays as de-identified telemetry under the
    pseudonym, like ``turn_events``.
    """

    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_activity_events_student_ts", "student_id", "ts"),
        Index("ix_activity_events_name_ts", "name", "ts"),
        Index("ix_activity_events_visit_seq", "visit_id", "seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    # The sessions row of the cookie that sent the batch (one row per login).
    login_session_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # A random id per browser tab (sessionStorage) and one per page load.
    visit_id: Mapped[str] = mapped_column(String(32), nullable=False)
    page_view_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # 1, 2, 3 ... within the page view; a gap is a lost batch.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    # Server time, corrected per batch by the browser's clock offset, so it
    # compares with every other table's timestamps.
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, index=True
    )
    client_ts: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    course: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    lecture_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slide_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    turn_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    path: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    target: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    # The one duration most events carry: a dwell, a played span, a wait.
    value_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detail_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    student: Mapped["Student"] = relationship(back_populates="activity_events")


class RequestError(Base):
    """One 4xx or 5xx the app answered a browser with, and the students'
    reports from the Tell-the-instructor button (tutor/request_errors.py;
    step 0.1). Written only through that module: the path carries no query
    and no magic-link token, the referrer only when it is this site's, the
    user agent only as a class, and ``ref`` is the short reference the error
    page showed. ``student_id`` is set when the cookie
    was a live session. Pruned after 90 days by the retention sweep.
    """

    __tablename__ = "request_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(200), nullable=False)
    referrer_path: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("students.id"), nullable=True, index=True)
    # page | fetch | deck | audio | book | explorable | static | report
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # browser | bot | scanner
    agent: Mapped[str] = mapped_column(String(12), nullable=False)
    ref: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # A 500's exception type, or the reference a report points at.
    detail: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)


class StudentDay(Base):
    """One student's day on the site, folded from the raw tables (tutor/
    student_days.py; step 0.9): the counts the instructor pages and the
    Sunday email compare students on, computed once from messages, the ledger, the browser's activity events,
    sessions, the sign-in trail, the feedback history and the error pages.
    ``day`` is a local calendar day; a day without activity has no row.
    Ids and counts only, never text, so the rows stay under the pseudonym
    when a student is anonymized.
    """

    __tablename__ = "student_days"
    __table_args__ = (UniqueConstraint("student_id", "day", name="uq_student_days_student_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    day: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    # The course most of the day's questions (else events) were in, if any.
    course: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    questions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    active_min: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    deck_min: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    narration_min: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    visits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    slides_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lectures_opened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_outcomes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    busy_outcomes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detector_cuts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    slow_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbs_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbs_down: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_agents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The local hours with any signal, as 24 bits (bit 0 is midnight).
    hours_mask: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Engine / session factory helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# No NUL byte reaches the database
# ---------------------------------------------------------------------------
# Postgres text cannot hold U+0000: a student's paste that carried one (a hex
# dump, some PDF extracts) failed the turn, or any form or query that bound
# it, with a DataError and a 500 (R8).
# It is removed from every bound string on every engine, reads included, so
# no route has to remember. Nothing else about a value changes.

def _strip_nul(value: Any) -> Any:
    return value.replace("\x00", "") if isinstance(value, str) and "\x00" in value else value


def _strip_nul_row(row: Any) -> Any:
    if isinstance(row, dict):
        if any(isinstance(v, str) and "\x00" in v for v in row.values()):
            return {k: _strip_nul(v) for k, v in row.items()}
        return row
    if isinstance(row, (list, tuple)) and any(isinstance(v, str) and "\x00" in v for v in row):
        return type(row)(_strip_nul(v) for v in row)
    return row


def _is_many(parameters: Any) -> bool:
    """A list of rows, by shape: a batched insert can arrive flagged
    ``executemany`` with one flat row of values."""
    return (isinstance(parameters, (list, tuple)) and bool(parameters)
            and all(isinstance(row, (dict, list, tuple)) for row in parameters))


@event.listens_for(Engine, "before_cursor_execute", retval=True)
def _no_nul_in_parameters(conn, cursor, statement, parameters, context, executemany):
    if _is_many(parameters):
        return statement, type(parameters)(_strip_nul_row(row) for row in parameters)
    return statement, _strip_nul_row(parameters)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _database_url(db_path=None):
    """The database URL the app would open: ``DATABASE_URL`` (normalized for
    psycopg v3) when set, else the local SQLite file."""
    # Production: use DATABASE_URL (e.g. Supabase Postgres) if provided.
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # Normalize common scheme variants to the psycopg (v3) driver.
        if url.startswith(("postgres://", "postgresql://")):
            url = "postgresql+psycopg://" + url.split("://", 1)[1]
        # Strip ORM-only query params that libpq/psycopg rejects (Supabase's
        # Prisma string adds ?schema= / ?pgbouncer= / ?connection_limit=).
        u = make_url(url)
        _bad = {"schema", "pgbouncer", "connection_limit", "pool_timeout"}
        return u.set(query={k: v for k, v in u.query.items() if k not in _bad})
    # Local/dev default: SQLite file.
    if db_path is None:
        db_path = get_config()["db_path"]
    return make_url(f"sqlite:///{db_path}")


def _build_engine(u):
    if u.get_backend_name() == "sqlite":
        return create_engine(u, connect_args={"check_same_thread": False})
    # A small pool of pre-pinged connections (plan step 2.6, finding C1). The
    # app used to open a fresh TCP+TLS connection, plus the dialect's setup
    # queries, for every request. Supabase's session pooler (port 5432, what
    # production uses) caps client connections at about 15 on the free tier.
    # One instance holds at most DB_POOL_SIZE + DB_MAX_OVERFLOW (default
    # 3 + 4), leaving room for the backup workflow and scripts; deploys are
    # stop-start (the persistent disk allows one instance), so two pools never
    # overlap. pool_pre_ping replaces a connection the pooler dropped;
    # pool_recycle retires idle ones before a network idle timeout can.
    # Server-side prepared statements stay off so the transaction pooler
    # (port 6543) also works.
    return create_engine(
        u,
        pool_size=_env_int("DB_POOL_SIZE", 3),
        max_overflow=_env_int("DB_MAX_OVERFLOW", 4),
        pool_timeout=_env_int("DB_POOL_TIMEOUT", 10),
        pool_recycle=_env_int("DB_POOL_RECYCLE", 300),
        pool_pre_ping=True,
        connect_args={"prepare_threshold": None},
    )


def get_engine(db_path=None):
    """A new Engine for the configured database.

    For scripts, migrations, and one-shot tools. The web app uses
    :func:`app_engine`, which is created once and shared.
    """
    return _build_engine(_database_url(db_path))


_APP_ENGINES: dict = {}
_APP_ENGINES_LOCK = threading.Lock()


def app_engine():
    """The web app's shared Engine (plan step 2.6, finding C1).

    One per database URL, so a test that points ``TUTOR_DB`` or
    ``DATABASE_URL`` elsewhere gets its own. ``get_db`` used to build a new
    Engine, and with it a new connection, on every request.
    """
    u = _database_url()
    key = u.render_as_string(hide_password=False)
    with _APP_ENGINES_LOCK:
        engine = _APP_ENGINES.get(key)
        if engine is None:
            engine = _APP_ENGINES[key] = _build_engine(u)
        return engine


def dispose_app_engines() -> None:
    """Close the shared engines' pooled connections (app shutdown, tests)."""
    with _APP_ENGINES_LOCK:
        engines = list(_APP_ENGINES.values())
        _APP_ENGINES.clear()
    for engine in engines:
        engine.dispose()


def get_session_factory(engine=None):
    if engine is None:
        engine = get_engine()
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db(engine=None) -> None:
    """Create all tables if they don't exist."""
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
