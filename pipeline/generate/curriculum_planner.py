"""Generate a course's lecture plan (one entry per lecture in courses.yaml) using Claude API."""

from __future__ import annotations

from pathlib import Path

import anthropic

from pipeline.config import get_claude_config
from pipeline.generate._utils import at_institution, uses_code
from tutor.courses import IDENTITY

PLANNER_SYSTEM_TEMPLATE = """You are an expert curriculum designer for courses in {subject}.
You design for {audience}. Your plans are detailed, realistic,
and grounded in the specific instructor's materials and teaching history."""

# The shape of one lecture's entry. extract_goals reads "## Lecture NN:",
# "**Learning Objectives:**" and "**Prerequisite Lectures:**", and the app
# reads the headings, so those three lines keep their exact wording; the
# plan extender (pipeline/utils/extend_plan.py) asks for the same shape.
LECTURE_ENTRY_TEMPLATE = """---

## Lecture NN: [Title]
**Week:** W, Session S
**Duration:** [minutes] min

**Topics:**
- Topic 1
- Topic 2

**Learning Objectives:** By the end of this lecture, students will be able to:
1. Objective 1
2. Objective 2

**Time Allocation:**
| Segment | Minutes |
|---------|---------|
| Opening / recap | 5 |
| [Main topic 1] | 30 |
| ... | ... |
| Summary & Q&A | 10 |

**Key Examples:** [list examples from the examples library]

{demo_heading} [{demo_hint}]

**Assignment Connection:** [which assignment this lecture supports]

**Prerequisite Lectures:** [lecture numbers]

---"""

PLANNER_USER_PROMPT = """Design a complete {lectures}-lecture curriculum for {course_name}{institution}.

## Course Description
{course_description}

## Topic List
{topic_list}

## Topic Map (existing coverage analysis)
{topic_map}

## Style Guide Summary
{style_guide_excerpt}

## Constraints
- Exactly {lectures} lectures, numbered 01 to {last:02d}
- Use the class-session length and the term structure the course description
  gives; where it gives none, assume one session per lecture of about 80 minutes
- Follow the existing topic sequence where proven effective
- If the course has a midterm or a final exam, reserve the lecture before each
  for review, and say so in its title
- Space the assignments through the term and say which lecture introduces each

Produce a Markdown document with this structure for EACH of the {lectures} lectures:

{entry_format}

Be specific. Ground every lecture in the actual {course_name} material."""


def lecture_entry_format() -> str:
    """One lecture's entry, with the demonstration line the subject calls for."""
    if uses_code():
        return LECTURE_ENTRY_TEMPLATE.format(
            demo_heading="**Code Demonstrations:**",
            demo_hint=f"what to live-code in {IDENTITY.code_language} or show")
    return LECTURE_ENTRY_TEMPLATE.format(
        demo_heading="**Demonstrations:**",
        demo_hint="what to work through, draw, or show")


def planner_system_prompt() -> str:
    return PLANNER_SYSTEM_TEMPLATE.format(subject=IDENTITY.subject, audience=IDENTITY.audience)


def _load_file_safe(path: Path, max_chars: int = 3000) -> str:
    if not path.exists():
        return f"[File not found: {path}]"
    text = path.read_text()
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def generate_curriculum_plan(
    course_config: dict,
    curriculum_dir: Path,
    instructor_profile_dir: Path,
    raw_dir: Path,
    verbose: bool = False,
) -> Path:
    cc = get_claude_config()

    course_num = course_config["course_number"]
    course_name = course_config.get("course_name") or course_config.get("course_label") or course_num
    lectures = int(course_config["lectures"])
    topic_list = _load_file_safe(Path(course_config["source_topic_list"]))
    course_desc = _load_file_safe(Path(course_config["course_description"]))
    style_guide = _load_file_safe(instructor_profile_dir / "style-guide.md", max_chars=2000)
    topic_map_path = curriculum_dir / f"{course_num}-topic-map.json"
    topic_map = _load_file_safe(topic_map_path, max_chars=4000)

    if verbose:
        print(f"[generate] Generating {lectures}-lecture curriculum plan for {course_name}...")

    client = anthropic.Anthropic(api_key=cc["api_key"])
    prompt_content = PLANNER_USER_PROMPT.format(
        lectures=lectures,
        last=lectures,
        course_name=course_name,
        institution=at_institution(),
        course_description=course_desc,
        topic_list=topic_list,
        topic_map=topic_map,
        style_guide_excerpt=style_guide,
        entry_format=lecture_entry_format(),
    )
    # Use streaming to avoid 10-minute timeout for large plans
    plan_text = ""
    with client.messages.stream(
        model=cc["model"],
        max_tokens=cc["max_tokens"],
        system=planner_system_prompt(),
        messages=[{"role": "user", "content": prompt_content}],
    ) as stream:
        for text in stream.text_stream:
            plan_text += text
    curriculum_dir.mkdir(parents=True, exist_ok=True)
    out_path = curriculum_dir / f"{course_num}-lecture-plan.md"
    out_path.write_text(plan_text)
    if verbose:
        print(f"[generate] Curriculum plan written to {out_path}")
    return out_path
