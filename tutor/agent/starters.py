"""Starter prompts for a student who does not know what to ask.

Part of the first-login UX. Each request merges:

- a personalized pair from the navigator: the student's top frontier goal in
  their track becomes a "Teach me" chip (served from the cached teach-me
  opener when one exists, so it costs no API call) and a "Practice" chip;
- a few curated prompts for the course from curriculum/starter_prompts.json,
  sampled with the student id as the seed so a reload shows the same chips
  but different students see different ones;
- one meta prompt ("What can you help me with?").

Pure data assembly: no LLM, no student prose. Fail-soft: a missing or
malformed JSON file yields only the personalized chips.
"""

from __future__ import annotations

import json
import logging
import random
from functools import lru_cache
from typing import Any, Optional

from sqlalchemy.orm import Session as DBSession
from tutor.paths import CURRICULUM_DIR

log = logging.getLogger("tutor.starters")

STARTERS_PATH = CURRICULUM_DIR / "starter_prompts.json"
CURATED_PER_REQUEST = 3
META_PER_REQUEST = 1


@lru_cache(maxsize=1)
def load_starter_prompts() -> dict[str, list[dict[str, str]]]:
    """The curated file as ``{"meta": [...], "AST101": [...], "AST102": [...]}``.

    Entries missing a label or message are dropped; any failure logs and
    returns an empty mapping so the chips degrade rather than 500.
    """
    try:
        raw = json.loads(STARTERS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("starter prompts unavailable: %s", exc)
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for key, entries in raw.items():
        if key.startswith("_") or not isinstance(entries, list):
            continue
        clean = [
            {"label": str(e["label"]).strip(), "message": str(e["message"]).strip()}
            for e in entries
            if isinstance(e, dict) and e.get("label") and e.get("message")
        ]
        if clean:
            out[key] = clean
    return out


def _personalized(student_id: int, course: str, db: DBSession) -> list[dict[str, Any]]:
    """Teach-me + practice chips for the student's top frontier goal."""
    try:
        from tutor.agent.navigator import next_goals
        from tutor.db.models import LearningGoal

        top = next_goals(student_id, course, db, n=1)
        if not top:
            return []
        goal = db.get(LearningGoal, top[0])
        if goal is None:
            return []
    except Exception as exc:  # navigator is best-effort here
        log.debug("starters: navigator unavailable: %s", exc)
        return []
    return [
        {
            "label": f"Teach me: {goal.title}",
            "message": f"Explain: {goal.title}",
            "kind": "next",
            "goal_id": goal.id,
            "lecture_num": goal.lecture_num,
            "modality": "teach",
            "opener": True,
        },
        {
            "label": f"Practice: {goal.title}",
            "message": f"Give me a practice exercise on {goal.title.lower()}.",
            "kind": "next",
            "goal_id": goal.id,
            "lecture_num": goal.lecture_num,
            "modality": "quiz",
            "opener": False,
        },
    ]


def starter_prompts(
    student_id: int,
    course: str,
    db: Optional[DBSession],
    *,
    curated_n: int = CURATED_PER_REQUEST,
    meta_n: int = META_PER_REQUEST,
) -> list[dict[str, Any]]:
    """Ordered chips: personalized first, then curated, then meta."""
    chips: list[dict[str, Any]] = []
    if db is not None:
        chips.extend(_personalized(student_id, course, db))

    data = load_starter_prompts()
    rng = random.Random(f"{student_id}:{course}")
    curated = list(data.get(course, []))
    for entry in rng.sample(curated, min(curated_n, len(curated))):
        chips.append({**entry, "kind": "course"})
    meta = list(data.get("meta", []))
    for entry in rng.sample(meta, min(meta_n, len(meta))):
        chips.append({**entry, "kind": "meta"})

    seen: set[str] = set()
    unique = []
    for c in chips:
        if c["message"] in seen:
            continue
        seen.add(c["message"])
        unique.append(c)
    return unique
