"""What the shared site header needs (templates/_nav.html, plan step 3.1).

Every signed-in page passes the same four values, so the header shows the
same links everywhere and the Review link opens the right course.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor.courses import COURSES, DEFAULT_COURSE


def nav_context(student, db: DBSession, course: Optional[str] = None) -> dict:
    """``student``, the admin and instructor flags, and ``course``: the given
    course when valid, else the student's current one, else the first course
    in courses.yaml."""
    from tutor.agent.navigator import current_course
    from tutor.auth.access import is_admin, is_instructor

    if course not in COURSES:
        course = (current_course(student.id, db) if student is not None else None) or DEFAULT_COURSE
    email = student.email if student is not None else None
    return {
        "student": student,
        "is_admin": is_admin(email),
        "is_instructor": is_instructor(db, email),
        "course": course,
    }
