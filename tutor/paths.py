"""Where the course content lives.

Everything an instructor writes to make the tutor theirs sits under one
content root: ``courses.yaml``, ``curriculum/``, ``persona/``,
``instructor-profile/`` and ``explorables/``. The root is the repository by
default; ``TUTOR_CONTENT_DIR`` moves it (a path relative to the repository is
resolved against it). The test suite points it at ``tests/fixtures/content``,
so replacing the example course with your own never breaks a test.

Generated artifacts (``output/``, ``knowledge-base/``) stay at the repository
root; see tutor/config.py.

The paths are read once, at import. Set ``TUTOR_CONTENT_DIR`` before the
first ``import tutor...`` (tests/conftest.py does).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# TUTOR_CONTENT_DIR may be set in .env; this module can be imported before
# tutor/config.py loads it (PYTHON_DOTENV_DISABLED still switches it off).
load_dotenv(ROOT / ".env")


def _content_root() -> Path:
    raw = os.getenv("TUTOR_CONTENT_DIR", "").strip()
    if not raw:
        return ROOT
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


CONTENT_DIR = _content_root()
COURSES_FILE = CONTENT_DIR / "courses.yaml"
CURRICULUM_DIR = CONTENT_DIR / "curriculum"
PERSONA_DIR = CONTENT_DIR / "persona"
PROFILE_DIR = CONTENT_DIR / "instructor-profile"
EXPLORABLES_DIR = CONTENT_DIR / "explorables"


def lecture_plan_path(course: str) -> Path:
    """``curriculum/<course>-lecture-plan.md``: the one lecture plan per
    course that the outline tool, the lecture titles, goal extraction and the
    slide and script generators all read."""
    return CURRICULUM_DIR / f"{course}-lecture-plan.md"


def topic_map_path(course: str) -> Path:
    """``curriculum/<course>-topic-map.json`` (written by ``pipeline map``)."""
    return CURRICULUM_DIR / f"{course}-topic-map.json"
