"""Pre-generate cached "Teach me" opener explanations, offline, via the Claude
subscription CLI (no metered API).

When a student clicks "Teach me" on a mastery-map node, the app can serve a
pre-vetted opening explanation instantly instead of firing a live, cold-cache
supervised turn (the app's slowest, most common entry action). This module
generates those openers offline.

For each learning goal we produce, per track and at three explanation depths,
a self-contained opener in the instructor's voice (the persona files and
``tutor.instructor`` in courses.yaml), grounded in the same ChromaDB sources a
live ``find_topic`` turn would retrieve. Output is one JSON file per
goal:

    curriculum/teachme/<course>/<goal_id>.json
    {
      "goal_id": "AST101-L02-explain-moon-phases", "title": "...", "course": "AST101",
      "variants": { "<track_id>": { "brief": "...", "standard": "...", "thorough": "..." } }
    }

curriculum/ is committed, so generate-once-serve-everywhere: production never
calls the API for these openers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline.generate._utils import at_institution
from pipeline.generate.script_generator import _generate_via_cli
from pipeline.knowledge_base.vectorstore import query as kb_query
from tutor.courses import IDENTITY
from tutor.paths import CURRICULUM_DIR, PERSONA_DIR as _PERSONA_DIR

ROOT = Path(__file__).resolve().parents[2]
PERSONA_DIR = _PERSONA_DIR
TEACHME_DIR = CURRICULUM_DIR / "teachme"

DEPTHS = ("brief", "standard", "thorough")

# Collections worth grounding a concept opener against (a subset of the app's
# find_topic list — the ones that carry expository content, not chat logs).
_GROUNDING_COLLECTIONS = ("slides", "textbook", "transcripts", "documents")

_DEPTH_GUIDANCE = {
    "brief": "2-4 sentences. A quick refresher for someone who has seen this "
             "before: the essence and why it matters. No worked example.",
    "standard": "~150-250 words. A complete but concise explanation with ONE "
                "concrete example. The default a student gets.",
    "thorough": "~300-450 words, from first principles: motivation, the core "
                "idea, a worked example, and 1-2 common pitfalls or misconceptions.",
}

# Track flavor by id suffix. Falls back to the track's own description.
_TRACK_FLAVOR = {
    "lecture-order": "Present it as it is taught in lecture — balanced and neutral.",
    "foundations-first": "Lead with intuition and fundamentals before any mechanics.",
    "code-first": "Lead with a concrete code example, then explain what it does and why.",
    "exam-prep": "Be concise and emphasize what is commonly tested and the typical mistakes.",
}

_SYSTEM_PROMPT = """You are {instructor}, who teaches {subject}{institution}, giving a student a \
focused opening explanation of a single concept, triggered when they click a \
"Teach me" button on a concept map. Write in {instructor_short}'s voice and \
teaching style (below). This is the FIRST thing the \
student reads about this concept, so teach it directly — do not ask what they \
want, do not greet at length, do not list what you'll cover. Just teach the \
concept clearly and get them oriented. Use Markdown; {snippets}

{persona}"""


def system_prompt(persona: str) -> str:
    """The opener's system prompt, in the voice of the instructor courses.yaml names."""
    snippets = (f"short {IDENTITY.code_language} snippets are welcome where they clarify."
                if IDENTITY.code_language else
                "a short worked example is welcome where it clarifies.")
    return _SYSTEM_PROMPT.format(
        instructor=IDENTITY.instructor, instructor_short=IDENTITY.instructor_short,
        subject=IDENTITY.subject, institution=at_institution(), snippets=snippets,
        persona=persona)


_USER_TEMPLATE = """Concept to explain: {title}
What the student should be able to do: {description}
Why it matters (hook): {hook}
Where it is taught: {location}

Grounding excerpts from the course materials (use these to stay faithful to how \
this course teaches it; do not quote them verbatim, synthesize):
{grounding}

Audience track: {track_title} — {track_desc}
Tone for this track: {track_flavor}

Produce THREE versions of the opener at increasing depth. Output EXACTLY these \
three sections, each introduced by its delimiter line on its own line, and \
nothing before the first delimiter:

===BRIEF===
{brief_g}

===STANDARD===
{standard_g}

===THOROUGH===
{thorough_g}
"""

_DELIM_RE = re.compile(r"^===(BRIEF|STANDARD|THOROUGH)===\s*$", re.M)


def _load_persona() -> str:
    parts = []
    for name, hdr in (
        ("persona-distilled.md", "## CORE PERSONA RULES"),
        ("communication.md", "## COMMUNICATION STYLE"),
        ("teaching.md", "## TEACHING PHILOSOPHY"),
    ):
        p = PERSONA_DIR / name
        if p.exists():
            parts.append(f"{hdr}\n\n{p.read_text().strip()}")
    return "\n\n".join(parts)


def _track_flavor(track_id: str, description: str) -> str:
    for suffix, flavor in _TRACK_FLAVOR.items():
        if track_id.endswith(suffix):
            return flavor
    return description or "Balanced and neutral."


def _goal_location(goal: dict) -> str:
    bits = []
    sa = goal.get("slide_anchor")
    if sa:
        bits.append(f"Lecture {sa.get('lecture')}, slide {sa.get('slide')} "
                    f"(\"{sa.get('title', '')}\")")
    ba = goal.get("book_anchor")
    if ba:
        bits.append(f"Book §{ba.get('section_id', '')} ({ba.get('title', '')})")
    return "; ".join(bits) or f"Lecture {goal.get('lecture', '?')}"


def _grounding(goal: dict, n_per_collection: int = 2, max_chars: int = 500) -> str:
    """Retrieve a few expository excerpts for the goal from ChromaDB."""
    query_text = f"{goal['title']}. {goal['description']}"
    excerpts: list[str] = []
    for coll in _GROUNDING_COLLECTIONS:
        for r in kb_query(coll, query_text, n_results=n_per_collection):
            text = (r.get("text") or "").strip().replace("\n", " ")
            if text:
                excerpts.append(f"[{coll}] {text[:max_chars]}")
    return "\n---\n".join(excerpts) or "[No indexed grounding found]"


def _parse_variants(raw: str) -> dict[str, str] | None:
    """Split a CLI response into {depth: text}. Returns None if not all three
    sections are present (caller can retry)."""
    matches = list(_DELIM_RE.finditer(raw))
    if len(matches) < 3:
        return None
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        depth = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out[depth] = raw[start:end].strip()
    if not all(out.get(d) for d in DEPTHS):
        return None
    return out


def generate_goal_openers(
    goal: dict,
    tracks: list[dict],
    persona: str,
    model: str = "sonnet",
    verbose: bool = True,
) -> dict[str, dict[str, str]]:
    """Return ``{track_id: {depth: text}}`` for one goal across the given tracks."""
    grounding = _grounding(goal)
    location = _goal_location(goal)
    system = system_prompt(persona)
    variants: dict[str, dict[str, str]] = {}

    for track in tracks:
        track_id = track["id"]
        user_prompt = _USER_TEMPLATE.format(
            title=goal["title"],
            description=goal["description"],
            hook=goal.get("hook", ""),
            location=location,
            grounding=grounding,
            track_title=track.get("title", track_id),
            track_desc=track.get("description", ""),
            track_flavor=_track_flavor(track_id, track.get("description", "")),
            brief_g=_DEPTH_GUIDANCE["brief"],
            standard_g=_DEPTH_GUIDANCE["standard"],
            thorough_g=_DEPTH_GUIDANCE["thorough"],
        )
        parsed = None
        for attempt in range(2):
            raw = _generate_via_cli(system, user_prompt, model=model)
            parsed = _parse_variants(raw)
            if parsed:
                break
            if verbose:
                print(f"      [{track_id}] malformed output, retrying ({attempt + 1})")
        if not parsed:
            raise RuntimeError(
                f"Could not parse 3 depth sections for goal {goal['id']} track {track_id}"
            )
        variants[track_id] = parsed
        if verbose:
            sizes = " ".join(f"{d}:{len(parsed[d])}" for d in DEPTHS)
            print(f"      [{track_id}] ok ({sizes})")
    return variants


def generate_teachme(
    goals: list[dict],
    tracks: list[dict],
    course: str,
    force: bool = False,
    model: str = "sonnet",
    verbose: bool = True,
) -> dict:
    """Generate + write opener caches for the given goals. Returns a summary."""
    persona = _load_persona()
    out_dir = TEACHME_DIR / course
    out_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    failed: list[str] = []

    for i, goal in enumerate(goals, 1):
        out_path = out_dir / f"{goal['id']}.json"
        if out_path.exists() and not force:
            skipped += 1
            if verbose:
                print(f"[{i}/{len(goals)}] {goal['id']}: cached, skip")
            continue
        if verbose:
            print(f"[{i}/{len(goals)}] {goal['id']}: {goal['title']}")
        # One goal's failure (a transient CLI hang, a parse miss) must not abort
        # the whole batch — record it and keep going; a re-run fills the gaps.
        try:
            variants = generate_goal_openers(goal, tracks, persona, model=model, verbose=verbose)
        except Exception as exc:
            failed.append(goal["id"])
            if verbose:
                print(f"      !! FAILED {goal['id']}: {exc}")
            continue
        out_path.write_text(json.dumps({
            "goal_id": goal["id"],
            "title": goal["title"],
            "course": course,
            "variants": variants,
        }, indent=2) + "\n")
        written += 1

    if failed and verbose:
        print(f"\n{len(failed)} goal(s) failed (re-run to retry): {', '.join(failed)}")
    return {"written": written, "skipped": skipped, "total": len(goals),
            "failed": failed, "out_dir": out_dir}
