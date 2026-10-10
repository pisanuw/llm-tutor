"""Who the tutor is and which courses it serves, read from ``courses.yaml``.

``courses.yaml`` (at the content root, see tutor/paths.py) is the one file an
instructor edits to make the tutor theirs: the tutor's name, the instructor it
speaks for, the subject, the time zone, and the list of courses. The web app
reads it here; the content pipeline reads the same file through
pipeline/config.py (the ``sources`` block of each course is the pipeline's).

Everything is read once, at import, and validated: a malformed file stops the
app at boot with a message that names the field, never later in a student's
turn. ADDING-KNOWLEDGE.md documents every field.

Plan step 2.9 (findings C15, B24): ``course`` arrives unvalidated from forms,
query strings, and JSON bodies and lands in ``varchar(8)`` columns. Every
endpoint that takes a course checks it here and answers 422 for anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from tutor.paths import COURSES_FILE

# A course id is a file name, a URL segment, a goal-id prefix and a
# varchar(8) column: letters and digits only, at most eight.
_COURSE_ID = re.compile(r"^[A-Za-z0-9]{1,8}$")

# Accent colors for courses, in order: the first course gets the first. Hex,
# so pages and the learning map can use them inline without a CSS rebuild.
PALETTE: tuple[dict[str, str], ...] = (
    {"name": "violet", "strong": "#6d28d9", "mid": "#7c3aed", "light": "#c4b5fd", "wash": "#ede9fe"},
    {"name": "cyan", "strong": "#0e7490", "mid": "#0891b2", "light": "#a5f3fc", "wash": "#cffafe"},
    {"name": "emerald", "strong": "#047857", "mid": "#059669", "light": "#a7f3d0", "wash": "#d1fae5"},
    {"name": "amber", "strong": "#b45309", "mid": "#d97706", "light": "#fde68a", "wash": "#fef3c7"},
    {"name": "rose", "strong": "#be123c", "mid": "#e11d48", "light": "#fecdd3", "wash": "#ffe4e6"},
    {"name": "sky", "strong": "#0369a1", "mid": "#0284c7", "light": "#bae6fd", "wash": "#e0f2fe"},
)


class CourseConfigError(RuntimeError):
    """``courses.yaml`` is missing or does not describe a tutor and its courses."""


@dataclass(frozen=True)
class Identity:
    """Who the tutor is. Every field reaches students: page titles, the
    greeting, emails, and the system prompt."""
    name: str                 # what students call the tutor ("Astra")
    instructor: str           # the instructor it speaks for ("Dr. Alex Morgan")
    instructor_short: str     # how students address them ("Dr. Morgan")
    instructor_email: str     # where "email your instructor" points; may be ""
    institution: str          # may be ""
    subject: str              # a noun phrase: "introductory astronomy"
    audience: str             # who the students are, for grader prompts
    timezone: str             # IANA zone for "today", quotas and reports
    code_language: str        # "C++" when students submit code; "" otherwise
    site_url: str             # the public address, for emails and the help page; may be ""
    avatar: str               # an image under tutor/web/static, or "" for initials


@dataclass(frozen=True)
class Course:
    id: str                   # "AST101": file names, URLs, goal-id prefix
    label: str                # "AST 101": how the course is named on pages
    title: str                # "The Night Sky"
    lectures: int             # lectures 1..N exist in the plan and the decks
    topics: str = ""          # one line for the course picker
    note: str = ""            # optional extra line under the picker
    sources: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def full_title(self) -> str:
        return f"{self.label}: {self.title}" if self.title else self.label


def _text(raw: dict, key: str, where: str, *, required: bool = True, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, (str, int, float)):
        raise CourseConfigError(f"{where}.{key} must be text, got {type(value).__name__}")
    value = str(value).strip()
    if required and not value:
        raise CourseConfigError(f"{where}.{key} is required in {COURSES_FILE}")
    return value


def _load(path: Path) -> tuple[Identity, tuple[Course, ...]]:
    if not path.is_file():
        raise CourseConfigError(
            f"No courses file at {path}. Copy the example (courses.yaml) or set "
            "TUTOR_CONTENT_DIR to the folder that holds yours.")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise CourseConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CourseConfigError(f"{path} must hold a mapping with 'tutor' and 'courses'")

    t = data.get("tutor") or {}
    if not isinstance(t, dict):
        raise CourseConfigError("tutor: must be a mapping")
    tz = _text(t, "timezone", "tutor", required=False, default="UTC")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception as exc:   # ZoneInfoNotFoundError, ValueError
        raise CourseConfigError(f"tutor.timezone {tz!r} is not an IANA time zone") from exc
    instructor = _text(t, "instructor", "tutor")
    identity = Identity(
        name=_text(t, "name", "tutor"),
        instructor=instructor,
        instructor_short=_text(t, "instructor_short", "tutor", required=False) or instructor,
        instructor_email=_text(t, "instructor_email", "tutor", required=False),
        institution=_text(t, "institution", "tutor", required=False),
        subject=_text(t, "subject", "tutor"),
        audience=_text(t, "audience", "tutor", required=False, default="students"),
        timezone=tz,
        code_language=_text(t, "code_language", "tutor", required=False),
        site_url=_text(t, "site_url", "tutor", required=False).rstrip("/"),
        avatar=_text(t, "avatar", "tutor", required=False),
    )

    raw_courses = data.get("courses")
    if not isinstance(raw_courses, list) or not raw_courses:
        raise CourseConfigError("courses: must be a list with at least one course")
    courses: list[Course] = []
    for i, c in enumerate(raw_courses):
        where = f"courses[{i}]"
        if not isinstance(c, dict):
            raise CourseConfigError(f"{where} must be a mapping")
        cid = _text(c, "id", where)
        if not _COURSE_ID.match(cid):
            raise CourseConfigError(
                f"{where}.id {cid!r}: use 1 to 8 letters or digits (no spaces or dashes); "
                "it becomes a file name, a URL segment and the prefix of every goal id")
        if any(x.id == cid for x in courses):
            raise CourseConfigError(f"{where}.id {cid!r} appears twice")
        try:
            lectures = int(c.get("lectures", 0))
        except (TypeError, ValueError) as exc:
            raise CourseConfigError(f"{where}.lectures must be a whole number") from exc
        if not 1 <= lectures <= 99:
            raise CourseConfigError(f"{where}.lectures must be between 1 and 99")
        sources = c.get("sources") or {}
        if not isinstance(sources, dict):
            raise CourseConfigError(f"{where}.sources must be a mapping")
        courses.append(Course(
            id=cid,
            label=_text(c, "label", where, required=False) or cid,
            title=_text(c, "title", where, required=False),
            lectures=lectures,
            topics=_text(c, "topics", where, required=False),
            note=_text(c, "note", where, required=False),
            sources=dict(sources),
        ))
    return identity, tuple(courses)


IDENTITY, COURSE_LIST = _load(COURSES_FILE)

# The one list of course ids (plan step 5.4, C23). FastAPI is imported only by
# the validators, so agent and pipeline code can import COURSES freely.
COURSES: tuple[str, ...] = tuple(c.id for c in COURSE_LIST)
_BY_ID: dict[str, Course] = {c.id: c for c in COURSE_LIST}

# The course a page, a tool call or a function falls back on when none is
# given: the first one listed in courses.yaml.
DEFAULT_COURSE: str = COURSES[0]


def default_course() -> str:
    """The course a page or tool falls back on: the first one listed."""
    return COURSES[0]


def get_course(course_id: str) -> Optional[Course]:
    return _BY_ID.get(course_id)


def course_label(course_id: Any) -> str:
    """"AST 101" for "AST101"; an unknown id as given (an old row's course)."""
    c = _BY_ID.get(str(course_id) if course_id is not None else "")
    return c.label if c else str(course_id or "?")


def lecture_numbers(course_id: str) -> list[int]:
    """1 through the course's lecture count; empty for an unknown course."""
    c = _BY_ID.get(course_id)
    return list(range(1, c.lectures + 1)) if c else []


def course_color(course_id: Any) -> dict[str, str]:
    """The course's accent colors (``PALETTE``), by its position in the list."""
    try:
        index = COURSES.index(str(course_id))
    except ValueError:
        index = 0
    return PALETTE[index % len(PALETTE)]


def course_context() -> str:
    """One phrase that tells a grader or classifier what is being taught:
    "introductory astronomy (AST 101: The Night Sky; AST 102: Stars and
    Galaxies) at Example College, for first-year undergraduates"."""
    titles = "; ".join(c.full_title for c in COURSE_LIST)
    at = f" at {IDENTITY.institution}" if IDENTITY.institution else ""
    who = f", for {IDENTITY.audience}" if IDENTITY.audience and IDENTITY.audience != "students" else ""
    return f"{IDENTITY.subject} ({titles}){at}{who}"


def match_course(value: Any) -> Optional[str]:
    """The course id a loose mention names, or None: the id itself in any
    case ("ast101"), or its label with or without spaces ("AST 101")."""
    text = re.sub(r"\s+", "", str(value if value is not None else "")).lower()
    if not text:
        return None
    for c in COURSE_LIST:
        if text in (c.id.lower(), re.sub(r"\s+", "", c.label).lower()):
            return c.id
    return None


def require_course(value) -> str:
    """``value`` as a course code, or a 422."""
    from fastapi import HTTPException

    course = str(value if value is not None else "").strip()
    if course not in COURSES:
        raise HTTPException(status_code=422, detail=f"Unknown course: {course[:16]!r}")
    return course


def optional_course(value) -> Optional[str]:
    """None when no course was given (the caller picks a default), else as
    :func:`require_course`."""
    if value is None or str(value).strip() == "":
        return None
    return require_course(value)
