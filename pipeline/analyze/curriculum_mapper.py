"""Map course topics to existing lectures and identify coverage structure."""

from __future__ import annotations

import json
from pathlib import Path

import anthropic

from pipeline.config import get_claude_config
from pipeline.utils import text_from
from tutor.courses import IDENTITY

MAPPER_SYSTEM_TEMPLATE = """You are an expert curriculum designer specializing in {subject} education.
You will analyze existing course materials and produce a structured topic map for a course
in {subject}."""

MAPPER_USER_PROMPT = """Analyze the {course_name} course materials below and produce a JSON topic map.

## Course Topic List
{topic_list}

## Existing Lecture Slide Files
{slide_inventory}

## Sample Slide Content
{slide_samples}

Produce a JSON object with this structure:
{{
  "course": "{course_num}",
  "topics": [
    {{
      "name": "topic name",
      "lectures_covered": ["filename.pptx"],
      "depth": "introductory|intermediate|advanced",
      "typical_minutes": 60,
      "prerequisites": ["topic names"],
      "examples_used": ["example names from slides"],
      "key_concepts": ["concept 1", "concept 2"],
      "coverage_notes": "any gaps or special observations"
    }}
  ],
  "coverage_gaps": ["topics in the list not covered by any lecture"],
  "suggested_lecture_sequence": ["brief sequence recommendation"]
}}

Be precise and ground every claim in the slide content provided."""


def _load_topic_list(topic_list_path: Path) -> str:
    if not topic_list_path.exists():
        return "No topic list found."
    return topic_list_path.read_text()


def _load_slide_inventory(slides_raw_dir: Path) -> tuple[str, str]:
    inventory_lines: list[str] = []
    samples: list[str] = []
    if not slides_raw_dir.exists():
        return "No slide data found.", ""
    for jf in sorted(slides_raw_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        title = data.get("lecture_title", jf.stem)
        count = data.get("slide_count", 0)
        inventory_lines.append(f"- {data['filename']}: {title} ({count} slides)")
        # First 3 slides as sample
        for slide in data.get("slides", [])[:3]:
            slide_text = slide.get("title", "") + " | " + " ".join(slide.get("body_text", []))[:200]
            samples.append(f"  [{jf.stem} s{slide['slide_number']}] {slide_text.strip()}")
    return "\n".join(inventory_lines), "\n".join(samples[:60])


def map_curriculum(
    course_config: dict,
    raw_dir: Path,
    output_dir: Path,
    verbose: bool = False,
) -> Path:
    cc = get_claude_config()
    course_num = course_config["course_number"]
    course_name = course_config.get("course_name") or course_config.get("course_label") or course_num
    topic_list_path = Path(course_config["source_topic_list"])
    slides_raw_dir = raw_dir / "slides"

    topic_list = _load_topic_list(topic_list_path)
    # Every deck, the pipeline's and any Canvas download's, is in raw/slides.
    inventory, samples = _load_slide_inventory(slides_raw_dir)

    if verbose:
        print(f"[analyze] Mapping curriculum from {len(list(slides_raw_dir.glob('*.json')))} slide files")

    client = anthropic.Anthropic(api_key=cc["api_key"])
    message = client.messages.create(
        model=cc["model"],
        max_tokens=cc["max_tokens"],
        system=MAPPER_SYSTEM_TEMPLATE.format(subject=IDENTITY.subject),
        messages=[{
            "role": "user",
            "content": MAPPER_USER_PROMPT.format(
                course_num=course_num,
                course_name=course_name,
                topic_list=topic_list,
                slide_inventory=inventory,
                slide_samples=samples,
            ),
        }],
    )

    response_text = text_from(message)

    # Extract JSON from response (Claude may wrap it in markdown fences)
    json_text = response_text
    if "```json" in response_text:
        json_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        json_text = response_text.split("```")[1].split("```")[0].strip()

    try:
        topic_map = json.loads(json_text)
    except json.JSONDecodeError:
        # Save raw response if JSON parse fails
        topic_map = {"raw_response": response_text, "parse_error": True}

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{course_num}-topic-map.json"
    out_path.write_text(json.dumps(topic_map, indent=2, ensure_ascii=False))
    if verbose:
        print(f"[analyze] Topic map written to {out_path}")
    return out_path
