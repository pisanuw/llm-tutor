"""Generate lecture scripts from slide content using Claude.

Two backends:
- CLI (default): shells out to the local `claude` CLI in print mode, billed
  against the machine's logged-in Claude subscription. Mirrors the pattern
  already used in pipeline/generate_mastery_exams.py. Requires `claude`
  installed and logged into a Pro/Max account on this machine.
- API (opt-in via use_api=True): the original anthropic SDK streaming call,
  billed per-token against ANTHROPIC_API_KEY. Useful where no subscription
  login is available (e.g. unattended CI).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from pipeline.config import get_claude_config
from pipeline.generate._utils import course_line, extract_lecture_plan_entry
from pipeline.knowledge_base.vectorstore import query as kb_query
from pipeline.render.html_renderer import parse_slides
from tutor.courses import IDENTITY

SCRIPT_SYSTEM_PROMPT_TEMPLATE = """You are writing a detailed lecture script for {course_line}.
The script must capture the instructor's specific voice, teaching patterns, and established examples.
It is written for a junior faculty member or virtual agent who will deliver it in place of the original instructor.

The script is NOT a word-for-word read-aloud. It is a detailed talking-points guide that
tells the presenter exactly what to say and do at each point in the lecture.
Include timing estimates, interaction cues, and {demo_kind} instructions."""

SCRIPT_USER_PROMPT = """Write a complete lecture script for the lecture below.

## Lecture Plan Entry
{lecture_plan_entry}

## Slide Content
{slide_content}

## Instructor Style Guide
{style_guide}

## Examples Library (excerpt)
{examples_library}

## Relevant Transcript Excerpts (how this instructor actually taught this material)
{rag_transcripts}

## Deck Slides ({slide_count} total — write EXACTLY ONE section per slide, in this order)
{slide_manifest}

CRITICAL — SLIDE ALIGNMENT: The deck has exactly {slide_count} slides, listed above.
Write one — and only one — `<!-- ADVANCE SLIDE N -->` / `### Slide N:` section for EACH
of them, using the SAME slide number and title shown in the manifest. Do NOT merge
several slides into one "segment", do NOT skip any slide, and do NOT invent extra
slides. There must be exactly {slide_count} `<!-- ADVANCE SLIDE N -->` markers,
numbered 1..{slide_count} in order. This 1:1 mapping is required so each slide's
narration can be synced to it — a merged or missing slide breaks delivery.

Write the script in this format:

---
# Lecture {lecture_num:02d} Script: [Title]
**Estimated Duration:** [minutes, from the lecture plan's Duration]
**Instructor Prep:** [What to have ready before class{prep_examples}]

## Pre-Class (before students arrive)
- [Setup checklist]

## Opening (5 min)
[Exactly what to say/do to open class, including energy and tone notes]

## For EACH of the {slide_count} slides above, in order:
<!-- ADVANCE SLIDE N -->
### Slide N: [Title] (~X min)
**Talking Points:**
- [Point 1 - specific, not vague]
- [Point 2]

**What to say (suggested phrasing):**
"[Direct quote in the instructor's voice — this is the narration students will hear, so make it a complete, self-contained delivery of the slide, not a fragment]"

{demo_line}

**Student Interaction:** [Question to ask, pause for, expected responses]

**Transition:** "[Phrase to bridge to next slide]"

## Closing (10 min)
[Summary, preview of next lecture, assignment reminders]

## If Running Long
[What to cut or defer]

## Common Student Questions
Q: [question]
A: [answer in instructor's voice]
---

Use the transcript excerpts to ground the phrasing in how this instructor actually talks."""


def _demo_parts() -> dict[str, str]:
    """The demonstration cue in the script: live code when the course uses
    code (``code_language``), a worked example otherwise."""
    lang = IDENTITY.code_language
    if lang:
        return {
            "demo_kind": f"{lang} code demo",
            "prep_examples": " - editor open, example files ready, etc.",
            "demo_line": (f"**Code Demo:** [If applicable: exact steps to live-code the {lang}, "
                          "what to type, what mistakes to make deliberately]"),
        }
    return {
        "demo_kind": "worked-example",
        "prep_examples": " - handouts, images, props, etc.",
        "demo_line": ("**Worked Example:** [If applicable: the example to work through step by step, "
                      "what to draw or show, which wrong turn to take deliberately]"),
    }


def _load_file_safe(path: Path, max_chars: int = 3000) -> str:
    if not path.exists():
        return f"[Not found: {path.name}]"
    text = path.read_text()
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


# "## Slide 12: Why the Moon Has Phases" -> (12, "Why the Moon Has Phases")
# "<!-- ADVANCE SLIDE 9 (Eclipse Geometry) -->": the parenthetical is tolerated
_SCRIPT_MARKER_RE = re.compile(r"<!--\s*ADVANCE SLIDE\s+(\d+).*?-->")


def _parse_deck_slides(content_path: Path) -> list[tuple[int, str]]:
    """Ordered ``(slide_number, title)`` pairs from a slide ``content.md``,
    numbered by position as the deck is (plan step 5.8, E24). The markers
    are checked against 1..N, so a header that skipped a number used to
    fail every attempt."""
    if not content_path.exists():
        return []
    return [(s.number, s.heading or s.title) for s in parse_slides(content_path.read_text(errors="ignore"))]


# ---------------------------------------------------------------------------
# CLI backend (default) — billed against the local Claude subscription.
# ---------------------------------------------------------------------------

# Pure text generation: no agentic tool use needed or wanted for a script draft.
_NO_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit"
# For callers whose prompt carries the whole instruction (audit, anchors, plan).
PLAIN_SYSTEM = ("Do exactly what the request below asks and answer in the format it names. "
                "You have no tools; do not ask to use any.")
_CLI_TIMEOUT = 900  # seconds — full lecture scripts run long; generous headroom


def _claude_env() -> dict:
    """Strip the API key so the CLI can't silently fall back to metered billing."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _generate_via_cli(system_prompt: str, user_prompt: str, model: str = "sonnet",
                      timeout: int = _CLI_TIMEOUT) -> str:
    """Text from the local `claude` CLI, billed to the subscription: no tools,
    no API key in its environment, the prompt on stdin, and the whole process
    group killed on timeout. The script and teach-me generators, the goal audit,
    the anchor hooks, and the plan extender call it (plan step 6.8, finding
    E14); extract_goals and generate_mastery_exams launch the CLI the same way
    themselves. tests/test_cli_hardening.py holds every launch to it.
    Raises RuntimeError when the CLI fails or times out."""
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "text",
        "--system-prompt", system_prompt,
        "--tools", "",            # every built-in tool off, including any the CLI adds later
        "--disallowed-tools", _NO_TOOLS,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_claude_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=user_prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait()
        raise RuntimeError(f"claude CLI timed out after {timeout}s") from None

    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {stderr.strip()[:500]}")
    return stdout


# ---------------------------------------------------------------------------
# API backend (opt-in) — billed per-token against ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------

def _generate_via_api(system_prompt: str, user_prompt: str) -> str:
    import anthropic  # local import: keep this dependency optional for the CLI-only default path

    cc = get_claude_config()
    client = anthropic.Anthropic(api_key=cc["api_key"])
    script_md = ""
    # Streaming avoids the 10-minute non-streaming timeout on large scripts.
    with client.messages.stream(
        model=cc["model"],
        max_tokens=cc["max_tokens"],
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            script_md += chunk
    return script_md


def generate_script(
    lecture_num: int,
    course_config: dict,
    curriculum_dir: Path,
    instructor_profile_dir: Path,
    output_dir: Path,
    verbose: bool = False,
    use_api: bool = False,
) -> Path:
    lec_dir = output_dir / f"lecture-{lecture_num:02d}"
    content_path = lec_dir / "content.md"
    course_num = course_config["course_number"]
    course_name = course_config.get("course_name") or course_config.get("course_label") or course_num
    plan_path = curriculum_dir / f"{course_num}-lecture-plan.md"
    style_guide_path = instructor_profile_dir / "style-guide.md"
    examples_path = instructor_profile_dir / "examples-library.md"

    # Pass the FULL deck: truncating here (previously 6000 chars) silently hid
    # later slides from the model, so the script collapsed them into vague
    # "segments" that no longer aligned 1:1 with the deck.
    slide_content = _load_file_safe(content_path, max_chars=60000)
    deck_slides = _parse_deck_slides(content_path)
    slide_count = len(deck_slides)
    slide_manifest = "\n".join(f"- Slide {n}: {title}" for n, title in deck_slides) \
        or "[content.md has no parseable slides]"
    lecture_plan_entry = extract_lecture_plan_entry(plan_path, lecture_num, max_chars=2500)
    style_guide = _load_file_safe(style_guide_path, max_chars=2500)
    examples_library = _load_file_safe(examples_path, max_chars=2000)

    # RAG: find how this instructor actually taught these topics
    query_text = lecture_plan_entry[:200]
    transcript_results = kb_query("transcripts", query_text, n_results=6)
    rag_transcripts = "\n---\n".join(r["text"][:600] for r in transcript_results)
    rag_transcripts = rag_transcripts or "[No transcripts indexed yet]"

    demo = _demo_parts()
    system_prompt = SCRIPT_SYSTEM_PROMPT_TEMPLATE.format(
        course_line=course_line(course_name), demo_kind=demo["demo_kind"])
    user_prompt = SCRIPT_USER_PROMPT.format(
        prep_examples=demo["prep_examples"],
        demo_line=demo["demo_line"],
        lecture_num=lecture_num,
        lecture_plan_entry=lecture_plan_entry,
        slide_content=slide_content,
        slide_count=slide_count,
        slide_manifest=slide_manifest,
        style_guide=style_guide,
        examples_library=examples_library,
        rag_transcripts=rag_transcripts,
    )

    if verbose:
        backend = "Claude API (metered)" if use_api else "claude CLI (subscription)"
        print(f"[generate] Generating script for Lecture {lecture_num:02d} via {backend} "
              f"({slide_count} slides)...")

    generate = _generate_via_api if use_api else _generate_via_cli
    # Regenerate once if the model fails to emit one ADVANCE marker per deck slide;
    # a misaligned script silently breaks per-slide audio sync downstream.
    script_md = generate(system_prompt, user_prompt)
    if slide_count:
        markers = sorted(int(m) for m in _SCRIPT_MARKER_RE.findall(script_md))
        expected = list(range(1, slide_count + 1))
        if markers != expected:
            if verbose:
                print(f"[generate] Slide markers {markers[:3]}... ({len(markers)}) "
                      f"!= expected {slide_count}; regenerating once.")
            script_md = generate(
                system_prompt,
                user_prompt + f"\n\nIMPORTANT: your output must contain exactly "
                f"{slide_count} `<!-- ADVANCE SLIDE N -->` markers numbered 1..{slide_count}, "
                f"one per deck slide. The previous attempt did not.",
            )
            markers = sorted(int(m) for m in _SCRIPT_MARKER_RE.findall(script_md))
            if markers != expected:
                raise RuntimeError(
                    f"Script for Lecture {lecture_num:02d} is not slide-aligned: got "
                    f"{len(markers)} ADVANCE markers ({markers}), expected {slide_count} "
                    f"(1..{slide_count}). Not writing script.md."
                )

    lec_dir.mkdir(parents=True, exist_ok=True)
    out_path = lec_dir / "script.md"
    out_path.write_text(script_md)
    if verbose:
        print(f"[generate] Script written to {out_path} ({slide_count} slides aligned)")
    return out_path
