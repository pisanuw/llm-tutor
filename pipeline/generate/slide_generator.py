"""Generate slide content for a lecture using Claude API + RAG."""

from __future__ import annotations

from pathlib import Path

import anthropic

from pipeline.config import get_claude_config, get_slide_generation_config
from pipeline.generate._utils import course_line, extract_lecture_plan_entry
from tutor.courses import IDENTITY

SLIDE_SYSTEM_PROMPT_TEMPLATE = """You are generating lecture slides for {course_line}.
You must match the instructor's teaching voice, use their established examples, and produce
accurate {subject} content for these students.

Output a structured Markdown document where each slide is a clearly delimited section.
{code_rule}"""

SLIDE_USER_PROMPT = """Generate slides for this lecture:

## Lecture Plan Entry
{lecture_plan_entry}

## Instructor Style Guide (excerpt)
{style_guide}

## Examples Library (excerpt)
{examples_library}

## Relevant Existing Slide Content (RAG)
{rag_slides}

## Relevant Lecture Recording Transcripts (RAG)
{rag_transcripts}

## Relevant Textbook Content (RAG)
{rag_textbook}

## Relevant Course Documents: notes, handouts, PDFs (RAG)
{rag_documents}

Generate {min_slides} to {max_slides} slides. Format EXACTLY as follows for each slide:

---
## Slide N: [Title]
type: [title|agenda|content|{code_type}diagram-placeholder|summary|quiz]
title: [slide title]
bullets:
  - bullet 1
  - bullet 2
body: |
  [optional body text for content slides]
{code_field}notes: [speaker notes - what to SAY while showing this slide, 2-4 sentences]
---

Rules:
- First slide is always type: title
- Second slide is always type: agenda (today's topics)
- Last slide is always type: summary
{code_slide_rule}- Notes should match the instructor's voice from the style guide
- Use the examples from the examples library where relevant"""


def _code_parts() -> dict[str, str]:
    """The code slide type and its rules, only when the course uses code."""
    lang = IDENTITY.code_language
    if not lang:
        return {
            "code_rule": "Concrete examples are worked examples, diagrams described in words, "
                         "or short problems with their solutions; do not write code.",
            "code_type": "",
            "code_field": "",
            "code_slide_rule": "",
        }
    return {
        "code_rule": f"Code examples must be valid, idiomatic {lang} that compiles or runs as shown.",
        "code_type": "code|",
        "code_field": f"code: |\n  [optional {lang} code for code slides]\n",
        "code_slide_rule": f"- Code slides should have syntax-correct {lang}\n",
    }


def _rag_context(query: str, collection: str, n: int = 4) -> str:
    from pipeline.knowledge_base.vectorstore import query as kb_query
    results = kb_query(collection, query, n_results=n)
    if not results:
        return "[No relevant content found]"
    return "\n---\n".join(r["text"][:500] for r in results)


def generate_slide_content(
    lecture_num: int,
    course_config: dict,
    curriculum_dir: Path,
    instructor_profile_dir: Path,
    output_dir: Path,
    verbose: bool = False,
) -> Path:
    cc = get_claude_config()
    sg_config = get_slide_generation_config()
    client = anthropic.Anthropic(api_key=cc["api_key"])

    course_num = course_config["course_number"]
    course_name = course_config.get("course_name") or course_config.get("course_label") or course_num
    plan_path = curriculum_dir / f"{course_num}-lecture-plan.md"
    style_guide_path = instructor_profile_dir / "style-guide.md"
    examples_path = instructor_profile_dir / "examples-library.md"

    lecture_plan_entry = extract_lecture_plan_entry(plan_path, lecture_num)
    style_guide = style_guide_path.read_text()[:2500] if style_guide_path.exists() else ""
    examples_library = examples_path.read_text()[:2000] if examples_path.exists() else ""

    # RAG queries based on the lecture title/topics from the plan: each of the
    # instructor's own collections once (tutor/kb.py).
    query_text = lecture_plan_entry[:200]
    n = sg_config["rag_results_per_query"]
    rag_slides = _rag_context(query_text, "slides", n=n)
    rag_transcripts = _rag_context(query_text, "transcripts", n=n)
    rag_textbook = _rag_context(query_text, "textbook", n=n)
    rag_documents = _rag_context(query_text, "documents", n=n)

    if verbose:
        print(f"[generate] Generating slide content for Lecture {lecture_num:02d}...")

    parts = _code_parts()
    # Streaming avoids the 10-minute non-streaming timeout on large slide sets.
    system_prompt = SLIDE_SYSTEM_PROMPT_TEMPLATE.format(
        course_line=course_line(course_name), subject=IDENTITY.subject, code_rule=parts["code_rule"])
    content_md = ""
    with client.messages.stream(
        model=cc["model"],
        max_tokens=cc["max_tokens"],
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": SLIDE_USER_PROMPT.format(
                lecture_plan_entry=lecture_plan_entry,
                style_guide=style_guide,
                examples_library=examples_library,
                rag_slides=rag_slides,
                rag_transcripts=rag_transcripts,
                rag_textbook=rag_textbook,
                rag_documents=rag_documents,
                min_slides=sg_config["min_slides_per_lecture"],
                max_slides=sg_config["max_slides_per_lecture"],
                code_type=parts["code_type"],
                code_field=parts["code_field"],
                code_slide_rule=parts["code_slide_rule"],
            ),
        }],
    ) as stream:
        for chunk in stream.text_stream:
            content_md += chunk
    lec_dir = output_dir / f"lecture-{lecture_num:02d}"
    lec_dir.mkdir(parents=True, exist_ok=True)
    out_path = lec_dir / "content.md"
    out_path.write_text(content_md)
    if verbose:
        print(f"[generate] Slide content written to {out_path}")
    return out_path
