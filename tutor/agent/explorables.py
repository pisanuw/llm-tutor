"""Interactive explorables for threshold concepts (Engram integration, Phase 4).

An *explorable* is a small, self-contained, offline HTML manipulable for a
learning goal the curriculum flags as a "threshold" concept (the phases of the
Moon, the cause of the seasons, ...). They are authored offline and shipped as
static files under the repo's ``explorables/`` directory, one per goal id:

    explorables/<goal_id>.html

served at ``/explorables/<goal_id>.html``. This module is the single place that
maps a goal id to its explorable URL, gated on the file actually existing, so a
flagged-but-not-yet-authored goal surfaces no dead link. Pure + filesystem-read
only; no LLM, no writes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from tutor.paths import EXPLORABLES_DIR as _EXPLORABLES_DIR

# Repo-root ``explorables/`` — resolved relative to this file so it works both in
# the dev tree and the deployed container.
EXPLORABLES_DIR: Path = _EXPLORABLES_DIR

URL_PREFIX = "/explorables"

# Goal ids are curriculum slugs like "AST101-L02-explain-moon-phases"; anything else is
# rejected so a caller can never coax a path-traversal filename through here.
_SAFE_GOAL_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def explorable_path(goal_id: str, base_dir: Optional[Path] = None) -> Optional[Path]:
    """Path to the explorable HTML for ``goal_id``, or None when absent/unsafe."""
    if not goal_id or not _SAFE_GOAL_ID.match(goal_id):
        return None
    path = (base_dir or EXPLORABLES_DIR) / f"{goal_id}.html"
    return path if path.is_file() else None


def has_explorable(goal_id: str, base_dir: Optional[Path] = None) -> bool:
    """True when a served explorable file exists for ``goal_id``."""
    return explorable_path(goal_id, base_dir) is not None


def explorable_url(goal_id: str, base_dir: Optional[Path] = None) -> Optional[str]:
    """Public URL for ``goal_id``'s explorable, or None when there is no file."""
    if explorable_path(goal_id, base_dir) is None:
        return None
    return f"{URL_PREFIX}/{goal_id}.html"
