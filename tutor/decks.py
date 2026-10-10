"""Which lecture decks are on disk, for every place that offers one.

The tutor's citations checked the deck existed before naming it
(``tools._slide_deck_url``), while the What's-next card, the map's Slides
links and the chat's lecture picker built ``/slides/<course>/lecture-NN/``
URLs from a goal's anchor with no check, so a lecture missing from the seed
was a raw 404 in a new tab (finding F3, step 0.3). One answer, read from the
output directory the ``/slides`` mount serves, cached for a minute: the
directory changes only at boot.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from tutor.config import get_config
from tutor.courses import COURSES, lecture_numbers
_TTL_S = 60.0
_cache: dict[str, tuple[float, list[int]]] = {}


def _reset_for_tests() -> None:
    _cache.clear()


def deck_dir(course: str, lecture_num: int, output_dir: Optional[Path] = None) -> Path:
    return (output_dir or get_config()["output_dir"]) / course / f"lecture-{lecture_num:02d}"


def deck_exists(course: str, lecture_num: Optional[int], output_dir: Optional[Path] = None) -> bool:
    """Whether the Reveal deck for this lecture is on disk."""
    if course not in COURSES or not lecture_num or lecture_num < 1:
        return False
    return (deck_dir(course, lecture_num, output_dir) / "index.html").is_file()


def deck_url(course: str, lecture_num: Optional[int], output_dir: Optional[Path] = None) -> Optional[str]:
    """``/slides/<course>/lecture-NN/index.html`` when the deck exists, else None."""
    if not deck_exists(course, lecture_num, output_dir):
        return None
    return f"/slides/{course}/lecture-{lecture_num:02d}/index.html"


def available_lectures(course: str, output_dir: Optional[Path] = None) -> list[int]:
    """The lecture numbers with a deck, ascending; cached briefly per course."""
    if course not in COURSES:
        return []
    key = f"{output_dir or ''}|{course}"
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return list(hit[1])
    base = (output_dir or get_config()["output_dir"]) / course
    found: list[int] = []
    if base.is_dir():
        for child in sorted(base.glob("lecture-*")):
            tail = child.name.rsplit("-", 1)[-1]
            if tail.isdigit() and (child / "index.html").is_file():
                found.append(int(tail))
    _cache[key] = (now + _TTL_S, found)
    return list(found)


def available_decks(output_dir: Optional[Path] = None) -> dict[str, list[int]]:
    """``{course: [lecture numbers with a deck]}`` for the page scripts."""
    return {course: available_lectures(course, output_dir) for course in COURSES}


def missing_lectures(course: str, output_dir: Optional[Path] = None,
                     expected: Optional[int] = None) -> list[int]:
    """The lecture numbers of the plan (courses.yaml ``lectures``, or
    ``expected``) with no deck on disk."""
    have = set(available_lectures(course, output_dir))
    numbers = range(1, expected + 1) if expected is not None else lecture_numbers(course)
    return [n for n in numbers if n not in have]
