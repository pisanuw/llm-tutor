"""Serve pre-generated "Teach me" opener explanations at runtime.

Reads the committed cache written by ``python -m pipeline generate-teachme``
(curriculum/teachme/<course>/<goal_id>.json) and returns the right variant for a
student's track and the requested depth, with graceful fallbacks. When a cache
entry exists the app renders it instantly as the first assistant turn — no API
call. On a miss the caller falls back to a normal live turn.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from tutor.paths import CURRICULUM_DIR
from tutor.courses import DEFAULT_COURSE

_TEACHME_DIR = CURRICULUM_DIR / "teachme"

DEPTHS = ("brief", "standard", "thorough")
_DEFAULT_DEPTH = "standard"


# Every generated goal id has this shape (AST101-L02-explain-moon-phases).
# Anything else is a hand-edited ?goal= and never reaches the filesystem: a
# long one raised OSError (name too long), a 500, and a "../" one read another
# course's file (finding R5).
GOAL_ID = re.compile(r"[A-Za-z0-9]{1,8}-L[0-9]{2}-[a-z0-9-]{1,100}")


def valid_goal_id(goal_id: str) -> bool:
    return bool(GOAL_ID.fullmatch(goal_id or ""))


def _load_goal(course: str, goal_id: str) -> dict | None:
    """Load one goal's cache file, or None for an id of the wrong shape."""
    if not valid_goal_id(goal_id):
        return None
    return _read_goal(course, goal_id)


@lru_cache(maxsize=2048)
def _read_goal(course: str, goal_id: str) -> dict | None:
    """Read one goal's cache file (memoized; files are static once committed)."""
    path = _TEACHME_DIR / course / f"{goal_id}.json"
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


_load_goal.cache_clear = _read_goal.cache_clear  # type: ignore[attr-defined]


def _pick_track(variants: dict, track_id: str | None) -> str | None:
    """Requested track, else the lecture-order track, else any available."""
    if not variants:
        return None
    if track_id and track_id in variants:
        return track_id
    for tid in variants:
        if tid.endswith("lecture-order"):
            return tid
    return next(iter(variants))


def get_opener(
    goal_id: str,
    course: str = DEFAULT_COURSE,
    track_id: str | None = None,
    depth: str | None = None,
) -> dict | None:
    """Return ``{opener, depth, track}`` for a goal, or None if not cached.

    Falls back across track (→ lecture-order → any) and depth (→ standard → any)
    so a partially generated cache still serves something sensible.
    """
    data = _load_goal(course, goal_id)
    if not data:
        return None
    variants = data.get("variants") or {}
    track = _pick_track(variants, track_id)
    if not track:
        return None
    by_depth = variants[track]
    want = depth if depth in DEPTHS else _DEFAULT_DEPTH
    text = by_depth.get(want) or by_depth.get(_DEFAULT_DEPTH) or next(
        (by_depth[d] for d in DEPTHS if by_depth.get(d)), None
    )
    if not text:
        return None
    used_depth = want if by_depth.get(want) else (
        _DEFAULT_DEPTH if by_depth.get(_DEFAULT_DEPTH) else
        next(d for d in DEPTHS if by_depth.get(d))
    )
    return {"opener": text, "depth": used_depth, "track": track, "title": data.get("title", "")}
