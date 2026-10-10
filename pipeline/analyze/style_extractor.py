"""Extract instructor teaching style and voice from transcripts and slide notes."""

from __future__ import annotations

import json
from pathlib import Path

import anthropic

from pipeline.config import get_claude_config
from pipeline.generate._utils import all_course_titles, at_institution
from pipeline.utils import text_from
from tutor.courses import IDENTITY

STYLE_SYSTEM_TEMPLATE = """You are an expert at analyzing teaching styles and pedagogical approaches.
You will be given excerpts from lecture transcripts and slide speaker notes from an
instructor who teaches {subject}. Your task is to produce a detailed, actionable
instructor style guide that captures this person's unique voice and teaching approach."""

STYLE_USER_PROMPT = """Analyze the following lecture materials from {instructor} ({courses}{institution}).

{material}

Produce a detailed instructor style guide in Markdown with these sections:

## Voice Characteristics
How formal/informal, pacing, energy level, directness. Include verbatim example phrases.

## Recurring Phrases and Idioms
Exact phrases the instructor uses repeatedly (e.g., "Let's think about this differently...")

{examples_section}

## How Difficulty Is Signaled
How does the instructor flag "this is the hard part"? How is student confusion anticipated?

## Analogy Patterns
What analogies and metaphors are used? What domains do they draw from?

## Example Progression Pattern
How are examples structured? (simple → complex? abstract → concrete?)

## Student Interaction Style
How are questions asked? How is participation solicited?

## Common Misconceptions Addressed
What student errors/misconceptions does the instructor proactively address?

## Tone and Humor
Describe the humor style. Include examples if present.

{code_style_section}Keep each section concrete with verbatim examples from the material where possible."""


def style_system_prompt() -> str:
    return STYLE_SYSTEM_TEMPLATE.format(subject=IDENTITY.subject)


def style_user_prompt(material: str) -> str:
    """The analysis request, with the code sections only when the course uses code."""
    lang = IDENTITY.code_language
    if lang:
        examples = ("## How Code Is Introduced\n"
                    "Does the instructor use live coding, walk through existing code, build incrementally?\n"
                    "What context is given before showing code? How are errors handled during demo?")
        code_style = (f"## {lang} Code Style\n"
                      f"How is {lang} presented on slides vs. explained verbally? "
                      "Naming conventions emphasized?\n\n")
    else:
        examples = ("## How Examples Are Worked\n"
                    "Does the instructor work problems live, walk through finished solutions, build up step by step?\n"
                    "What context is given before an example? How are mistakes handled along the way?")
        code_style = ""
    return STYLE_USER_PROMPT.format(
        instructor=IDENTITY.instructor, courses=all_course_titles(), institution=at_institution(),
        material=material, examples_section=examples, code_style_section=code_style)


def _load_transcripts(raw_dir: Path, max_chars: int = 40000) -> str:
    transcript_dir = raw_dir / "transcripts"
    if not transcript_dir.exists():
        return ""
    texts: list[str] = []
    total = 0
    for jf in sorted(transcript_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        text = data.get("full_text", "")[:8000]
        texts.append(f"### Transcript: {jf.stem}\n{text}")
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n".join(texts)


def _load_slide_notes(raw_dir: Path, max_chars: int = 20000) -> str:
    slides_dir = raw_dir / "slides"
    if not slides_dir.exists():
        return ""
    texts: list[str] = []
    total = 0
    for jf in sorted(slides_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        notes_parts: list[str] = []
        for slide in data.get("slides", []):
            notes = slide.get("speaker_notes", "").strip()
            if notes:
                notes_parts.append(f"  Slide {slide['slide_number']} ({slide.get('title','')}): {notes}")
        if notes_parts:
            texts.append(f"### Slide Notes: {data['filename']}\n" + "\n".join(notes_parts))
            total += sum(len(n) for n in notes_parts)
        if total >= max_chars:
            break
    return "\n\n".join(texts)


def extract_style(raw_dir: Path, output_dir: Path, verbose: bool = False) -> Path:
    cc = get_claude_config()

    transcripts_text = _load_transcripts(raw_dir)
    notes_text = _load_slide_notes(raw_dir)

    if not transcripts_text and not notes_text:
        print("[analyze] No transcripts or slide notes found. Run 'ingest' and 'transcribe' first.")
        output_dir.mkdir(parents=True, exist_ok=True)
        stub = output_dir / "style-guide.md"
        stub.write_text("# Instructor Style Guide\n\n*Run ingest and transcribe first to generate this file.*\n")
        return stub

    material = ""
    if transcripts_text:
        material += "## LECTURE TRANSCRIPTS\n\n" + transcripts_text + "\n\n"
    if notes_text:
        material += "## SLIDE SPEAKER NOTES\n\n" + notes_text

    if verbose:
        print(f"[analyze] Style extraction: {len(material):,} chars of source material")

    client = anthropic.Anthropic(api_key=cc["api_key"])
    message = client.messages.create(
        model=cc["model"],
        max_tokens=cc["max_tokens"],
        system=style_system_prompt(),
        messages=[{"role": "user", "content": style_user_prompt(material[:55000])}],
    )

    style_guide = text_from(message)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "style-guide.md"
    out_path.write_text(style_guide)
    if verbose:
        print(f"[analyze] Style guide written to {out_path}")
    return out_path
