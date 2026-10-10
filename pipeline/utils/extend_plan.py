"""Extend a lecture plan that was truncated before its last lecture.

The course's lecture count comes from courses.yaml; the missing entries are
asked for in the same shape the planner writes (curriculum_planner.py).

    python -m pipeline.utils.extend_plan AST101
"""

from __future__ import annotations

import sys

from pipeline.generate._utils import course_title
from tutor.courses import default_course, get_course
from tutor.paths import lecture_plan_path


def extend_plan(course_num: str | None = None) -> None:
    course_num = course_num or default_course()
    course = get_course(course_num)
    if course is None:
        raise SystemExit(f"Unknown course {course_num!r}: not in courses.yaml")
    total = course.lectures
    name = course_title(course_num)
    plan_path = lecture_plan_path(course_num)
    existing_plan = plan_path.read_text()

    # Check how many lectures exist
    lecture_headers = [ln for ln in existing_plan.splitlines() if ln.startswith("## Lecture")]
    last_num = max(int(h.split(":")[0].split()[-1]) for h in lecture_headers)
    print(f"[extend_plan] Found {last_num} lectures. Extending to {total}...")

    if last_num >= total:
        print(f"[extend_plan] Plan already has {total}+ lectures. Nothing to do.")
        return

    missing = list(range(last_num + 1, total + 1))
    span = f"{missing[0]}-{total}" if len(missing) > 1 else str(total)

    from pipeline.generate.curriculum_planner import lecture_entry_format

    prompt = f"""The following is an existing {total}-lecture curriculum plan for {name}.
The plan is missing lecture(s) {span}. Please generate ONLY the missing lectures following exactly the same format as the existing ones.

## Existing Plan (last few lectures shown for context)
{chr(10).join(existing_plan.splitlines()[-200:])}

## Required: Generate lecture(s) {span}

Follow this exact format for EACH missing lecture:

{lecture_entry_format()}

Remember:
- If the plan has a final exam and no review lecture yet, the lecture before it reviews for it
- Lecture {total} is the last class: typically a synthesis, wrap-up, or skills integration session
- All {name} content must be covered by lecture {total}"""

    # The pipeline's hardened CLI helper (plan step 6.8, finding E14): no
    # tools, no API key; it raises RuntimeError when the CLI fails.
    from pipeline.generate.script_generator import PLAIN_SYSTEM, _generate_via_cli

    extension = _generate_via_cli(PLAIN_SYSTEM, prompt, timeout=300)

    # Append to existing plan (remove trailing --- if present)
    plan_text = existing_plan.rstrip()
    if not plan_text.endswith("---"):
        plan_text += "\n\n---\n"
    plan_path.write_text(plan_text + "\n\n" + extension.strip() + "\n")
    print(f"[extend_plan] Plan extended with lecture(s) {span}. Written to {plan_path}")


if __name__ == "__main__":
    extend_plan(sys.argv[1] if len(sys.argv) > 1 else None)
