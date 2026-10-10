"""Classes and rosters: the only module that writes ``classes`` /
``class_enrollments``, plus the scoping rules the instructor views rely on.

It also creates ``students`` rows, which is otherwise ``routers/auth.py``'s
job: a roster is the instructor's list of who is in the course, so enrolling
pre-registers an email that has never logged in rather than skipping it. Such a
row carries only the email (blank name, ``login_count`` 0, ``last_login`` NULL);
the student's first login finds it by email and fills in the rest. ``Student``
is not one of ``GUARDED_MODELS`` (tests/test_state.py) -- the single-writer rule
covers learner state, not the identity row.

A class is a named roster of students in one course with one or more
instructors (co-instructors since 2026-10-06).
Admins create classes and staff them; an instructor reads their own, and any
instructor of a class (or an admin) can add another instructor to it or remove
one, never themselves. The lead instructor (``classes.instructor_id``, the one
the class was created for) also has a ``class_instructors`` row, and that
table is what the reads below use; removing the lead promotes the
longest-standing remaining instructor. This module deliberately mirrors
``tutor/auth/access.py`` (the whitelist CRUD) rather than
``tutor/agent/state.py``: a roster is course administration, not learner state
or telemetry, so it is outside the single-writer rule.

The scoping helpers are the security contract for every ``/instructor`` route:

    can_view_class(db, viewer, class_id)    -> the class, or None
    can_view_student(db, viewer, student)   -> True iff enrolled in one of the
                                               viewer's classes
    visible_student_ids(db, viewer)         -> every student the viewer may see

``viewer`` is the logged-in Student. Admins pass every check (they already see
everything through /admin); an instructor passes only for classes they are an
instructor of. Each helper fails closed on an unknown id.
"""

from __future__ import annotations

from tutor.courses import COURSES as _COURSES

import datetime
import logging
from typing import Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from tutor.auth.access import find_student, is_admin, is_allowed
from tutor.db.models import ClassEnrollment, ClassInstructor, CourseClass, Student

log = logging.getLogger("tutor.classes")

COURSES = _COURSES   # the one list lives in tutor.courses
NAME_MAX = 160
TERM_MAX = 64
EMAIL_MAX = 255      # students.email is String(255)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_class(
    db: DBSession,
    *,
    name: str,
    course: str,
    instructor_email: str,
    term: str = "",
    created_by: str = "",
    quarter_start: Optional[datetime.date] = None,
    quarter_end: Optional[datetime.date] = None,
) -> Optional[CourseClass]:
    """Create a class for an instructor. None when the input is unusable.

    ``quarter_start``/``quarter_end`` are the days the class runs; the weekly
    instructor email goes out only inside them (an end before the start is
    unusable too).

    Returns None (rather than raising) for a blank name, an unknown course, or
    an email with no student row, so the admin form can re-render with a
    message. Creating a class does NOT grant the role: the caller promotes the
    instructor first, which keeps "who may log in where" one decision.
    """
    name = " ".join(name.split())[:NAME_MAX]
    term = " ".join(term.split())[:TERM_MAX]
    if not name or course not in COURSES:
        return None
    if quarter_start and quarter_end and quarter_end < quarter_start:
        return None
    instructor = find_student(db, instructor_email)
    if instructor is None:
        return None
    row = CourseClass(
        name=name,
        course=course,
        term=term,
        instructor_id=instructor.id,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        created_by=created_by.strip().lower()[:255],
    )
    db.add(row)
    db.flush()
    db.add(ClassInstructor(class_id=row.id, student_id=instructor.id, added_by=row.created_by))
    db.commit()
    db.refresh(row)
    log.info("class created: id=%s %r course=%s instructor=%s by=%s",
             row.id, row.name, row.course, instructor.email, created_by)
    return row


def update_class(
    db: DBSession,
    class_id: int,
    *,
    name: Optional[str] = None,
    term: Optional[str] = None,
    instructor_email: Optional[str] = None,
    archived: Optional[bool] = None,
    weekly_report: Optional[bool] = None,
    quarter_start: Optional[datetime.date] = None,
    quarter_end: Optional[datetime.date] = None,
) -> Optional[CourseClass]:
    """Edit a class in place. None when the class (or a new instructor) is unknown.

    The quarter dates are set together: both given, they replace the pair
    (an end before the start is ignored); one alone leaves the pair as it is.

    ``course`` is intentionally not editable: the roster's metrics are
    course-scoped, so switching course mid-quarter would silently change what
    every past report meant. Make a new class instead.
    """
    row = db.get(CourseClass, class_id)
    if row is None:
        return None
    if name is not None:
        cleaned = " ".join(name.split())[:NAME_MAX]
        if cleaned:
            row.name = cleaned
    if term is not None:
        row.term = " ".join(term.split())[:TERM_MAX]
    if instructor_email is not None:
        instructor = find_student(db, instructor_email)
        if instructor is None:
            return None
        if instructor.id != row.instructor_id:
            # The admin form replaces the lead: the old lead leaves the class
            # (add them back as a co-instructor if they stay on), the new one
            # joins if not already on it.
            db.query(ClassInstructor).filter_by(class_id=class_id, student_id=row.instructor_id) \
                .delete(synchronize_session=False)
            if not _membership(db, class_id, instructor.id):
                db.add(ClassInstructor(class_id=class_id, student_id=instructor.id, added_by=""))
            row.instructor_id = instructor.id
    if archived is not None:
        row.archived = bool(archived)
    if weekly_report is not None:
        row.weekly_report = bool(weekly_report)
    if quarter_start is not None and quarter_end is not None and quarter_end >= quarter_start:
        row.quarter_start, row.quarter_end = quarter_start, quarter_end
    db.commit()
    return row


def parse_day(value: Optional[str]) -> Optional[datetime.date]:
    """A form's ``YYYY-MM-DD``, or None for blank or malformed."""
    try:
        return datetime.date.fromisoformat((value or "").strip()) if (value or "").strip() else None
    except ValueError:
        return None


def delete_class(db: DBSession, class_id: int) -> bool:
    """Delete a class and its roster rows. False when the class is unknown.

    Only the membership rows go: the students, their transcripts, and their
    progress are untouched.
    """
    row = db.get(CourseClass, class_id)
    if row is None:
        return False
    db.query(ClassEnrollment).filter_by(class_id=class_id).delete(synchronize_session=False)
    db.query(ClassInstructor).filter_by(class_id=class_id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()
    log.info("class deleted: id=%s", class_id)
    return True


def clean_email(raw: str) -> Optional[str]:
    """One pasted roster token normalized for storage, or None if unusable.

    As strict as ``access.normalize_whitelist_entry``: a local part and a dotted
    domain. Enrolling now creates the student row, so a typo would otherwise
    become a permanent account nobody can ever log into.
    """
    email = (raw or "").strip().strip("<>").lower()
    if len(email) > EMAIL_MAX:
        return None
    local, _, domain = email.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return email


def _pre_register(db: DBSession, email: str) -> Student:
    """Create the roster-only ``students`` row for an already-cleaned email.

    The one place this module writes ``students``. The row is deliberately
    bare — the first login fills in name and ``google_sub``
    (``auth._login_or_create``) — and it is logged, because an admin paste is
    the only way an account appears without its owner acting.
    """
    student = Student(email=email, name="")
    db.add(student)
    try:
        db.commit()
    except IntegrityError:
        # The same roster pasted twice, or the student logging in mid-paste:
        # read the winner's row rather than failing the whole paste (the same
        # trade as auth._insert_student).
        db.rollback()
        existing = find_student(db, email)
        if existing is None:
            raise
        return existing
    db.refresh(student)
    # The id, not the address: the process log is outside the retention and
    # erasure scope (tutor/logging_config.py).
    log.info("student pre-registered from a roster: student %s", student.id)
    return student


def enroll(db: DBSession, class_id: int, email: str) -> bool:
    """Add one student to a class by email. False if unusable or already enrolled.

    An email with no ``students`` row is pre-registered rather than skipped, so
    a roster is complete the moment the admin pastes it. Pre-registering does
    NOT grant login access: ``access.is_allowed`` (the whitelist) stays the one
    decision about who may log in, just as ``create_class`` leaves the
    instructor role to a separate admin action.
    """
    row = db.get(CourseClass, class_id)
    if row is None:
        return False
    key = clean_email(email)
    if key is None:
        return False
    student = find_student(db, key) or _pre_register(db, key)
    exists = (
        db.query(ClassEnrollment.id)
        .filter_by(class_id=class_id, student_id=student.id)
        .first()
    )
    if exists:
        return False
    db.add(ClassEnrollment(class_id=class_id, student_id=student.id))
    db.commit()
    return True


def enroll_many(db: DBSession, class_id: int, raw: str) -> tuple[int, list[str]]:
    """Enroll a pasted list of emails (comma, whitespace, or newline separated).

    Returns ``(added, skipped)``. ``skipped`` now holds only the entries that
    were already on the roster or are not usable addresses — never "no account
    yet", which ``enroll`` pre-registers — so a partial paste still tells the
    admin exactly which lines to fix, not fail as a whole.
    """
    added = 0
    skipped: list[str] = []
    for token in raw.replace(",", " ").split():
        email = clean_email(token)
        if email is None:
            skipped.append(token.strip().strip("<>").lower()[:EMAIL_MAX])
            continue
        if enroll(db, class_id, email):
            added += 1
        else:
            skipped.append(email)
    return added, skipped


def unenroll(db: DBSession, class_id: int, email: str) -> bool:
    """Remove one student from a class. False when they were not on it."""
    student = find_student(db, email)
    if student is None:
        return False
    n = (
        db.query(ClassEnrollment)
        .filter_by(class_id=class_id, student_id=student.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return bool(n)


# ---------------------------------------------------------------------------
# The instructors of a class
# ---------------------------------------------------------------------------

# What add_instructor / remove_instructor answer; the routes turn these into
# the note the page shows.
ADDED = "added"
ALREADY = "already"           # add: already an instructor of this class
REMOVED = "removed"
SELF = "self"                 # remove: nobody removes themselves
LAST = "last"                 # remove: a class keeps at least one instructor
NOT_INSTRUCTOR = "not_instructor"   # remove: not an instructor of this class
UNKNOWN = "unknown"           # no such class, or an unusable address
ADDED_NO_LOGIN = "added_no_login"   # add: on the class, but the whitelist does not let them in yet

# The sentence a page shows for each answer (the key travels in the redirect's
# query string; never the address, which is not reflected back).
NOTES: dict[str, str] = {
    ADDED: "Added. They can open this class on the instructor dashboard the next time they sign in.",
    ADDED_NO_LOGIN: ("Added, but that address is not on the login whitelist, so they cannot sign in "
                     "until an admin adds it (Admin → Access)."),
    ALREADY: "Already an instructor of this class.",
    REMOVED: "Removed from this class. Their instructor role, if any other class needs it, stays.",
    SELF: "You cannot remove yourself. Another instructor of the class, or an admin, can.",
    LAST: "A class keeps at least one instructor. Add another before removing this one.",
    NOT_INSTRUCTOR: "Not an instructor of this class.",
    UNKNOWN: "That is not a usable email address.",
}


def _membership(db: DBSession, class_id: int, student_id: int) -> Optional[ClassInstructor]:
    return db.query(ClassInstructor).filter_by(class_id=class_id, student_id=student_id).first()


def instructor_ids(db: DBSession, class_id: int) -> list[int]:
    """The ids of a class's instructors, the lead first, then by seniority.
    The lead is listed even without a membership row, so a class never
    reads as having no instructor."""
    row = db.get(CourseClass, class_id)
    if row is None:
        return []
    rows = (
        db.query(ClassInstructor.student_id)
        .filter_by(class_id=class_id)
        .order_by(ClassInstructor.created_at.asc(), ClassInstructor.id.asc())
        .all()
    )
    ids = [r[0] for r in rows if r[0] != row.instructor_id]
    return [row.instructor_id, *ids]


def class_instructors(db: DBSession, class_id: int) -> list[Student]:
    """The instructors of a class as Student rows, in :func:`instructor_ids` order."""
    ids = instructor_ids(db, class_id)
    if not ids:
        return []
    by_id = {s.id: s for s in db.query(Student).filter(Student.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id]


def is_instructor_of(db: DBSession, student_id: int, class_id: int) -> bool:
    row = db.get(CourseClass, class_id)
    if row is None:
        return False
    return row.instructor_id == student_id or _membership(db, class_id, student_id) is not None


def add_instructor(db: DBSession, class_id: int, email: str, *, added_by: str = "") -> str:
    """Make ``email`` an instructor of the class; :data:`ADDED`,
    :data:`ADDED_NO_LOGIN`, :data:`ALREADY` or :data:`UNKNOWN`.

    An address with no account is pre-registered like a roster paste, and the
    person is given the instructor role (``students.is_instructor``) so
    /instructor opens for them: being an instructor of a class is what the
    role means. Whether they may log in at all is still the whitelist
    (``access.is_allowed``), an admin's decision, which is why the answer
    says when it would keep them out. Removing them from a class does not
    take the role back (they may teach another); Admin → Classes does that.
    """
    row = db.get(CourseClass, class_id)
    key = clean_email(email)
    if row is None or key is None:
        return UNKNOWN
    student = find_student(db, key) or _pre_register(db, key)
    if is_instructor_of(db, student.id, class_id):
        return ALREADY
    db.add(ClassInstructor(class_id=class_id, student_id=student.id,
                           added_by=(added_by or "").strip().lower()[:255]))
    if not student.is_instructor:
        student.is_instructor = True
    db.commit()
    log.info("instructor added: class=%s student=%s by=%s", class_id, student.id, added_by)
    return ADDED if is_allowed(db, key) else ADDED_NO_LOGIN


def remove_instructor(db: DBSession, class_id: int, email: str, *, actor_email: str = "") -> str:
    """Take ``email`` off the class's instructors; one of :data:`REMOVED`,
    :data:`SELF`, :data:`LAST`, :data:`NOT_INSTRUCTOR`, :data:`UNKNOWN`.

    ``actor_email`` is who asked: nobody removes themselves (so a class is
    never orphaned by a mis-click, and an instructor cannot lock themselves
    out), and the last instructor stays. Removing the lead promotes the
    longest-standing co-instructor to ``instructor_id``.
    """
    row = db.get(CourseClass, class_id)
    key = clean_email(email)
    if row is None or key is None:
        return UNKNOWN
    if key == (actor_email or "").strip().lower():
        return SELF
    student = find_student(db, key)
    if student is None or not is_instructor_of(db, student.id, class_id):
        return NOT_INSTRUCTOR
    remaining = [i for i in instructor_ids(db, class_id) if i != student.id]
    if not remaining:
        return LAST
    db.query(ClassInstructor).filter_by(class_id=class_id, student_id=student.id) \
        .delete(synchronize_session=False)
    if row.instructor_id == student.id:
        row.instructor_id = remaining[0]
    db.commit()
    log.info("instructor removed: class=%s student=%s by=%s", class_id, student.id, actor_email)
    return REMOVED


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_class(db: DBSession, class_id: int) -> Optional[CourseClass]:
    return db.get(CourseClass, class_id)


def all_classes(db: DBSession, include_archived: bool = True) -> list[CourseClass]:
    """Every class, newest first (the admin Classes tab)."""
    q = db.query(CourseClass)
    if not include_archived:
        q = q.filter(CourseClass.archived == False)  # noqa: E712
    return q.order_by(CourseClass.archived.asc(), CourseClass.id.desc()).all()


def classes_for_instructor(
    db: DBSession, instructor_id: int, include_archived: bool = False
) -> list[CourseClass]:
    """The classes this instructor teaches (as lead or co-instructor), active first."""
    member_of = db.query(ClassInstructor.class_id).filter(ClassInstructor.student_id == instructor_id)
    q = db.query(CourseClass).filter(
        or_(CourseClass.instructor_id == instructor_id, CourseClass.id.in_(member_of))
    )
    if not include_archived:
        q = q.filter(CourseClass.archived == False)  # noqa: E712
    return q.order_by(CourseClass.archived.asc(), CourseClass.id.desc()).all()


def visible_classes(
    db: DBSession, viewer: Student, include_archived: bool = False
) -> list[CourseClass]:
    """Classes ``viewer`` may open: all of them for an admin, else their own."""
    if is_admin(viewer.email):
        return all_classes(db, include_archived=include_archived)
    return classes_for_instructor(db, viewer.id, include_archived=include_archived)


def class_roster(db: DBSession, class_id: int) -> list[Student]:
    """The students enrolled in a class, by email."""
    return (
        db.query(Student)
        .join(ClassEnrollment, ClassEnrollment.student_id == Student.id)
        .filter(ClassEnrollment.class_id == class_id)
        .order_by(Student.email)
        .all()
    )


def roster_ids(db: DBSession, class_id: int) -> list[int]:
    rows = db.query(ClassEnrollment.student_id).filter_by(class_id=class_id).all()
    return [r[0] for r in rows]


def enrollment_counts(db: DBSession, class_ids: Sequence[int]) -> dict[int, int]:
    """``{class_id: n_students}`` for a set of classes, in one query."""
    if not class_ids:
        return {}
    rows = (
        db.query(ClassEnrollment.class_id, func.count(ClassEnrollment.id))
        .filter(ClassEnrollment.class_id.in_(list(class_ids)))
        .group_by(ClassEnrollment.class_id)
        .all()
    )
    return {cid: n for cid, n in rows}


def classes_for_student(db: DBSession, student_id: int) -> list[CourseClass]:
    """Every class a student is enrolled in (used by the student-facing note)."""
    return (
        db.query(CourseClass)
        .join(ClassEnrollment, ClassEnrollment.class_id == CourseClass.id)
        .filter(ClassEnrollment.student_id == student_id)
        .order_by(CourseClass.id.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# Scoping (the security contract — see the module docstring)
# ---------------------------------------------------------------------------

def can_view_class(db: DBSession, viewer: Student, class_id: int) -> Optional[CourseClass]:
    """The class when ``viewer`` may open it, else None (fails closed).

    An admin may open any class. An instructor may open only a class they are
    an instructor of (the lead, or a co-instructor with a ``class_instructors``
    row) — being an instructor of some other class is never enough.
    """
    row = db.get(CourseClass, class_id)
    if row is None:
        return None
    if is_admin(viewer.email):
        return row
    return row if is_instructor_of(db, viewer.id, class_id) else None


def visible_student_ids(db: DBSession, viewer: Student) -> set[int]:
    """Every student id ``viewer`` may see through their classes.

    For an admin this is every enrolled student; note that an admin's reach is
    not limited to classes (they have /admin), this is only what the instructor
    views expose. Archived classes are included: the term being over does not
    retract the instructor's access to that term's record.
    """
    class_ids = [c.id for c in visible_classes(db, viewer, include_archived=True)]
    if not class_ids:
        return set()
    rows = (
        db.query(ClassEnrollment.student_id)
        .filter(ClassEnrollment.class_id.in_(class_ids))
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def can_view_student(db: DBSession, viewer: Student, student_id: int) -> bool:
    """True iff ``student_id`` is enrolled in a class ``viewer`` may open."""
    return student_id in visible_student_ids(db, viewer)


