"""Generate mini-mastery exam questions for all learning goals.

Reads  curriculum/learning_goals.json and writes curriculum/mastery_exams.json.
Uses the claude CLI (subscription) — no API key required.

Code questions are asked for only when courses.yaml sets ``code_language``,
and then in that language; otherwise every question is a short-answer one
(kind "concept"), which is how the exam shows a course without code.

Usage:
    python -m pipeline generate-mastery-exams                     # every goal
    python -m pipeline generate-mastery-exams --resume            # skip already done
    python -m pipeline generate-mastery-exams --course AST101
    python -m pipeline generate-mastery-exams --goal-id AST101-L02-explain-moon-phases
    .venv/bin/python pipeline/generate_mastery_exams.py --questions-per-goal 10
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from tutor.courses import COURSES, IDENTITY
from tutor.paths import CURRICULUM_DIR

ROOT        = Path(__file__).parent.parent
GOALS_PATH  = CURRICULUM_DIR / "learning_goals.json"
OUTPUT_PATH = CURRICULUM_DIR / "mastery_exams.json"

NO_TOOLS     = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit"
CALL_TIMEOUT = 90   # seconds per goal
INTER_SLEEP  = 0.5  # seconds between calls

SYSTEM_TEMPLATE = """\
You are a curriculum designer for a course in {subject}, for {audience}.
Output ONLY valid JSON — no markdown fences, no preamble, no explanation.
"""


def system_prompt() -> str:
    return SYSTEM_TEMPLATE.format(subject=IDENTITY.subject, audience=IDENTITY.audience)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _build_prompt(goal: dict, n_questions: int = 8) -> str:
    from pipeline.generate._utils import course_title

    lang = IDENTITY.code_language
    n_code = max(2, n_questions // 3)
    n_concept = n_questions - n_code
    if lang:
        mix = (
            f"Mix: about {n_concept} conceptual short-answer questions and about {n_code} small\n"
            f"{lang} coding problems (write or complete a function, ~5-15 lines).\n"
            f"Exception: if the goal has no coding aspect (pure theory), make all\n"
            f"questions conceptual (kind \"concept\").\n\n"
        )
        shape = (
            '  {"id": "c1", "kind": "concept", "question": "...", "rubric": "..."},\n'
            '  {"id": "k1", "kind": "code", "question": "...", "starter": "optional starter code", "rubric": "..."}\n'
        )
        starter_rule = "- Code questions may include starter code when it reduces ambiguity.\n"
    else:
        mix = (
            f"Mix: about {n_concept} conceptual short-answer questions and about {n_code} short\n"
            f"worked problems (a small calculation, a diagram to describe in words, or a\n"
            f"scenario to reason through). Give every question kind \"concept\"; there is no code.\n\n"
        )
        shape = '  {"id": "c1", "kind": "concept", "question": "...", "rubric": "..."}\n'
        starter_rule = ""
    return (
        f"Generate a pool of {n_questions} mini-mastery exam questions for this learning goal.\n\n"
        f"Course: {course_title(goal['course'])} ({IDENTITY.subject})\n"
        f"Lecture: {goal['lecture']}\n"
        f"Goal: {goal['title']}\n"
        f"Description: {goal['description']}\n\n"
        + mix +
        f"Rules:\n"
        f"- Each question must directly test THIS specific goal, not general knowledge.\n"
        f"- Vary difficulty and angle so the {n_questions} are genuinely different.\n"
        f"- Every question needs its own rubric: 1-2 sentences of key points a correct\n"
        f"  answer must include.\n"
        + starter_rule +
        "- Give each question a short unique id.\n"
        "- Keep everything terse.\n\n"
        "Return ONLY valid JSON (no markdown, no preamble):\n"
        '{"questions": [\n'
        + shape +
        "]}"
    )


# ---------------------------------------------------------------------------
# Claude CLI call
# ---------------------------------------------------------------------------

def _claude_env() -> dict:
    """Strip the API key so the CLI uses the subscription."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _generate_one(goal: dict, n_questions: int = 8) -> dict:
    prompt = _build_prompt(goal, n_questions)
    cmd = [
        "claude", "-p",
        "--model", "haiku",
        "--output-format", "text",
        "--system-prompt", system_prompt(),
        "--tools", "",            # every built-in tool off, including any the CLI adds later
        "--disallowed-tools", NO_TOOLS,
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
        stdout, stderr = proc.communicate(input=prompt, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait()
        raise RuntimeError(f"claude timed out after {CALL_TIMEOUT}s") from None

    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {stderr.strip()[:200]}")

    return json.loads(_strip_fences(stdout))


# ---------------------------------------------------------------------------
# Core generation logic (called by both CLI entry-point and pipeline command)
# ---------------------------------------------------------------------------

def run_generation(
    course: str | None = None,
    goal_id: str | None = None,
    resume: bool = False,
    questions_per_goal: int = 8,
) -> tuple[int, int]:
    """Generate exam questions and write to OUTPUT_PATH.

    Returns (total_saved, error_count).
    """
    if not GOALS_PATH.exists():
        raise FileNotFoundError(f"{GOALS_PATH} not found")

    all_goals: list[dict] = json.loads(GOALS_PATH.read_text())["goals"]

    existing: dict = {}
    if OUTPUT_PATH.exists():
        existing = json.loads(OUTPUT_PATH.read_text())
        if resume and existing:
            print(f"Resuming: {len(existing)} goals already done")

    goals = all_goals
    if goal_id:
        goals = [g for g in goals if g["id"] == goal_id]
    elif course:
        goals = [g for g in goals if g["course"] == course]
    if resume:
        goals = [g for g in goals if g["id"] not in existing]

    if not goals:
        print("Nothing to generate.")
        return len(existing), 0

    results: dict = dict(existing)
    errors = 0

    print(f"Generating exams for {len(goals)} goal(s) via claude CLI → {OUTPUT_PATH}")

    for i, goal in enumerate(goals):
        tag = f"[{i + 1}/{len(goals)}] {goal['id']}"
        try:
            print(f"{tag} ...", end=" ", flush=True)
            questions = _generate_one(goal, questions_per_goal)
            results[goal["id"]] = questions
            print("OK")
        except json.JSONDecodeError as exc:
            print(f"JSON ERROR: {exc}")
            errors += 1
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            errors += 1
        except Exception as exc:
            print(f"ERROR: {exc}")
            errors += 1

        OUTPUT_PATH.write_text(json.dumps(results, indent=2))

        if i < len(goals) - 1:
            time.sleep(INTER_SLEEP)

    print(f"\nDone. {len(results)} exams in {OUTPUT_PATH}  ({errors} error(s))")
    return len(results), errors


# ---------------------------------------------------------------------------
# Standalone entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate mastery exam questions via claude CLI")
    parser.add_argument("--course",  default=None, choices=list(COURSES), help="Filter by course id")
    parser.add_argument("--goal-id", default=None, help="Generate for a single goal ID")
    parser.add_argument("--resume",  action="store_true", help="Skip already-generated goals")
    parser.add_argument("--questions-per-goal", type=int, default=8, help="Pool size per goal (default 8)")
    args = parser.parse_args()

    try:
        run_generation(
            course=args.course, goal_id=args.goal_id, resume=args.resume,
            questions_per_goal=args.questions_per_goal,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
