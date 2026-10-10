"""What a finished lecture is, written once (plan step 5.8, findings E12, E23).

``status`` and ``generate-all --resume`` both read ``lecture_state``. A
lecture is complete when its content.md parses to at least one slide and its
Reveal deck, index.html, exists: the deck is what the app serves. script.md
is optional (it feeds only narration and video), and slides.pptx is the
retired format. A content.md with no parseable slide counts as absent, so
``--resume`` regenerates it instead of stalling on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.render.html_renderer import parse_slides


@dataclass(frozen=True)
class LectureState:
    slides: int          # slides content.md parses to; 0 when absent or unparseable
    has_html: bool
    has_script: bool
    has_pptx: bool = False

    @property
    def has_content(self) -> bool:
        return self.slides > 0

    @property
    def complete(self) -> bool:
        return self.has_content and self.has_html


def lecture_dir(output_dir: Path, num: int) -> Path:
    return Path(output_dir) / f"lecture-{num:02d}"


def lecture_state(lec_dir: Path) -> LectureState:
    content = lec_dir / "content.md"
    slides = len(parse_slides(content.read_text(errors="ignore"))) if content.exists() else 0
    return LectureState(
        slides=slides,
        has_html=(lec_dir / "index.html").exists(),
        has_script=(lec_dir / "script.md").exists(),
        has_pptx=(lec_dir / "slides.pptx").exists(),
    )


def steps_to_run(state: LectureState, *, resume: bool, pptx: bool = False) -> list[str]:
    """The generation steps a lecture needs, in order: "content", "html",
    "pptx" (only when asked for), "script".

    Without ``resume``, all of them. With it, a step whose output is present
    is skipped, and new slide content redoes everything built from it. A
    lecture ``status`` calls complete gets no content or deck step.
    """
    if not resume:
        return ["content", "html", *(["pptx"] if pptx else []), "script"]
    fresh = not state.has_content
    steps = []
    if fresh:
        steps.append("content")
    if fresh or not state.has_html:
        steps.append("html")
    if pptx and (fresh or not state.has_pptx):
        steps.append("pptx")
    if fresh or not state.has_script:
        steps.append("script")
    return steps


def parse_lecture_range(spec: str) -> list[int]:
    """"1-20", "1,4,8", or "2-3,5-7,9-20" as a sorted list without
    duplicates. Raises ValueError for text that is not a range or list, or
    for a range that runs backwards."""
    nums: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            if end < start:
                raise ValueError(f"the range {part} runs backwards")
            nums.extend(range(start, end + 1))
        else:
            nums.append(int(part))
    return sorted(set(nums))
