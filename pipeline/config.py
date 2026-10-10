"""Load and expose project configuration.

Two files feed the pipeline:

* ``courses.yaml`` (the content root's, read through tutor/courses.py) names
  the courses, their lecture counts, and where each one's source materials sit
  (each course's ``sources`` block). The web app reads the same file, so a
  course is declared once.
* ``config.yaml`` holds the pipeline's own knobs: the knowledge-base folders,
  Whisper, the generation model, slide counts. Every key in it has a reader
  (plan step 5.8, finding E20; a test enforces it).

Settings that must match between the index build and the app's queries, such
as the embedder and the chunk size, are code, not config: changing one side
alone would break retrieval.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from tutor import courses as _courses
from tutor.paths import CURRICULUM_DIR, PROFILE_DIR

load_dotenv()

_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _ROOT / "config.yaml"

with open(_CONFIG_PATH) as f:
    _cfg = yaml.safe_load(f)

# A course's ``sources`` keys in courses.yaml, and the key each has in the
# dict get_course_config returns. Every value is a path relative to the
# project root (an absolute path or ~ works too).
_SOURCE_KEYS = {
    "slides": "source_slides",              # folder of .pptx decks
    "assignments": "source_assignments",    # folder of .docx / .txt / .md specs
    "exams": "source_exams_dir",            # folder of exam PDFs
    "textbook": "source_textbook",          # one PDF (or a folder of PDFs)
    "documents": "source_documents",        # folder of notes/handouts: .pdf .docx .txt .md
    "topic_list": "source_topic_list",      # a Markdown list of the course's topics
    "description": "course_description",    # a text file: the catalog description
}


def _resolve(value: str) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else _ROOT / path


def get_course_config(course: str) -> dict:
    """The pipeline's view of one course in courses.yaml: its number, name,
    lecture count, output folder, and source paths (missing sources default
    to ``source-materials/<course>/<kind>``, which may simply not exist)."""
    c = _courses.get_course(course)
    if c is None:
        raise ValueError(f"Unknown course '{course}'. Available: {list(_courses.COURSES)}")
    defaults = {
        "slides": f"source-materials/{course}/slides",
        "assignments": f"source-materials/{course}/assignments",
        "exams": f"source-materials/{course}/exams",
        "textbook": f"source-materials/{course}/textbook.pdf",
        "documents": f"source-materials/{course}/documents",
        "topic_list": f"source-materials/{course}/topics.md",
        "description": f"source-materials/{course}/description.txt",
    }
    cfg: dict = {
        "course_number": c.id,
        "course_label": c.label,
        "course_name": c.full_title,
        "lectures": c.lectures,
        "output_dir": _ROOT / "output" / c.id,
    }
    for key, name in _SOURCE_KEYS.items():
        cfg[name] = _resolve(c.sources.get(key) or defaults[key])
    return cfg


def lecture_numbers(course: str) -> list[int]:
    """1 through the course's ``lectures`` in courses.yaml."""
    return list(range(1, int(get_course_config(course)["lectures"]) + 1))


def get_recordings_config() -> dict:
    rc = _cfg["recordings"].copy()
    rc["dir"] = _ROOT / rc["dir"]
    return rc


def get_whisper_config() -> dict:
    return _cfg["whisper"].copy()


def get_canvas_config() -> dict:
    cc = _cfg["canvas"].copy()
    cc["token_file"] = Path(cc["token_file"]).expanduser()
    return cc


def get_kb_config() -> dict:
    kb = _cfg["knowledge_base"].copy()
    kb["raw_dir"] = _ROOT / kb["raw_dir"]
    kb["db_dir"] = _ROOT / kb["db_dir"]
    return kb


def get_instructor_profile_config() -> dict:
    """Where the instructor profile is written and read (the content root's
    instructor-profile/, tutor/paths.py)."""
    return {"output_dir": PROFILE_DIR}


def get_curriculum_config() -> dict:
    """Where lecture plans, goals and tracks live (the content root's
    curriculum/, tutor/paths.py)."""
    return {"output_dir": CURRICULUM_DIR}


def get_claude_config() -> dict:
    cc = _cfg["claude"].copy()
    cc["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return cc


def get_slide_generation_config() -> dict:
    return _cfg["slide_generation"].copy()
