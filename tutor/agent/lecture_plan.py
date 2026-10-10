"""Lecture titles from the committed curriculum plans.

``curriculum/<course>-lecture-plan.md`` is the same file
``tools.get_lecture_outline`` reads; its ``## Lecture NN: Title`` headings give
every lecture a human title even where ``output/`` (the rendered decks) is
absent. Used by the chat lecture dropdown and as the lectures catalog's
fallback title. No LLM, no DB.
"""

from __future__ import annotations

import re
from functools import lru_cache

from tutor.courses import COURSES  # the one list (plan step 5.4)
from tutor.paths import lecture_plan_path

_HEADING = re.compile(r"^## Lecture (\d+):\s*(.+?)\s*$", re.MULTILINE)


@lru_cache(maxsize=32)
def lecture_titles(course: str) -> dict[int, str]:
    """``{lecture_num: title}`` for a course; empty when the plan is missing."""
    path = lecture_plan_path(course)
    if not path.exists():
        return {}
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return {}
    return {int(num): title for num, title in _HEADING.findall(text)}


def all_lecture_titles() -> dict[str, dict[int, str]]:
    """Titles for every course, keyed by course string (for the chat page)."""
    return {course: lecture_titles(course) for course in COURSES}
