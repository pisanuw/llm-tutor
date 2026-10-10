"""The one Jinja environment every page renders with (plan step 5.4, C23).

Nine routers each built ``Jinja2Templates(directory="tutor/web/templates")``,
a path relative to the working directory, so a process started anywhere
but the repository root found no templates. The directory is anchored on
this file instead.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "web" / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def asset_url(name: str) -> str:
    """``/static/<name>`` versioned by the file's content (plan step 5.5):
    a changed script is fetched fresh after a deploy, and an unchanged one
    stays cached across deploys."""
    path = STATIC_DIR / name
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={_digest(str(path), stamp)}"


@lru_cache(maxsize=64)
def _digest(path: str, stamp: int) -> str:
    """The content hash, recomputed only when the file's mtime changes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def activity_tracking() -> dict:
    """What base.html needs to load track.js:
    whether it is on, and the heartbeat interval. Read per render, so a test
    that changes the environment sees it without a restart."""
    from tutor.config import get_config

    cfg = get_config()
    return {"on": cfg["activity_events"], "heartbeat_s": cfg["activity_heartbeat_s"]}


templates.env.globals["asset_url"] = asset_url
templates.env.globals["activity_tracking"] = activity_tracking


def _initials(name: str) -> str:
    """The tutor's initial for the avatar circle when courses.yaml names no
    image ("Astra" -> "A")."""
    words = [w for w in (name or "").split() if w[:1].isalnum()]
    return (words[0][:1] if words else "?").upper()


def _identity_context() -> dict:
    """Who the tutor is, for every page (courses.yaml ``tutor``)."""
    from tutor.courses import IDENTITY

    avatar = IDENTITY.avatar.strip().lstrip("/")
    return {
        "name": IDENTITY.name,
        "instructor": IDENTITY.instructor,
        "instructor_short": IDENTITY.instructor_short,
        "instructor_email": IDENTITY.instructor_email,
        "institution": IDENTITY.institution,
        "subject": IDENTITY.subject,
        "code_language": IDENTITY.code_language,
        "site_url": IDENTITY.site_url,
        "avatar_url": asset_url(avatar) if avatar else "",
        "initial": _initials(IDENTITY.name),
    }


def book_available() -> bool:
    """Whether a built course book is there to link (book/_book/index.html;
    tutor/main.py mounts /book only then). The book is optional."""
    from tutor.paths import ROOT

    return (ROOT / "book" / "_book" / "index.html").is_file()


def _courses_globals() -> None:
    from tutor.courses import COURSE_LIST, course_color, course_label

    templates.env.globals["tutor"] = _identity_context()
    templates.env.globals["course_list"] = COURSE_LIST
    templates.env.globals["course_label"] = course_label
    templates.env.globals["course_color"] = course_color
    templates.env.globals["book_available"] = book_available


_courses_globals()
