"""Extract recurring examples, analogies, and code patterns from course materials."""

from __future__ import annotations

import json
from pathlib import Path

import anthropic

from pipeline.config import get_claude_config
from pipeline.generate._utils import all_course_titles
from pipeline.utils import text_from
from tutor.courses import IDENTITY

EXTRACT_SYSTEM_TEMPLATE = """You are analyzing instructor course materials to identify the canonical
examples and analogies used to teach {subject}."""

EXTRACT_USER_PROMPT = """Analyze the following course materials ({courses}) and produce an examples library.

## Slide Content
{slide_content}

{transcript_sample}

Identify ALL recurring examples and produce a Markdown document with these sections:

## {examples_heading}
For each recurring {example_kind} (a running problem, case, or scenario the instructor returns to):
- **Example name**: brief description
- **Concept illustrated**: which concept of the course this teaches
- **When introduced**: which lecture/topic
- **Complexity level**: beginner/intermediate/advanced
- **Key learning points**: what students should take away

## Analogies and Metaphors
For each analogy used to explain abstract concepts:
- **Analogy**: description
- **Concept explained**: what abstract idea this illustrates
- **Typical phrasing**: how the instructor introduces it

## {patterns_heading}
{patterns_body}

## Assignment Progression
How assignments build on each other, from the first to the last:
- What each assignment teaches
- How complexity increases
- Skills carried forward"""


def extract_system_prompt() -> str:
    return EXTRACT_SYSTEM_TEMPLATE.format(subject=IDENTITY.subject)


def extract_user_prompt(slide_content: str, transcript_sample: str) -> str:
    """The examples-library request; the code sections only when the course uses code."""
    lang = IDENTITY.code_language
    if lang:
        parts = {
            "examples_heading": "Programming Examples",
            "example_kind": "coding example",
            "patterns_heading": "Code Patterns",
            "patterns_body": (f"Recurring {lang} patterns the instructor uses as teaching vehicles: "
                              "the idioms, the library features, and the program shapes that come back "
                              "lecture after lecture."),
        }
    else:
        parts = {
            "examples_heading": "Worked Examples",
            "example_kind": "worked example",
            "patterns_heading": "Problem Patterns",
            "patterns_body": ("Recurring kinds of problems the instructor uses as teaching vehicles: "
                              "the calculations, diagrams, and reasoning steps that come back "
                              "lecture after lecture."),
        }
    return EXTRACT_USER_PROMPT.format(courses=all_course_titles(), slide_content=slide_content,
                                      transcript_sample=transcript_sample, **parts)


def _load_all_slide_text(slides_raw_dir: Path, max_chars: int = 30000) -> str:
    if not slides_raw_dir.exists():
        return ""
    parts: list[str] = []
    total = 0
    for jf in sorted(slides_raw_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        for slide in data.get("slides", []):
            title = slide.get("title", "")
            body = " ".join(slide.get("body_text", []))[:500]
            if title or body:
                parts.append(f"[{data['filename']} s{slide['slide_number']}] {title}: {body}")
                total += len(parts[-1])
                if total >= max_chars:
                    break
        if total >= max_chars:
            break
    return "\n".join(parts)


def _load_transcript_sample(transcripts_dir: Path, max_chars: int = 15000) -> str:
    if not transcripts_dir.exists():
        return ""
    texts: list[str] = []
    total = 0
    for jf in sorted(transcripts_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        sample = data.get("full_text", "")[:5000]
        texts.append(f"## Transcript: {jf.stem}\n{sample}")
        total += len(sample)
        if total >= max_chars:
            break
    if not texts:
        return ""
    return "## Transcript Samples\n" + "\n\n".join(texts)


def extract_examples(raw_dir: Path, output_dir: Path, verbose: bool = False) -> Path:
    cc = get_claude_config()
    slide_content = _load_all_slide_text(raw_dir / "slides")
    transcript_sample = _load_transcript_sample(raw_dir / "transcripts")

    if not slide_content:
        print("[analyze] No slide content found. Run ingest first.")
        output_dir.mkdir(parents=True, exist_ok=True)
        stub = output_dir / "examples-library.md"
        stub.write_text("# Examples Library\n\n*Run ingest first to generate this file.*\n")
        return stub

    client = anthropic.Anthropic(api_key=cc["api_key"])
    message = client.messages.create(
        model=cc["model"],
        max_tokens=cc["max_tokens"],
        system=extract_system_prompt(),
        messages=[{
            "role": "user",
            "content": extract_user_prompt(slide_content[:40000], transcript_sample),
        }],
    )

    examples_library = text_from(message)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "examples-library.md"
    out_path.write_text(examples_library)
    if verbose:
        print(f"[analyze] Examples library written to {out_path}")
    return out_path
