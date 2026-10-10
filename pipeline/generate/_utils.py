"""Shared helpers for the pipeline's generators and prompts.

Every prompt the pipeline sends names the instructor, the subject and the
course from ``IDENTITY`` and ``COURSE_LIST`` in courses.yaml
(tutor/courses.py), never from constants, so a new set of courses changes
every prompt at once. A prompt asks for code only when
``tutor.code_language`` is set, and then in that language; otherwise it asks
for worked examples and short-answer problems.
"""

from __future__ import annotations

import re
from pathlib import Path

from tutor.courses import COURSE_LIST, IDENTITY, get_course


def extract_lecture_plan_entry(plan_path: Path, lecture_num: int, max_chars: int = 3000) -> str:
    """Return the plan entry for a specific lecture, truncated to max_chars."""
    if not plan_path.exists():
        return f"Lecture {lecture_num:02d}: No plan found."
    text = plan_path.read_text()
    pattern = rf"## Lecture {lecture_num:02d}:.*?(?=## Lecture \d{{2}}:|$)"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(0).strip()[:max_chars]
    return f"Lecture {lecture_num:02d}: See curriculum plan."


def collect_stream(stream) -> str:
    """Consume a messages.stream() context and return the accumulated text."""
    text = ""
    for chunk in stream.text_stream:
        text += chunk
    return text


# ---------------------------------------------------------------------------
# Prompt pieces from courses.yaml
# ---------------------------------------------------------------------------

def at_institution() -> str:
    """``" at Example College"``, or ``""`` when courses.yaml names none."""
    return f" at {IDENTITY.institution}" if IDENTITY.institution else ""


def course_title(course: str) -> str:
    """``"AST 101: The Night Sky"`` for a course id; the id itself when it is
    not in courses.yaml."""
    c = get_course(str(course))
    return c.full_title if c else str(course)


def all_course_titles() -> str:
    """Every course: ``"AST 101: The Night Sky; AST 102: Stars and Galaxies"``."""
    return "; ".join(c.full_title for c in COURSE_LIST)


def uses_code() -> bool:
    """True when students write code in this subject (``code_language``)."""
    return bool(IDENTITY.code_language)


def course_line(course_name: str) -> str:
    """The course, its instructor and its students in one phrase, for the top
    of a system prompt: "Dr. Alex Morgan's AST 101: The Night Sky course at
    Example College (introductory astronomy, for first-year undergraduates
    ...)"."""
    return (f"{IDENTITY.instructor}'s {course_name} course{at_institution()} "
            f"({IDENTITY.subject}, for {IDENTITY.audience})")
