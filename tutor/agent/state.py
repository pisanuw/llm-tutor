"""Single writer for learner state and telemetry (P3).

This is the ONLY module allowed to write the progress / mastery / event tables
(StudentProgress, MasteryAttempt, TurnEvent, JudgeSample). Every other module
(progress.py, mastery.py, tutor.py, routers) goes through these typed
transitions. The actor never writes state.

A test (tests/test_state.py) asserts that no other module constructs those
models, so the invariant holds mechanically.

Each meaningful transition also records a TurnEvent (P6: everything
observable). Reads of these tables are unrestricted; only writes are funneled
here.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session as DBSession

from tutor.agent import spaced_review
from tutor.db.models import (
    ActivityEvent,
    ExerciseAttempt,
    GradedEvent,
    JudgeSample,
    LearningGoal,
    MasteryAttempt,
    Message,
    MessageFeedback,
    Misconception,
    PathEvent,
    ScorePrediction,
    SessionSummary,
    Student,
    StudentPath,
    StudentProgress,
    TopicAccess,
    TurnEvent,
)
from tutor.utctime import utcnow

# Graded-mastery tunables (Task 5).
WINDOW_K = 6              # events in the success-rate window
UNLOCK_THRESHOLD = 0.6   # mastery_score to count as unlocked
MASTER_THRESHOLD = 0.85  # mastery_score to count as mastered
MASTER_MIN_EVENTS = 4    # ... with at least this many graded events
# Evidence-based mastery (pedagogy item 9): a goal is CONFIRMED only when
# two successful retrievals are at least this many hours apart. One good quiz
# sitting can reach status=mastered (progression tier) but cannot confirm; the
# FSRS review that comes due later supplies the second, spaced retrieval. It
# used to be two distinct UTC dates, which roll over at 4 or 5 pm in Seattle,
# so two retrievals an hour apart could confirm (plan step 1.10, finding B10).
CONFIRM_MIN_GAP_HOURS = 16
# A GradedEvent counts as a successful retrieval at/above this. 0.7 keeps a
# shaky review (HARD, 0.6) and a half-right exercise (2/4) out, while GOOD
# (0.85), EASY, an exam pass, and a 3/4 exercise count (plan 1.10, B11).
RETRIEVAL_SUCCESS = 0.7
DECAY_DAYS = 21          # mastery_score decays to the floor over this many days
DECAY_FLOOR = 0.6        # ... never below this from decay

_STATUS_ORDER = {"locked": 0, "unlocked": 1, "mastered": 2}


def _utcnow() -> datetime.datetime:
    return utcnow()


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    if is_dataclass(value):
        return json.dumps(asdict(value), default=str)
    return json.dumps(value, default=str)


# ---------------------------------------------------------------------------
# Progress transitions
# ---------------------------------------------------------------------------

def _upsert_progress(
    db: DBSession,
    student_id: int,
    goal_id: str,
    target_status: str,
    evidence_type: str,
    should_upgrade,
) -> bool:
    """Create or upgrade a StudentProgress row to ``target_status``.

    Creates a new row when none exists; otherwise upgrades only when
    ``should_upgrade(current_status)`` is True, leaving the row untouched
    (and returning False) when it is already at or past the target. Sets
    ``evidence_type`` and ``updated_at`` on any write. Returns True if a row
    was created or changed.
    """
    row = (
        db.query(StudentProgress)
        .filter_by(student_id=student_id, goal_id=goal_id)
        .first()
    )
    now = _utcnow()
    if row is None:
        db.add(StudentProgress(
            student_id=student_id,
            goal_id=goal_id,
            status=target_status,
            evidence_type=evidence_type,
            updated_at=now,
            mastered_at=now if target_status == "mastered" else None,
        ))
        db.flush()
        return True
    if should_upgrade(row.status):
        row.status = target_status
        row.evidence_type = evidence_type
        row.updated_at = now
        if target_status == "mastered" and row.mastered_at is None:
            row.mastered_at = now
        db.flush()
        return True
    return False


def unlock_goal(
    db: DBSession,
    student_id: int,
    goal_id: str,
    evidence_type: str = "chat",
) -> bool:
    """Upsert a goal to 'unlocked' (no-op if already unlocked/mastered).

    Returns True if a row was created or changed.
    """
    return _upsert_progress(
        db, student_id, goal_id,
        target_status="unlocked",
        evidence_type=evidence_type,
        should_upgrade=lambda status: status == "locked",
    )


def mark_goal_mastered(
    db: DBSession,
    student_id: int,
    goal_id: str,
    evidence_type: str = "exercise",
) -> bool:
    """Upsert a goal to 'mastered'. Returns True if created or changed."""
    return _upsert_progress(
        db, student_id, goal_id,
        target_status="mastered",
        evidence_type=evidence_type,
        should_upgrade=lambda status: status != "mastered",
    )


# ---------------------------------------------------------------------------
# Graded mastery (windowed success rate + decay)
# ---------------------------------------------------------------------------

def windowed_score(db: DBSession, student_id: int, goal_id: str, k: int = WINDOW_K) -> tuple[float, int]:
    """Return (mean success over the last k graded events, total event count)."""
    q = db.query(GradedEvent).filter_by(student_id=student_id, goal_id=goal_id)
    total = q.count()
    rows = q.order_by(GradedEvent.ts.desc(), GradedEvent.id.desc()).limit(k).all()
    if not rows:
        return 0.0, 0
    return sum(r.success for r in rows) / len(rows), total


def effective_mastery(
    score: Optional[float],
    updated_at: Optional[datetime.datetime],
    now: Optional[datetime.datetime] = None,
) -> float:
    """Decay ``score`` linearly toward the floor over DECAY_DAYS since contact.

    Pure. A score at or below the floor does not decay. Used to surface review
    nudges and to set the envelope rung.
    """
    if score is None:
        return 0.0
    if score <= DECAY_FLOOR or updated_at is None:
        return score
    now = now or _utcnow()
    days = max(0.0, (now - updated_at).total_seconds() / 86400.0)
    if days >= DECAY_DAYS:
        return DECAY_FLOOR
    return score - (score - DECAY_FLOOR) * (days / DECAY_DAYS)


# Rating -> success fraction for the windowed mastery_score, so a spaced-review
# outcome feeds both FSRS (via the explicit rating) and the graded window.
_REVIEW_RATING_SUCCESS: dict[spaced_review.Rating, float] = {
    spaced_review.Rating.AGAIN: 0.0,
    spaced_review.Rating.HARD: 0.6,
    spaced_review.Rating.GOOD: 0.85,
    spaced_review.Rating.EASY: 1.0,
}


def fsrs_effective_mastery(
    score: Optional[float],
    stability: Optional[float],
    elapsed_days: float,
) -> float:
    """FSRS-driven decay of ``score`` toward the floor (Engram integration P1).

    Drop-in replacement for :func:`effective_mastery`'s flat 21-day clock: the
    competence score fades by the FSRS *predicted forgetting* (1 - retrievability)
    instead of by elapsed days alone, so a goal with high stability (demonstrated
    well, many times) barely decays while a shaky one fades fast. Preserves the
    band endpoints exactly — no fade at retrievability 1.0 (full ``score``), full
    fade to ``DECAY_FLOOR`` as retrievability -> 0 — so the tuned RUNG_BANDS
    behaviour is unchanged at the extremes. Pure. Falls back to the undecayed
    score when there is no FSRS stability yet (legacy rows use effective_mastery).
    """
    if score is None:
        return 0.0
    if score <= DECAY_FLOOR or stability is None:
        return score
    r = spaced_review.retrievability(elapsed_days, stability)
    return score - (score - DECAY_FLOOR) * (1.0 - r)


@dataclass(frozen=True)
class ProgressSnapshot:
    """What :func:`next_progress` reads: a goal's progress row before a graded
    event, and the goal's graded history with that event included."""

    status: str = "locked"
    mastered_at: Optional[datetime.datetime] = None
    confirmed_at: Optional[datetime.datetime] = None
    last_evidence_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None
    fsrs_stability: Optional[float] = None
    fsrs_difficulty: Optional[float] = None
    fsrs_reps: int = 0
    fsrs_lapses: int = 0
    window: tuple[float, ...] = ()            # the last WINDOW_K successes, newest first
    n_events: int = 0                         # every graded event on the goal
    retrievals: tuple[datetime.datetime, ...] = ()   # when successes >= RETRIEVAL_SUCCESS happened


@dataclass(frozen=True)
class GradedOutcome:
    """One graded event: its success fraction, the FSRS rating when the
    caller has a finer signal than the fraction (the review loop), and whether
    it may move ``status`` (spaced-review answers may not)."""

    success: float
    fsrs_rating: Optional[spaced_review.Rating] = None
    upgrade_status: bool = True


@dataclass(frozen=True)
class ProgressUpdate:
    """The row after the event. ``status_changed`` means the status moved up
    (the row's evidence_type becomes "graded")."""

    mastery_score: float
    status: str
    status_changed: bool
    mastered_at: Optional[datetime.datetime]
    confirmed_at: Optional[datetime.datetime]
    fsrs_stability: float
    fsrs_difficulty: float
    fsrs_reps: int
    fsrs_lapses: int
    fsrs_due: datetime.datetime
    last_evidence_at: datetime.datetime


def next_progress(snapshot: ProgressSnapshot, event: GradedOutcome, now: datetime.datetime) -> ProgressUpdate:
    """The learner-state transition for one graded event (plan step 5.3,
    finding B28). Pure: no database and no clock, so thresholds, confirmation,
    and FSRS scheduling are table-tested (tests/test_next_progress.py).

    - The mastery_score is the mean success over the window.
    - Status moves up only (never down): unlocked at >= UNLOCK_THRESHOLD,
      mastered at >= MASTER_THRESHOLD with >= MASTER_MIN_EVENTS events, and
      not at all when ``upgrade_status`` is off (spaced review is practice,
      not a route to unlocked or mastered: plan step 1.6, decision 6).
      Mastery is sticky by design: a later bad streak does not relock a
      goal; rustiness shows through the decayed score (current_mastery) and
      the review clock (review_is_due), not by flipping status.
    - FSRS-4.5 (Engram P1): every graded event is a review for scheduling.
      The rating is ``fsrs_rating`` when given, else derived from the
      success fraction, so FSRS stays populated from every evidence source
      (exams, exercises, reviews). Elapsed days run from the previous
      evidence (last_evidence_at, else updated_at for legacy rows).
    - Confirmation: a mastered goal is confirmed once two successful
      retrievals are at least CONFIRM_MIN_GAP_HOURS apart. Never un-confirms.
    """
    success = max(0.0, min(1.0, float(event.success)))
    window = snapshot.window
    score = sum(window) / len(window) if window else 0.0

    reps = snapshot.fsrs_reps or 0
    rating = (event.fsrs_rating if event.fsrs_rating is not None
              else spaced_review.rating_from_fraction(success))
    elapsed_days = 0.0
    prev_evidence = snapshot.last_evidence_at or snapshot.updated_at
    if reps > 0 and prev_evidence is not None:
        elapsed_days = max(0.0, (now - prev_evidence).total_seconds() / 86400.0)
    review = spaced_review.rate(
        spaced_review.FSRSState(
            stability=snapshot.fsrs_stability,
            difficulty=snapshot.fsrs_difficulty,
            reps=reps,
            lapses=snapshot.fsrs_lapses or 0,
        ),
        rating,
        elapsed_days,
    )

    target = snapshot.status
    if event.upgrade_status:
        if score >= MASTER_THRESHOLD and snapshot.n_events >= MASTER_MIN_EVENTS:
            target = "mastered"
        elif score >= UNLOCK_THRESHOLD:
            target = "unlocked"
    status, changed, mastered_at = snapshot.status, False, snapshot.mastered_at
    if _STATUS_ORDER[target] > _STATUS_ORDER[snapshot.status]:
        status, changed = target, True
        if target == "mastered" and mastered_at is None:
            mastered_at = now

    confirmed_at = snapshot.confirmed_at
    stamps = snapshot.retrievals
    if (status == "mastered" and confirmed_at is None and len(stamps) >= 2
            and max(stamps) - min(stamps) >= datetime.timedelta(hours=CONFIRM_MIN_GAP_HOURS)):
        confirmed_at = now

    return ProgressUpdate(
        mastery_score=score,
        status=status,
        status_changed=changed,
        mastered_at=mastered_at,
        confirmed_at=confirmed_at,
        fsrs_stability=review.stability,
        fsrs_difficulty=review.difficulty,
        fsrs_reps=review.reps,
        fsrs_lapses=review.lapses,
        fsrs_due=now + datetime.timedelta(days=review.interval_days),
        # The decay clock (S19): only real graded evidence moves it, so a
        # later status flip or bookkeeping write cannot reset decay.
        last_evidence_at=now,
    )


def _progress_snapshot(db: DBSession, row: StudentProgress, student_id: int, goal_id: str) -> ProgressSnapshot:
    """The row and its graded history, the event just recorded included."""
    events = db.query(GradedEvent).filter_by(student_id=student_id, goal_id=goal_id)
    window = tuple(
        r.success for r in
        events.order_by(GradedEvent.ts.desc(), GradedEvent.id.desc()).limit(WINDOW_K).all()
    )
    retrievals: tuple[datetime.datetime, ...] = ()
    if row.confirmed_at is None:     # a confirmed goal stays confirmed
        retrievals = tuple(
            ts for (ts,) in db.query(GradedEvent.ts)
            .filter(GradedEvent.student_id == student_id,
                    GradedEvent.goal_id == goal_id,
                    GradedEvent.success >= RETRIEVAL_SUCCESS)
            .all()
            if ts is not None
        )
    return ProgressSnapshot(
        status=row.status or "locked",
        mastered_at=row.mastered_at,
        confirmed_at=row.confirmed_at,
        last_evidence_at=row.last_evidence_at,
        updated_at=row.updated_at,
        fsrs_stability=row.fsrs_stability,
        fsrs_difficulty=row.fsrs_difficulty,
        fsrs_reps=row.fsrs_reps or 0,
        fsrs_lapses=row.fsrs_lapses or 0,
        window=window,
        n_events=events.count(),
        retrievals=retrievals,
    )


def record_graded_event(
    db: DBSession,
    student_id: int,
    goal_id: str,
    success: float,
    source: str = "",
    fsrs_rating: Optional[spaced_review.Rating] = None,
    upgrade_status: bool = True,
) -> float:
    """Append a graded outcome and apply :func:`next_progress` to the goal's
    progress row. Returns the new mastery_score.

    ``upgrade_status=False`` records the evidence (window, FSRS schedule,
    confirmation) without moving ``status``: spaced-review answers are
    practice, not a route to unlocked or mastered (plan step 1.6, decision 6).
    """
    success = max(0.0, min(1.0, float(success)))
    now = _utcnow()
    db.add(GradedEvent(
        student_id=student_id, goal_id=goal_id, success=success, source=source, ts=now,
    ))
    db.flush()

    row = (
        db.query(StudentProgress)
        .filter_by(student_id=student_id, goal_id=goal_id)
        .first()
    )
    if row is None:
        row = StudentProgress(student_id=student_id, goal_id=goal_id, status="locked")
        db.add(row)
    update = next_progress(
        _progress_snapshot(db, row, student_id, goal_id),
        GradedOutcome(success=success, fsrs_rating=fsrs_rating, upgrade_status=upgrade_status),
        now,
    )
    row.mastery_score = update.mastery_score
    row.fsrs_stability = update.fsrs_stability
    row.fsrs_difficulty = update.fsrs_difficulty
    row.fsrs_reps = update.fsrs_reps
    row.fsrs_lapses = update.fsrs_lapses
    row.fsrs_due = update.fsrs_due
    row.last_evidence_at = update.last_evidence_at
    if update.status_changed:
        row.status = update.status
        row.evidence_type = "graded"
    row.mastered_at = update.mastered_at
    row.confirmed_at = update.confirmed_at
    row.updated_at = now
    db.flush()
    return update.mastery_score


# ---------------------------------------------------------------------------
# One review clock (plan step 5.3, finding B20)
# ---------------------------------------------------------------------------

def review_is_due(fsrs_due: Optional[datetime.datetime], now: Optional[datetime.datetime] = None) -> bool:
    """The one "review due" clock: a goal is due once its FSRS due date has
    arrived. The navigator, the progress summary, /review, and the class
    report all read it; they used to run three clocks that disagreed, and a
    mastered row with no evidence date was due forever (B20). A row with no
    FSRS schedule is not due: its first graded event gives it one."""
    return fsrs_due is not None and fsrs_due <= (now or _utcnow())


def review_due_rows(
    db: DBSession, student_id: int, course: str, now: Optional[datetime.datetime] = None,
) -> list[StudentProgress]:
    """The student's goals in ``course`` that are due (review_is_due), the
    longest overdue first."""
    now = now or _utcnow()
    return (
        db.query(StudentProgress)
        .join(LearningGoal, LearningGoal.id == StudentProgress.goal_id)
        .filter(
            StudentProgress.student_id == student_id,
            LearningGoal.course == course,
            StudentProgress.fsrs_due.isnot(None),
            StudentProgress.fsrs_due <= now,
        )
        .order_by(StudentProgress.fsrs_due.asc(), StudentProgress.goal_id)
        .all()
    )


def current_mastery(row: StudentProgress, now: Optional[datetime.datetime] = None) -> float:
    """A progress row's mastery_score as of ``now``, decayed on the same clock
    everywhere: by FSRS predicted forgetting when the row has a stability
    (Engram P1), else by the flat DECAY_DAYS clock for rows from before FSRS.
    The decay clock is last_evidence_at (S19); legacy rows fall back to
    updated_at."""
    now = now or _utcnow()
    clock = row.last_evidence_at or row.updated_at
    if row.fsrs_stability is not None:
        elapsed_days = max(0.0, (now - clock).total_seconds() / 86400.0) if clock else 0.0
        return fsrs_effective_mastery(row.mastery_score, row.fsrs_stability, elapsed_days)
    return effective_mastery(row.mastery_score, clock, now)


def record_review_rating(
    db: DBSession,
    student_id: int,
    goal_id: str,
    rating: spaced_review.Rating,
    source: str = "review",
) -> float:
    """Record one spaced-review outcome (Engram P1). Single writer (P3).

    The review loop grades a free-recall probe and maps the score to an FSRS
    rating; this advances the FSRS schedule with that exact rating and feeds the
    windowed mastery_score with the rating's success fraction. Returns the new
    mastery_score. It never moves ``status`` (decision 6's default): the exam
    and exercises decide unlocked and mastered; a later review can still
    confirm a goal that is already mastered.
    """
    success = _REVIEW_RATING_SUCCESS[spaced_review.Rating(int(rating))]
    return record_graded_event(
        db, student_id, goal_id, success, source=source, fsrs_rating=rating,
        upgrade_status=False,
    )


def record_misconception(
    db: DBSession,
    student_id: int,
    goal_id: str,
    description: str,
) -> Misconception:
    """Log a durable misconception for a (student, goal) (Engram P3).

    At most one unresolved row per (student, goal): an existing open row is
    refreshed (new description + created_at) rather than duplicated, so the
    contract note never repeats the same wrong model. Single writer (P3).
    """
    description = (description or "").strip()[:1000]
    row = (
        db.query(Misconception)
        .filter_by(student_id=student_id, goal_id=goal_id, resolved_at=None)
        .order_by(Misconception.id.desc())
        .first()
    )
    if row is None:
        row = Misconception(student_id=student_id, goal_id=goal_id, description=description)
        db.add(row)
    else:
        row.description = description
        row.created_at = _utcnow()
    db.flush()
    return row


def resolve_misconceptions(db: DBSession, student_id: int, goal_id: str) -> int:
    """Close all open misconceptions for a (student, goal). Returns the count.

    Called when the student later demonstrates the goal (a correct, reasonably
    confident answer), so the contract note stops flagging a model they have
    since corrected. Single writer (P3).
    """
    rows = (
        db.query(Misconception)
        .filter_by(student_id=student_id, goal_id=goal_id, resolved_at=None)
        .all()
    )
    now = _utcnow()
    for row in rows:
        row.resolved_at = now
    if rows:
        db.flush()
    return len(rows)


def set_student_restricted(db: DBSession, student_id: int, restricted: bool) -> bool:
    """Set/clear the conduct restriction flag (Session 10, S24).

    Set by the conduct ladder's second strike; cleared only by the
    instructor (scripts/clear_restriction.py until the Phase F admin
    button). Records a TurnEvent either way. Returns the new value.
    """
    student = db.get(Student, student_id)
    if student is None:
        return False
    student.restricted = bool(restricted)
    db.flush()
    # kind 'restriction', NOT 'conduct': the conduct ladder counts conduct
    # events per day, and a set/clear must not feed that counter.
    record_turn_event(
        db,
        student_id=student_id,
        kind="restriction",
        verdicts={"restricted": bool(restricted)},
    )
    return bool(restricted)


def mark_goal_seen(
    db: DBSession,
    student_id: int,
    goal_id: str,
) -> bool:
    """Mark a goal as seen (discussed in chat) without changing its mastery status.

    Creates a locked row with seen=True if none exists. Returns True if
    the row was created or the seen flag was newly set.
    """
    row = (
        db.query(StudentProgress)
        .filter_by(student_id=student_id, goal_id=goal_id)
        .first()
    )
    if row is None:
        db.add(StudentProgress(
            student_id=student_id,
            goal_id=goal_id,
            status="locked",
            seen=True,
            updated_at=_utcnow(),
        ))
        db.flush()
        return True
    if not row.seen:
        row.seen = True
        row.updated_at = _utcnow()
        db.flush()
        return True
    return False


# The one list of path-event modalities and sources (plan step 2.9, B24): the
# router validates against these, and the model docstring points here.
PATH_MODALITIES = frozenset({"teach", "slides", "book", "quiz", "review", "track_set", "explore"})
PATH_SOURCES = frozenset({"student", "system"})


def record_path_event(
    db: DBSession,
    student_id: int,
    course: str,
    modality: str,
    goal_id: Optional[str] = None,
    source: str = "student",
) -> PathEvent:
    """Record one navigation action (goal chosen, modality selected).

    ``modality`` is one of PATH_MODALITIES and ``source`` one of
    PATH_SOURCES. This is the primary feed for adaptive pacing and study data.
    """
    event = PathEvent(
        student_id=student_id,
        course=course,
        goal_id=goal_id,
        modality=modality,
        source=source,
        ts=_utcnow(),
    )
    db.add(event)
    db.flush()
    return event


def record_activity_events(
    db: DBSession,
    student_id: int,
    login_session_id: Optional[int],
    rows: list[dict[str, Any]],
) -> int:
    """Write one batch of browser activity events in one statement
    (step 1.3).

    ``rows`` are the dicts ``tutor.activity_events.clean_batch`` produced:
    the vocabulary's columns only, each already clipped and typed, with the
    server-aligned ``ts``. The student and the login session are stamped
    here so a client can never claim another's. Returns the number written.
    """
    if not rows:
        return 0
    db.execute(
        ActivityEvent.__table__.insert(),
        [{**row, "student_id": student_id, "login_session_id": login_session_id} for row in rows],
    )
    return len(rows)


def set_student_path(
    db: DBSession,
    student_id: int,
    course: str,
    track_id: str,
) -> StudentPath:
    """Upsert the student's track choice for a course.

    Creates a new StudentPath row or updates the existing one. The unique
    constraint is (student_id, course) so each student has at most one track
    per course at a time.
    """
    row = (
        db.query(StudentPath)
        .filter_by(student_id=student_id, course=course)
        .first()
    )
    if row is None:
        row, created = _insert_student_path(db, student_id, course, track_id)
        if created:
            return row
    row.track_id = track_id
    row.updated_at = _utcnow()
    db.flush()
    return row


def _insert_student_path(
    db: DBSession, student_id: int, course: str, track_id: str,
) -> tuple[StudentPath, bool]:
    """Insert a StudentPath inside a savepoint; returns (row, created). If a
    concurrent request inserted the same (student, course) first, returns
    that row with created=False instead of failing on the unique key (plan
    step 2.2, finding C22)."""
    from sqlalchemy.exc import IntegrityError

    try:
        with db.begin_nested():
            row = StudentPath(student_id=student_id, course=course, track_id=track_id)
            db.add(row)
            db.flush()
        return row, True
    except IntegrityError:
        existing = db.query(StudentPath).filter_by(student_id=student_id, course=course).first()
        if existing is None:
            raise
        return existing, False


def ensure_student_path(
    db: DBSession,
    student_id: int,
    course: str,
    default_track_id: str,
) -> tuple[StudentPath, bool]:
    """Make ``course`` the student's current course (first-login UX).

    Creates the StudentPath with ``default_track_id`` when the student has no
    track for this course yet, recording a system ``track_set`` PathEvent; or
    touches the existing row (keeping its track) so it becomes the most
    recently updated path, which is what navigator.current_course reads.
    Returns ``(row, created)``.
    """
    row = (
        db.query(StudentPath)
        .filter_by(student_id=student_id, course=course)
        .first()
    )
    if row is None:
        row, created = _insert_student_path(db, student_id, course, default_track_id)
        if created:
            record_path_event(
                db,
                student_id=student_id,
                course=course,
                modality="track_set",
                goal_id=None,
                source="system",
            )
            return row, True
    row.updated_at = _utcnow()
    db.flush()
    return row, False


def upsert_session_summary(
    db: DBSession,
    student_id: int,
    course: str,
    summary: str,
    last_message_id: int,
) -> SessionSummary:
    """Store the rolling per-course conversation summary (Session 7)."""
    row = (
        db.query(SessionSummary)
        .filter_by(student_id=student_id, course=course)
        .first()
    )
    if row is None:
        row = SessionSummary(student_id=student_id, course=course)
        db.add(row)
    row.summary = summary[:4000]
    row.last_message_id = last_message_id
    row.updated_at = _utcnow()
    db.flush()
    return row


# ---------------------------------------------------------------------------
# Mastery exam transitions
# ---------------------------------------------------------------------------

def start_exam_attempt(
    db: DBSession,
    student_id: int,
    goal_id: str,
    questions_json: str,
    attempts: int = 0,
    trigger_message_id: Optional[int] = None,
) -> MasteryAttempt:
    """Create a pending MasteryAttempt and flush it."""
    attempt = MasteryAttempt(
        student_id=student_id,
        goal_id=goal_id,
        questions_json=questions_json,
        status="pending",
        attempts=attempts,
        trigger_message_id=trigger_message_id,
        started_at=_utcnow(),
    )
    db.add(attempt)
    db.flush()
    return attempt


def mark_attempt_failed(db: DBSession, attempt: MasteryAttempt) -> None:
    """Close an attempt as failed without reopening (error paths)."""
    attempt.completed_at = _utcnow()
    attempt.status = "failed"
    db.flush()


def void_exam_attempt(db: DBSession, attempt: MasteryAttempt, reason: str = "") -> None:
    """Close an attempt that could not be run for an infrastructure reason.

    Status "voided" and no ``completed_at``: it stops intercepting messages but
    does not count toward the day's attempt cap, and its questions are not
    treated as failed (plan step 1.7, finding B21). Records a TurnEvent.
    """
    attempt.status = "voided"
    db.flush()
    record_turn_event(
        db,
        student_id=attempt.student_id,
        kind="exam",
        goal_ids=[attempt.goal_id],
        verdicts={"voided": True, "reason": reason[:200]},
    )


def defer_exam_attempt(db: DBSession, attempt: MasteryAttempt) -> None:
    """Pause a pending exam: status 'deferred', no attempt consumed.

    The prerequisite gate re-fires on the next gated access and resumes the
    same questions (no rerolling by deferring). Records a TurnEvent.
    """
    attempt.status = "deferred"
    attempt.deferred_at = _utcnow()
    db.flush()
    record_turn_event(
        db,
        student_id=attempt.student_id,
        kind="exam",
        goal_ids=[attempt.goal_id],
        verdicts={"deferred": True},
    )


def resume_exam_attempt(db: DBSession, attempt: MasteryAttempt) -> MasteryAttempt:
    """Reactivate a deferred exam attempt (gate re-fired)."""
    attempt.status = "pending"
    db.flush()
    return attempt


class AttemptNotPending(Exception):
    """The exam attempt was already graded (or closed) by another request."""


def record_exam_result(
    db: DBSession,
    attempt: MasteryAttempt,
    passed: bool,
    requeue_questions_json: Optional[str] = None,
) -> Optional[MasteryAttempt]:
    """Close a pending exam attempt.

    On pass: mark the goal mastered. On fail: reopen a fresh pending attempt
    only when ``requeue_questions_json`` is given (mastery.py decides whether
    to requeue, applying the daily attempt cap, and resamples the questions).
    Returns the reopened attempt or None. Also records a TurnEvent of kind
    'exam'.

    The close is one conditional UPDATE (status must still be 'pending'), so
    two concurrent answers grade the attempt once: the loser raises
    AttemptNotPending (plan step 2.2, finding A6).
    """
    new_status = "passed" if passed else "failed"
    claimed = (
        db.query(MasteryAttempt)
        .filter(MasteryAttempt.id == attempt.id, MasteryAttempt.status == "pending")
        .update(
            {
                "status": new_status,
                "completed_at": _utcnow(),
                "attempts": MasteryAttempt.attempts + 1,
            },
            synchronize_session=False,
        )
    )
    if claimed != 1:
        raise AttemptNotPending(f"attempt {attempt.id} is not pending")
    db.refresh(attempt)

    reopened: Optional[MasteryAttempt] = None
    if passed:
        mark_goal_mastered(db, attempt.student_id, attempt.goal_id, evidence_type="mastery_exam")
    else:
        if requeue_questions_json is not None:
            reopened = start_exam_attempt(
                db, attempt.student_id, attempt.goal_id, requeue_questions_json,
                attempts=attempt.attempts,
                trigger_message_id=attempt.trigger_message_id,
            )
    db.flush()

    # Feed the graded-mastery window (1.0 on pass, 0.0 on fail).
    record_graded_event(db, attempt.student_id, attempt.goal_id, 1.0 if passed else 0.0, source="exam")

    record_turn_event(
        db,
        student_id=attempt.student_id,
        kind="exam",
        goal_ids=[attempt.goal_id],
        verdicts={"passed": bool(passed)},
    )
    return reopened


# ---------------------------------------------------------------------------
# Exercise outcome
# ---------------------------------------------------------------------------

def record_exercise_outcome(
    db: DBSession,
    student_id: int,
    exercise_id: str,
    lecture_num: Optional[int],
    submission: str,
    score: float,
    passed: bool,
    feedback: str,
    course: Optional[str] = None,
    goal_ids: Optional[list[str]] = None,
    evidence_source: str = "exercise",
) -> ExerciseAttempt:
    """Persist an exercise attempt and record graded evidence on its goals.

    Goal attribution (S9, plan step 1.5): the graded event lands on exactly
    ``goal_ids``: a bank exercise's own goals, or, for an unstructured chat
    submission, the goals the classifier found in that turn (turn.py). With no
    goals there is no graded event; the lecture is never used as a fallback, so
    one pasted snippet cannot touch every goal of the lecture.

    A score of 3+ flips the boolean mastered flag only on deterministic test
    evidence, or once the goal has MASTER_MIN_EVENTS graded events: one LLM
    opinion never masters a goal. ``evidence_source`` "unavailable" (the
    evaluator produced no score) records the attempt and no evidence. Records a
    TurnEvent of kind 'exercise'.

    Evidence source (S29): ``evidence_source`` is "test" when the score came from
    run_code facts (the strongest evidence class) or "llm-opinion"/"exercise"
    otherwise. The graded event records source "test" for facts and "exercise"
    otherwise; the TurnEvent verdict carries the precise eval_source.
    """
    graded_source = "test" if evidence_source == "test" else "exercise"
    attempt = ExerciseAttempt(
        student_id=student_id,
        exercise_id=exercise_id,
        lecture_num=lecture_num,
        submission=submission[:2000],
        score=float(score),
        feedback=feedback[:1000],
        passed=bool(passed),
    )
    db.add(attempt)

    target_goal_ids: list[str] = []
    if goal_ids and evidence_source != "unavailable":
        # Only attribute to goals that actually exist (defensive; ids come from
        # the curated bank or the classifier's validated catalog).
        existing = {
            g.id for g in db.query(LearningGoal).filter(LearningGoal.id.in_(goal_ids)).all()
        }
        target_goal_ids = [gid for gid in dict.fromkeys(goal_ids) if gid in existing]

    mastered_goal_ids: list[str] = []
    for gid in target_goal_ids:
        # Every exercise feeds the graded-mastery window (score on 0..1).
        record_graded_event(db, student_id, gid, float(score) / 4.0, source=graded_source)
        # A strong score (3+) also flips the boolean, but never on one opinion:
        # it takes deterministic test evidence or enough graded events.
        if score >= 3 and (
            graded_source == "test"
            or db.query(GradedEvent).filter_by(student_id=student_id, goal_id=gid).count()
            >= MASTER_MIN_EVENTS
        ):
            if mark_goal_mastered(db, student_id, gid, evidence_type=graded_source):
                mastered_goal_ids.append(gid)

    db.flush()
    record_turn_event(
        db,
        student_id=student_id,
        kind="exercise",
        goal_ids=mastered_goal_ids or None,
        verdicts={"score": float(score), "passed": bool(passed), "eval_source": evidence_source},
    )
    return attempt


# ---------------------------------------------------------------------------
# Topic access, score predictions, reply feedback (plan step 1.11: these were
# written from turn.py and routers/chat.py; every learner-state row now goes
# through this module, and tests/test_state.py enforces it)
# ---------------------------------------------------------------------------

_FENCED = re.compile(r"(```|~~~).*?(?:\1|\Z)", re.S)
# The chat's code-submission message (chat.html, pickPrediction).
_SUBMISSION = re.compile(
    r'^Please evaluate my (?:C\+\+ )?code(?: for this problem: "(?P<problem>[^\n]*)")?:?')


def topic_label(message: str) -> str:
    """What a message is about, for "Recent topics" and the admin Topics tab
    (plan step 3.5, finding D19): the problem of a code submission, else the
    message's prose without its code blocks, else "Code review" for code
    alone. Before, the first 64 characters were taken as they came, fences
    and code included."""
    text = message or ""
    submission = _SUBMISSION.match(text)
    if submission:
        return ((submission.group("problem") or "").strip() or "Code review")[:64]
    prose = " ".join(_FENCED.sub(" ", text).split())
    if prose:
        return prose[:64]
    return "Code review" if _FENCED.search(text) else ""


def record_topic_access(
    db: DBSession,
    student_id: int,
    lecture_num: int,
    slide_num: Optional[int] = None,
    topic_tag: str = "",
) -> TopicAccess:
    """Note that the student asked about a lecture (and slide) this turn.
    ``topic_tag`` may be the whole message; it is stored as its label."""
    row = TopicAccess(
        student_id=student_id, lecture_num=lecture_num, slide_num=slide_num,
        topic_tag=topic_label(topic_tag),
    )
    db.add(row)
    db.flush()
    return row


def record_score_prediction(
    db: DBSession,
    *,
    student_id: int,
    course: str,
    predicted: int,
    actual: int,
    correct: bool,
    context: str = "",
) -> ScorePrediction:
    """Store a pre-submission score prediction next to the score it got."""
    row = ScorePrediction(
        student_id=student_id, course=course, predicted=predicted, actual=actual,
        correct=bool(correct), context=(context or "")[:200],
    )
    db.add(row)
    db.flush()
    return row


def record_message_feedback(
    db: DBSession,
    *,
    message_id: int,
    student_id: int,
    rating: int,
    comment: Optional[str],
) -> MessageFeedback:
    """Upsert the student's thumbs up or down on one tutor reply. A fresh
    thumbs-up clears a stale "what was wrong" note from the live row; the
    history keeps every rating and comment (``feedback_events``, review
    step 0.4), and a new rating clears the instructor's seen mark."""
    from tutor.db.models import FeedbackEvent

    fb = db.query(MessageFeedback).filter_by(message_id=message_id).first()
    if fb is None:
        fb = MessageFeedback(message_id=message_id, student_id=student_id,
                             rating=rating, comment=comment)
        db.add(fb)
    else:
        fb.rating = rating
        if comment is not None or rating == 1:
            fb.comment = comment
        fb.seen_at = None
    db.add(FeedbackEvent(message_id=message_id, student_id=student_id, rating=rating, comment=comment))
    db.flush()
    return fb


def mark_feedback_seen(db: DBSession, feedback_id: int, seen: bool = True) -> Optional[MessageFeedback]:
    """The instructor's "seen" mark on a flag (Admin → Today); None when no such flag."""
    fb = db.get(MessageFeedback, feedback_id)
    if fb is None:
        return None
    fb.seen_at = utcnow() if seen else None
    db.flush()
    return fb


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def record_turn_event(
    db: DBSession,
    *,
    student_id: int,
    kind: str,
    goal_ids: Optional[list[str]] = None,
    envelope: Any = None,
    intent: Optional[str] = None,
    rung_used: Optional[int] = None,
    verdicts: Any = None,
    grounding_hit: Optional[bool] = None,
    sources: Optional[list[str]] = None,
    det_check: Any = None,
    model: Optional[str] = None,
    in_tokens: int = 0,
    out_tokens: int = 0,
    cache_read_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
    confidence: Optional[int] = None,
    cache_write_tokens: int = 0,
    turn_id: Optional[str] = None,
    ts: Optional[datetime.datetime] = None,
) -> TurnEvent:
    """Write one TurnEvent. The single source of the telemetry ledger.
    ``ts`` is for a row that belongs to a clock other than the process's
    (a quota hit is stamped with the time its status was read at)."""
    event = TurnEvent(
        student_id=student_id,
        ts=ts or _utcnow(),
        kind=kind,
        goal_ids_json=_dumps(goal_ids),
        envelope_json=_dumps(envelope),
        intent=intent,
        rung_used=rung_used,
        verdicts_json=_dumps(verdicts),
        grounding_hit=grounding_hit,
        # The retrieved sources the live judge saw, so the offline auditor can
        # re-judge grounding against them instead of an empty list (S: must_ground
        # artifact fix). None/empty -> stored as null.
        sources_json=_dumps(sources) if sources else None,
        det_check_json=_dumps(det_check),
        model=model,
        in_tokens=int(in_tokens or 0),
        out_tokens=int(out_tokens or 0),
        cache_read_tokens=int(cache_read_tokens or 0),
        cache_write_tokens=int(cache_write_tokens or 0),
        cost_usd=float(cost_usd or 0.0),
        latency_ms=latency_ms,
        confidence=confidence,
        turn_id=turn_id,
    )
    db.add(event)
    db.flush()
    return event


def record_turn_crash(db: DBSession, event: TurnEvent, error: str) -> None:
    """Mark a provisional chat row as the crash it ended in, touching only
    the verdicts: the intent, goals, envelope, rung and model stage 1 wrote
    stay, so the Supervision tab still shows what was asked and which model
    died (the review, step 0.9).
    Flushes; the caller commits."""
    event.verdicts_json = json.dumps({"verdict": "crash", "error": error[:300]})
    db.flush()


# Saved for a turn the site's restart cut off (a deploy): the process went
# before the reply did (R31). chat.js
# knows its opening words (INTERRUPTED_PREFIX).
INTERRUPTED_NOTE = "[No reply: the site restarted while this reply was being written. Please send it again.]"


def close_interrupted_turn(db: DBSession, turn_id: str) -> bool:
    """A turn the process lost before its reply (a restart, R31): its
    provisional chat row reads ``interrupted`` instead of staying
    ``in_progress``, and a question with no reply gets INTERRUPTED_NOTE, so
    the history does not end on it. Flushes; the caller commits. True when
    a note was added."""
    rows = db.query(TurnEvent).filter(TurnEvent.turn_id == turn_id, TurnEvent.kind == "chat").all()
    # Still open: the main reply was never saved, even when a first part was
    # (an exam's deferral or pass saves one before the tutor's answer).
    was_open = any('"in_progress"' in (r.verdicts_json or "") for r in rows)
    for row in rows:
        if '"in_progress"' in (row.verdicts_json or ""):
            row.verdicts_json = json.dumps({"verdict": "interrupted"})
    question = (db.query(Message).filter(Message.turn_id == turn_id, Message.role == "user")
                .order_by(Message.id.desc()).first())
    replied = db.query(Message.id).filter(Message.turn_id == turn_id, Message.role == "assistant").first()
    added = False
    if question is not None and (replied is None or was_open):
        # Only where the conversation still ends on this turn: a note after
        # later questions and answers would read as the newest reply, to
        # the student and to the tutor (the Stage 4 review). It is dated
        # with its question, so it opens no new conversation or day.
        later = (db.query(Message.id)
                 .filter(Message.student_id == question.student_id, Message.course == question.course,
                         Message.id > question.id, Message.turn_id != turn_id)
                 .first())
        if later is None:
            db.add(Message(student_id=question.student_id, role="assistant", content=INTERRUPTED_NOTE,
                           course=question.course, lecture_num=question.lecture_num, turn_id=turn_id,
                           created_at=question.created_at))
            added = True
    db.flush()
    return added


def close_interrupted_turns(db: DBSession, older_than: datetime.timedelta = datetime.timedelta(minutes=15),
                            limit: int = 500) -> int:
    """At boot: every chat turn still ``in_progress`` after ``older_than``
    was cut off by a restart that gave the process no time to say so (a
    deploy's SIGKILL). No turn runs that long (turn.INFLIGHT_STALE_SECONDS),
    and the floor keeps clear of turns the old instance is still finishing
    while the new one boots. Commits; returns the number closed."""
    rows = (
        db.query(TurnEvent.turn_id)
        .filter(TurnEvent.kind == "chat", TurnEvent.ts < utcnow() - older_than,
                TurnEvent.turn_id.isnot(None), TurnEvent.verdicts_json.like('%"in_progress"%'))
        .order_by(TurnEvent.ts)
        .limit(limit)
        .all()
    )
    closed = 0
    for (turn_id,) in rows:
        if turn_id:
            close_interrupted_turn(db, turn_id)
            closed += 1
    db.commit()
    return closed


def finalize_turn_event(
    db: DBSession,
    event: TurnEvent,
    *,
    goal_ids: Optional[list[str]] = None,
    envelope: Any = None,
    intent: Optional[str] = None,
    rung_used: Optional[int] = None,
    verdicts: Any = None,
    grounding_hit: Optional[bool] = None,
    sources: Optional[list[str]] = None,
    model: Optional[str] = None,
    in_tokens: int = 0,
    out_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
) -> TurnEvent:
    """Fill in a provisional TurnEvent once its turn is over.

    Plan step 2.5 (finding A13): a chat turn commits a provisional event
    before the actor runs, so the turn is on the ledger (and the daily cap)
    even if a later step fails; this writes its outcome.
    """
    event.goal_ids_json = _dumps(goal_ids)
    event.envelope_json = _dumps(envelope)
    event.intent = intent
    event.rung_used = rung_used
    event.verdicts_json = _dumps(verdicts)
    event.grounding_hit = grounding_hit
    event.sources_json = _dumps(sources) if sources else None
    event.model = model
    event.in_tokens = int(in_tokens or 0)
    event.out_tokens = int(out_tokens or 0)
    event.cache_read_tokens = int(cache_read_tokens or 0)
    event.cache_write_tokens = int(cache_write_tokens or 0)
    event.cost_usd = float(cost_usd or 0.0)
    event.latency_ms = latency_ms
    db.flush()
    return event


def add_judge_sample(
    db: DBSession,
    turn_event_id: int,
    judge: str,
    verdict: str,
    reason: str = "",
) -> JudgeSample:
    """Record one judge vote against a turn (for auditor disagreement)."""
    sample = JudgeSample(
        turn_event_id=turn_event_id,
        judge=judge,
        verdict=verdict,
        reason=reason[:2000],
    )
    db.add(sample)
    db.flush()
    return sample


# ---------------------------------------------------------------------------
# Data deletion (FERPA right to erasure, Session 26)
# ---------------------------------------------------------------------------

def erasure_scope(student_id: int) -> dict[str, Any]:
    """Map each table name to a WHERE clause selecting one student's rows.

    The scope is the ``students`` row plus, recursively, every row whose
    foreign key points at a row already in scope: a TurnEvent's JudgeSamples,
    the MessageFeedback on the student's replies, the enrollments of a class
    the student teaches. It is derived from the schema, so a table added later
    that references students is covered without anyone remembering to list it
    (finding B1).
    """
    from sqlalchemy import or_, select

    from tutor.db.models import Base

    scope: dict[str, Any] = {
        "students": Base.metadata.tables["students"].c.id == student_id,
    }
    for table in Base.metadata.sorted_tables:  # referenced tables come first
        if table.name in scope:
            continue
        conditions = [
            fk.parent.in_(select(fk.column).where(scope[fk.column.table.name]))
            for fk in table.foreign_keys
            if fk.column.table.name in scope
        ]
        if conditions:
            scope[table.name] = or_(*conditions)
    # A magic link requested before the student's first login carries only
    # the email (plan step 2.9), as does a login attempt made before the row
    # existed or one that was denied; select those by address too.
    students = Base.metadata.tables["students"]
    owner_emails = select(students.c.email).where(scope["students"])
    for name in ("magic_links", "login_attempts"):
        table = Base.metadata.tables[name]
        by_email = table.c.email.in_(owner_emails)
        scope[name] = or_(scope[name], by_email) if name in scope else by_email
    return scope


def _scrub_misconception_examples(db: DBSession, student_id: int) -> int:
    """Take this student's notes out of the cohort misconception library.

    Weekly mining (tutor/agent/misconception_mining.py) copies up to three raw
    per-student notes, ``description.strip()[:300]``, into
    ``goal_misconceptions.examples_json`` with no link back to the student, so
    erasure and anonymization remove those copies by matching the text.
    Returns the number of library rows changed.
    """
    from tutor.db.models import GoalMisconception, safe_json_loads

    notes: dict[str, set[str]] = {}
    for m in db.query(Misconception).filter_by(student_id=student_id).all():
        text = (m.description or "").strip()[:300]
        if text:
            notes.setdefault(m.goal_id, set()).add(text)
    changed = 0
    for goal_id, texts in notes.items():
        for row in db.query(GoalMisconception).filter_by(goal_id=goal_id).all():
            examples = safe_json_loads(row.examples_json, [])
            if not isinstance(examples, list):
                continue
            kept = [e for e in examples if e not in texts]
            if len(kept) != len(examples):
                row.examples_json = json.dumps(kept)
                changed += 1
    return changed


def delete_student_data(db: DBSession, student_id: int) -> dict[str, int]:
    """Erase all data for one student (a deletion request).

    Deletes every row in :func:`erasure_scope`, children before parents so the
    foreign keys Postgres enforces hold at every statement: transcripts and the
    feedback on them, telemetry and its judge samples, progress, paths, score
    predictions, enrollments, the classes the student teaches with their
    rosters, and the ``students`` row itself. The student's notes are also
    scrubbed from the cohort misconception library. Used by
    scripts/delete_student_data.py. Returns a per-table count of rows removed.
    """
    from tutor.db.models import Base

    db.flush()
    counts: dict[str, int] = {
        "goal_misconception_examples_scrubbed": _scrub_misconception_examples(db, student_id),
    }
    db.flush()
    scope = erasure_scope(student_id)
    for table in reversed(Base.metadata.sorted_tables):  # children before parents
        where = scope.get(table.name)
        if where is not None:
            counts[table.name] = db.execute(table.delete().where(where)).rowcount
    db.flush()
    return counts


# What "clear this student's conversation" removes: the transcript, the
# feedback on it, and the rolling summary. The summary matters as much as the
# messages -- clearing the transcript alone leaves it feeding the previous
# conversation into the next prompt, so the page looks blank while the tutor
# still remembers. Telemetry (turn_events), progress, graded events and the
# student row are deliberately kept: this is a reset of what was said, not an
# erasure of the student (scripts/delete_student_data.py --messages-only).
CONVERSATION_TABLES = ("messages", "message_feedback", "feedback_events", "session_summaries")


def clear_conversation(db: DBSession, student_id: int) -> dict[str, int]:
    """Delete one student's transcript, its feedback and the rolling summary,
    children before parents so the foreign keys Postgres enforces hold at
    every statement. Everything else the student has stays. Returns a
    per-table count of rows removed."""
    from tutor.db.models import Base

    db.flush()
    scope = erasure_scope(student_id)
    counts: dict[str, int] = {}
    for table in reversed(Base.metadata.sorted_tables):   # children before parents
        if table.name not in CONVERSATION_TABLES:
            continue
        where = scope.get(table.name)
        if where is not None:
            counts[table.name] = db.execute(table.delete().where(where)).rowcount
    db.flush()
    return counts


# How the one-year anonymization treats each table in erasure_scope()
# (docs/retention-policy.md): "delete" removes the rows, "redact" blanks their
# free text and keeps the structured columns, "keep" leaves them as
# de-identified telemetry under the pseudonymous student row. tests/
# test_erasure.py fails when a table in the scope is missing here, so a new
# table that references students forces an explicit decision.
ANONYMIZE_POLICY: dict[str, str] = {
    "students": "pseudonymize",
    "magic_links": "delete",
    "login_attempts": "delete",
    "sessions": "delete",
    "messages": "delete",
    "message_feedback": "delete",
    "feedback_events": "delete",
    "session_summaries": "delete",
    "exercise_attempts": "redact",
    "topics_accessed": "redact",
    "misconceptions": "redact",
    "score_predictions": "redact",
    "judge_samples": "redact",
    "turn_events": "redact",
    "graded_events": "keep",
    "mastery_attempts": "keep",
    "student_progress": "keep",
    "student_paths": "keep",
    "path_events": "keep",
    "activity_events": "redact",
    "request_errors": "keep",       # ids, paths and statuses; nothing a student wrote
    "student_days": "keep",         # ids and counts under the pseudonym
    "classes": "keep",
    "class_enrollments": "keep",
    "class_instructors": "keep",
}

# Column values written by a "redact" policy. turn_events is handled by
# _redact_turn_event_verdicts because its free text lives inside JSON.
_REDACTIONS: dict[str, dict[str, str]] = {
    "exercise_attempts": {"submission": "", "feedback": ""},
    "topics_accessed": {"topic_tag": ""},
    "misconceptions": {"description": ""},   # can quote the student's wrong model
    "score_predictions": {"context": ""},    # first 200 chars of the submission
    "judge_samples": {"reason": ""},         # can quote the draft
    "activity_events": {"label": ""},        # a control's visible text
}

# Free-text keys inside turn_events.verdicts_json: a judge reason quotes the
# draft, and an error string can echo SQL parameters (the student's message).
_FREE_TEXT_VERDICT_KEYS = ("judge_reason", "error", "grading_error")


def _redact_turn_event_verdicts(db: DBSession, student_id: int) -> int:
    """Drop the free-text keys from this student's TurnEvent verdicts, keeping
    the structured ones (verdict, rule id, scores). Returns rows changed."""
    from tutor.db.models import safe_json_loads

    changed = 0
    rows = (
        db.query(TurnEvent)
        .filter(TurnEvent.student_id == student_id, TurnEvent.verdicts_json.isnot(None))
        .all()
    )
    for te in rows:
        verdicts = safe_json_loads(te.verdicts_json, None)
        if isinstance(verdicts, dict) and any(k in verdicts for k in _FREE_TEXT_VERDICT_KEYS):
            te.verdicts_json = json.dumps(
                {k: v for k, v in verdicts.items() if k not in _FREE_TEXT_VERDICT_KEYS}
            )
            changed += 1
    return changed


def anonymize_student_data(db: DBSession, student_id: int) -> dict[str, int]:
    """De-identify one student after the retention window (docs/retention-policy.md).

    Applies :data:`ANONYMIZE_POLICY` to every table in :func:`erasure_scope`:
    auth and free-text rows are deleted, free-text columns are blanked, and
    the de-identified structured telemetry stays under this student row as a
    pseudonym, so aggregate usage data survives for tutor improvement but the
    student can no longer be identified. The student's notes are also scrubbed
    from the cohort misconception library. Counterpart to
    :func:`delete_student_data` (deletion is on request; this is the
    time-based one-year sweep).

    Idempotent: a student already anonymized (``anonymized_at`` set) or absent
    is left unchanged (returns ``{}``). Returns a per-action count.
    """
    from sqlalchemy import update

    from tutor.db.models import Base

    student = db.get(Student, student_id)
    if student is None or student.anonymized_at is not None:
        return {}

    db.flush()
    counts: dict[str, int] = {
        "goal_misconception_examples_scrubbed": _scrub_misconception_examples(db, student_id),
    }
    db.flush()
    scope = erasure_scope(student_id)
    for table in reversed(Base.metadata.sorted_tables):  # children before parents
        where = scope.get(table.name)
        if where is None:
            continue
        policy = ANONYMIZE_POLICY[table.name]
        if policy == "delete":
            counts[table.name] = db.execute(table.delete().where(where)).rowcount
        elif policy == "redact" and table.name in _REDACTIONS:
            counts[f"{table.name}_redacted"] = db.execute(
                update(table).where(where).values(**_REDACTIONS[table.name])
            ).rowcount
    counts["turn_events_redacted"] = _redact_turn_event_verdicts(db, student_id)

    # Pseudonymize the identity, keeping the row as an opaque key so the kept
    # telemetry (turn_events, graded_events, progress, paths) stays linkable
    # but not identifiable. email is unique and not null, so it gets a pseudonym.
    student.email = f"anon-{student_id}@anonymized.invalid"
    student.name = ""
    student.google_sub = None
    student.anonymized_at = _utcnow()
    counts["students_anonymized"] = 1

    db.flush()
    return counts
