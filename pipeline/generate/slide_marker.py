"""Post-process script.md files to insert machine-readable slide advance markers.

Inserts ``<!-- ADVANCE SLIDE N -->`` immediately before each slide section header
so the rendering pipeline knows when to advance to the next slide frame.

Usage (via CLI):
    python -m pipeline mark-slides --course AST101
    python -m pipeline mark-slides --course AST101 --lecture 2
"""

from __future__ import annotations

import re
from pathlib import Path


# Matches "### Slide 3: Title (~8 min)" or "## Slide 3: Title"
_SLIDE_HEADER_RE = re.compile(r"^(#{2,3})\s+Slide\s+(\d+)\s*:", re.MULTILINE)

MARKER_PREFIX = "<!-- ADVANCE SLIDE"


def add_markers(script_text: str) -> str:
    """Return script_text with ``<!-- ADVANCE SLIDE N -->`` before each slide header.

    Idempotent: if the marker already precedes the header on the nearest non-blank
    line, it is not duplicated.
    """
    lines = script_text.splitlines(keepends=True)
    result: list[str] = []

    for line in lines:
        m = _SLIDE_HEADER_RE.match(line)
        if m:
            slide_num = int(m.group(2))
            # Check the last non-blank line already written
            prev_nonblank = ""
            for j in range(len(result) - 1, -1, -1):
                stripped = result[j].strip()
                if stripped:
                    prev_nonblank = stripped
                    break
            if not prev_nonblank.startswith(MARKER_PREFIX):
                result.append(f"{MARKER_PREFIX} {slide_num} -->\n")
        result.append(line)

    return "".join(result)


def mark_lecture(script_path: Path) -> int:
    """Add advance markers to one script file in place.

    Returns the number of new markers inserted (0 if already marked).
    """
    original = script_path.read_text()
    marked = add_markers(original)
    if marked == original:
        return 0
    added = marked.count(MARKER_PREFIX) - original.count(MARKER_PREFIX)
    script_path.write_text(marked)
    return added


def mark_course(output_dir: Path, lecture: int | None = None,
                lectures: list[int] | None = None) -> dict[int, int]:
    """Mark all (or one) lecture scripts for a course; ``lectures`` is the
    course's lecture numbers (``lectures`` in courses.yaml). Without it, every
    ``lecture-NN`` folder under ``output_dir`` is marked.

    Returns ``{lecture_num: markers_added}``.
    """
    if lecture:
        nums = [lecture]
    elif lectures:
        nums = list(lectures)
    else:
        nums = sorted(int(m.group(1)) for d in Path(output_dir).glob("lecture-*")
                      if (m := re.fullmatch(r"lecture-(\d+)", d.name)))
    results: dict[int, int] = {}
    for num in nums:
        script_path = output_dir / f"lecture-{num:02d}" / "script.md"
        if script_path.exists():
            results[num] = mark_lecture(script_path)
    return results
