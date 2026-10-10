"""Lecture delivery pages.

Serves the generated Reveal.js decks (mounted at ``/slides``) to students inside
the app: a catalog at ``/lectures`` and a framed viewer at
``/lectures/{course}/{num}``. Decks that have per-slide narration audio (produced
by ``python -m pipeline render-audio``) get a "Play narration" button that plays
each slide's clip and auto-advances — see ``pipeline/render/html_renderer.py``.
"""

from __future__ import annotations

from tutor.courses import COURSES as _COURSES, course_label

import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from tutor.templating import templates
from sqlalchemy.orm import Session as DBSession

from tutor.agent.lecture_plan import lecture_titles
from tutor.auth.deps import get_current_student, get_db
from tutor.nav import nav_context
from tutor.config import get_config
from tutor.db.models import Student

router = APIRouter(tags=["lectures"])

COURSES = _COURSES   # the one list lives in tutor.courses
_TITLE_RE = re.compile(r"(?m)^##\s+Slide\s+1\s*:\s*(.+?)\s*$")
_LEC_RE = re.compile(r"lecture-(\d+)")


def _lecture_title(lec_dir) -> str:
    """First slide's title from content.md, or a generic fallback."""
    content = lec_dir / "content.md"
    if content.exists():
        m = _TITLE_RE.search(content.read_text(errors="ignore"))
        if m:
            return m.group(1)
    return ""


def _narration(course: str, lec_dir: Path) -> tuple[bool, int | None]:
    """Whether a lecture has per-slide narration, and its length in minutes.

    The clips live under ``lecture_audio_dir`` (the persistent disk in
    production, ``output/`` locally), not beside the deck: the slide seed
    carries no audio, so looking beside the deck found nothing in production
    and every narrated lecture read "slides only". The minutes come from
    ``timing.json`` beside the deck (it travels in the slide seed) or beside
    the clips.
    """
    audio_lec = get_config()["lecture_audio_dir"] / course / lec_dir.name
    has_audio = (audio_lec / "audio" / "slide-01.mp3").exists()
    minutes: int | None = None
    for timing in (lec_dir / "timing.json", audio_lec / "timing.json"):
        if timing.exists():
            try:
                minutes = round(sum(json.loads(timing.read_text()).values()) / 60)
            except (ValueError, TypeError):
                minutes = None
            break
    return has_audio, minutes


def _catalog() -> dict[str, list[dict]]:
    """Return ``{course: [lecture_meta, ...]}`` for every rendered deck."""
    out_dir = get_config()["output_dir"]
    catalog: dict[str, list[dict]] = {}
    for course in COURSES:
        course_dir = out_dir / course
        if not course_dir.exists():
            continue
        lectures: list[dict] = []
        for lec_dir in sorted(course_dir.glob("lecture-*")):
            if not (lec_dir / "index.html").exists():
                continue  # only decks that have actually been rendered
            m = _LEC_RE.search(lec_dir.name)
            num = int(m.group(1)) if m else 0
            has_audio, minutes = _narration(course, lec_dir)
            lectures.append({
                "num": num,
                "title": _lecture_title(lec_dir) or lecture_titles(course).get(num, ""),
                "has_audio": has_audio,
                "minutes": minutes,
            })
        if lectures:
            catalog[course] = lectures
    return catalog


@router.get("/lectures", response_class=HTMLResponse)
def lectures_index(
    request: Request,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "lectures.html",
        {
            **nav_context(student, db),
            "catalog": _catalog(),
        },
    )


@router.get("/lectures/{course}/{num}", response_class=HTMLResponse)
def lecture_view(
    request: Request,
    course: str,
    num: str,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    from tutor.routers.params import bounded_int

    # Parsed here, not by FastAPI: /lectures/AST101/abc was its raw 422 JSON
    # (R13). The range check comes before the filesystem: a 300-digit number
    # made the path name too long, an OSError and a 500 (R7).
    lecture = bounded_int(num, hi=99)
    lec_dir = get_config()["output_dir"] / course / f"lecture-{lecture or 0:02d}"
    if course not in COURSES or lecture is None or not (lec_dir / "index.html").exists():
        # The lecture list with a note, status 404 (plan step 3.5, finding
        # D15: this was a bare JSON 404, at desktop width on a phone).
        where = f"Lecture {lecture} of {course_label(course)}" if course in COURSES and lecture else "That lecture"
        return templates.TemplateResponse(
            request,
            "lectures.html",
            {
                **nav_context(student, db),
                "catalog": _catalog(),
                "missing": f"{where} is not available.",
            },
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "lecture_view.html",
        {
            **nav_context(student, db, course),
            "num": lecture,
            "title": _lecture_title(lec_dir) or lecture_titles(course).get(lecture) or f"Lecture {lecture}",
            "has_audio": _narration(course, lec_dir)[0],
            "deck_url": f"/slides/{course}/lecture-{lecture:02d}/index.html",
        },
    )
