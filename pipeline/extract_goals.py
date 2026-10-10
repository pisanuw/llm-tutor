"""Extract learning goals from the lecture plans and write curriculum/learning_goals.json.

Strategy: one Claude call per course (through the local ``claude`` CLI, so
your subscription is billed, never an API key) for goal nodes only, then
programmatic edge derivation from each lecture's "Prerequisite Lectures" line
in the plan. Splitting by course avoids the large-output timeout that kills a
single combined call.

Usage:
    python -m pipeline extract-goals                  # every course in courses.yaml
    python -m pipeline extract-goals --course AST102  # one course; the others' goals are kept
    python -m pipeline extract-goals --dry-run        # parse the plans, call nothing

The result is a draft: read it, rename or merge goals, and add the
prerequisite edges between courses by hand (ADDING-KNOWLEDGE.md, step 5).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from tutor.courses import COURSES, IDENTITY
from tutor.paths import CURRICULUM_DIR, lecture_plan_path

ROOT = Path(__file__).parent.parent
OUTPUT_PATH = CURRICULUM_DIR / "learning_goals.json"

NO_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit"
CALL_TIMEOUT = 300  # seconds per call — goals-only output is small (~5-8 KB)
INTER_CALL_SLEEP = 30  # seconds between calls


# ---------------------------------------------------------------------------
# Parse curriculum plan markdown
# ---------------------------------------------------------------------------

def _parse_range(s: str) -> list[int]:
    s = s.strip()
    if s.lower() == "none" or not s:
        return []
    s_norm = s.replace("–", "-").replace("—", "-")
    nums = [int(x) for x in re.findall(r"\d+", s_norm)]
    if not nums:
        return []
    has_range = bool(re.search(r"\d+\s*[-–—]\s*\d+", s))
    if has_range and len(nums) == 2 and nums[1] > nums[0] + 1:
        return list(range(nums[0], nums[1] + 1))
    return nums


def parse_lectures(plan_path: Path, course: str) -> list[dict]:
    text = plan_path.read_text()
    parts = re.split(r"\n## Lecture (\d+):", text)
    lectures = []
    for i in range(1, len(parts), 2):
        num = int(parts[i])
        body = parts[i + 1] if i + 1 < len(parts) else ""
        title_line = body.split("\n")[0].strip()

        obj_match = re.search(
            r"\*\*Learning Objectives:\*\*[^\n]*\n(.*?)(?=\n\*\*|\Z)", body, re.DOTALL
        )
        objectives: list[str] = []
        if obj_match:
            objectives = re.findall(r"^\d+\.\s+(.+)", obj_match.group(1), re.MULTILINE)

        prereq_match = re.search(r"\*\*Prerequisite Lectures:\*\*\s*(.+)", body)
        prereq_lectures: list[int] = []
        if prereq_match:
            prereq_lectures = _parse_range(prereq_match.group(1))

        lectures.append({
            "num": num,
            "title": title_line,
            "course": course,
            "objectives": objectives,
            "prereq_lectures": prereq_lectures,
        })
    return lectures


# ---------------------------------------------------------------------------
# Claude call — goals only (no edges)
# ---------------------------------------------------------------------------

GOALS_SYSTEM_TEMPLATE = """\
You are a curriculum designer for a course in {subject}.
Output ONLY a JSON array of goal objects — no wrapper key, no "edges", no markdown fences.

For each lecture in the input, produce exactly 3-4 goal nodes.
Each goal must be:
- Atomic: one skill or concept, not a summary of the lecture
- Actionable: phrased as something the student can DO after mastering it
- Distinct from other goals in the same lecture

Each goal object schema:
{{
  "id": "<course>-L<NN>-<slug>",
  "title": "Short imperative phrase (max 8 words)",
  "description": "One sentence: what can the student do?",
  "course": "<course id string>",
  "lecture": <integer>
}}

id rules: NN = zero-padded 2-digit lecture number; slug = 2-4 lowercase hyphen-separated words; no special chars.

Example: {{"id":"{example_course}-L02-explain-moon-phases","title":"Explain the cause of lunar phases","description":"Student can explain the Moon's phases from the changing angle between Sun, Moon and Earth, and rule out Earth's shadow as the cause.","course":"{example_course}","lecture":2}}

Skip review/wrap-up lectures (Midterm Review, Final Review, Wrap-Up, Course Wrap-Up).
Output ONLY the JSON array. No preamble, no explanation.
"""


def goals_system_prompt() -> str:
    return GOALS_SYSTEM_TEMPLATE.format(subject=IDENTITY.subject, example_course=COURSES[0])


def _claude_env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def call_claude_goals(lectures: list[dict], label: str) -> list[dict]:
    # Strip objectives down to avoid unnecessary tokens
    payload = [
        {"num": l["num"], "title": l["title"], "course": l["course"], "objectives": l["objectives"]}
        for l in lectures
    ]
    prompt_text = json.dumps(payload)
    print(f"  [{label}] prompt {len(prompt_text)} chars — calling claude...")

    cmd = [
        "claude", "-p",
        "--model", "sonnet",
        "--output-format", "text",
        "--system-prompt", goals_system_prompt(),
        "--tools", "",            # every built-in tool off, including any the CLI adds later
        "--disallowed-tools", NO_TOOLS,
    ]
    popen = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_claude_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = popen.communicate(input=prompt_text, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        popen.wait()
        raise RuntimeError(f"[{label}] claude timed out after {CALL_TIMEOUT}s") from None

    if popen.returncode != 0:
        print(f"  [{label}] stderr:\n{stderr}", file=sys.stderr)
        raise RuntimeError(f"[{label}] claude exited {popen.returncode}")

    raw = stdout.strip()
    # Strip accidental fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw.strip())

    goals = json.loads(raw)
    if not isinstance(goals, list):
        raise ValueError(f"[{label}] expected JSON array, got {type(goals)}")
    print(f"  [{label}] got {len(goals)} goals")
    return goals


# ---------------------------------------------------------------------------
# Programmatic edge derivation from lecture-level prerequisites
# ---------------------------------------------------------------------------

# Lectures that are review/wrap-up — exclude as edge sources/targets
SKIP_LECTURE_TITLES = {"midterm review", "final", "wrap-up", "course wrap-up", "final exam review"}


def _is_skip(lecture: dict) -> bool:
    title_lower = lecture["title"].lower()
    return any(skip in title_lower for skip in SKIP_LECTURE_TITLES)


def derive_edges(all_goals: list[dict], all_lectures: list[dict]) -> list[dict]:
    """
    For each lecture B with prereq_lectures [A1, A2, ...]:
      Connect the last goal of each Ai to the first goal of B.

    This creates a directed acyclic graph that mirrors the curriculum dependency
    structure without requiring per-goal semantic matching.
    """
    # Index: (course, lecture_num) -> ordered list of goal IDs
    goals_by_lecture: dict[tuple, list[str]] = defaultdict(list)
    for g in all_goals:
        goals_by_lecture[(g["course"], g["lecture"])].append(g["id"])

    # Build lecture lookup
    lecture_map = {(l["course"], l["num"]): l for l in all_lectures}

    edges: list[dict] = []
    seen: set[tuple] = set()

    for lec in all_lectures:
        if _is_skip(lec):
            continue
        course = lec["course"]
        lec_num = lec["num"]
        current_goals = goals_by_lecture.get((course, lec_num), [])
        if not current_goals:
            continue

        first_current = current_goals[0]

        for prereq_num in lec["prereq_lectures"]:
            prereq_lec = lecture_map.get((course, prereq_num))
            if prereq_lec and _is_skip(prereq_lec):
                continue
            prereq_goals = goals_by_lecture.get((course, prereq_num), [])
            if not prereq_goals:
                continue
            last_prereq = prereq_goals[-1]
            edge_key = (last_prereq, first_current)
            if edge_key not in seen:
                edges.append({"from": last_prereq, "to": first_current})
                seen.add(edge_key)

    # Edges between courses are not derived: whether one course builds on
    # another is yours to say, goal by goal, in learning_goals.json.
    return edges


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(courses: list[str] | None = None, dry_run: bool = False, out: Path = OUTPUT_PATH) -> None:
    """Extract goals for ``courses`` (default: all) and write ``out``. Goals
    and edges of a course not being extracted are kept as they are."""
    courses = list(courses or COURSES)
    print("Parsing lecture plans...")
    lectures_by_course: dict[str, list[dict]] = {}
    for course in courses:
        plan = lecture_plan_path(course)
        if not plan.exists():
            raise FileNotFoundError(f"{plan} not found: write the lecture plan first (ADDING-KNOWLEDGE.md, step 4)")
        lectures = parse_lectures(plan, course)
        lectures_by_course[course] = lectures
        print(f"  {course}: {len(lectures)} lectures, {sum(len(l['objectives']) for l in lectures)} objectives")

    if dry_run:
        first = next(iter(lectures_by_course.values()))
        if first:
            print("\nFirst parsed lecture:")
            print(json.dumps(first[0], indent=2))
        return

    new_goals: list[dict] = []
    for i, course in enumerate(courses):
        if i:
            print(f"\nSleeping {INTER_CALL_SLEEP}s between calls...")
            time.sleep(INTER_CALL_SLEEP)
        print(f"\nExtracting goals: call {i + 1}/{len(courses)} ({course})...")
        new_goals += call_claude_goals(lectures_by_course[course], course)

    all_lectures = [lec for lecs in lectures_by_course.values() for lec in lecs]
    print("\nDeriving edges from lecture prerequisites...")
    new_edges = derive_edges(new_goals, all_lectures)
    print(f"  Derived {len(new_edges)} edges.")

    # Keep what belongs to the courses not extracted this run, and any
    # hand-added edge between a kept goal and another kept goal.
    old: dict = json.loads(out.read_text()) if out.exists() else {"goals": [], "edges": []}
    kept_goals = [g for g in old.get("goals", []) if g.get("course") not in courses]
    kept_ids = {g["id"] for g in kept_goals}
    kept_edges = [e for e in old.get("edges", []) if e.get("from") in kept_ids and e.get("to") in kept_ids]
    data = {"goals": kept_goals + new_goals, "edges": kept_edges + new_edges,
            "graph_version": int(old.get("graph_version", 0)) + 1}

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n")
    print(f"\nWrote {len(data['goals'])} goals + {len(data['edges'])} edges to {out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print parsed data, skip Claude")
    parser.add_argument("--course", action="append", choices=list(COURSES),
                        help="Extract only this course (repeatable; default: every course)")
    parser.add_argument("--out", default=str(OUTPUT_PATH))
    args = parser.parse_args()
    run(args.course, args.dry_run, Path(args.out))


if __name__ == "__main__":
    main()
