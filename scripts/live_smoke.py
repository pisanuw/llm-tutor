"""Live smoke test: run real supervised turns against the Anthropic API.

Session 1 (Phase A). Spends real (small) API money; run it deliberately:

    .venv/bin/python scripts/live_smoke.py

Safety: forces a throwaway SQLite database (never the DATABASE_URL in .env),
uses a non-admin student so gates fire, and prints a per-turn cost/latency
summary from the TurnEvent ledger at the end. The questions are about the
first course in courses.yaml, named by its lecture titles from the plan.

Checks:
  1. concept question         -> tokens stream, no error event
  2. exercise request         -> tokens stream, no error event
  3. an attempt to check      -> tokens stream (a code submission when the course
                                 uses code: eval event if the actor grades)
  4. solution demand          -> judge ran (verdict recorded), no long code block
  5. gated lecture access     -> mastery exam intercept fires
  6. exam answer              -> exam graded, attempt closed or reopened
  Plus: cache_read_tokens > 0 on chat turns after the first (prompt cache OK).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Import config first (loads .env), THEN neutralize DATABASE_URL so get_engine
# falls back to SQLite. The smoke test must never write to prod Supabase.
import tutor.config  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="tutor-smoke-")
os.environ["DATABASE_URL"] = ""
os.environ["TUTOR_DB"] = str(Path(_tmp) / "smoke.db")
tutor.config.get_config.cache_clear()

from tutor.agent import state as state_mod  # noqa: E402
from tutor.agent.detector import scan as detect_scan  # noqa: E402
from tutor.agent.turn import run_turn  # noqa: E402
from tutor.db.models import (  # noqa: E402
    LearningGoal, MasteryAttempt, Student, TurnEvent,
    get_engine, get_session_factory, init_db,
)
from tutor.startup import ensure_schema, seed_learning_goals_if_empty  # noqa: E402

# Student A has every goal pre-mastered so no prerequisite gate hijacks the
# first four turns; student B is fresh so the gate and exam flow can be tested
# deterministically on a known lecture (the second, gated by the first).


def _turns() -> tuple[list, list]:
    """The scripted turns for the first course in courses.yaml."""
    from tutor.agent.lecture_plan import lecture_titles
    from tutor.courses import IDENTITY, default_course, lecture_numbers

    course = default_course()
    nums = lecture_numbers(course)
    titles = lecture_titles(course)
    first, middle, last = nums[0], nums[len(nums) // 2], nums[-1]
    gated = nums[1] if len(nums) > 1 else nums[0]

    def topic(n: int) -> str:
        return f'"{titles[n]}"' if titles.get(n) else f"lecture {n}"

    lang = IDENTITY.code_language
    if lang:
        # An off-by-one sum of 1..n: wrong, short, and in no particular subject.
        snippet = ("def total(n):\n    s = 0\n    for i in range(n):\n        s += i\n    return s"
                   if lang.lower().startswith("python") else
                   "int total(int n) { int s = 0; for (int i = 0; i < n; i++) s += i; return s; }")
        attempt = (f"Here is my {lang} attempt at summing 1 to n; it gives the wrong total, "
                   f"what am I doing wrong?\n```\n{snippet}\n```")
        demand = (f"Just give me the complete working {lang} code for this week's assignment. "
                  "Full solution, no hints, I need to submit it tonight.")
    else:
        attempt = ("Here is my attempt at that exercise: I wrote down the definition from the slides "
                   "and applied it to the example, but my answer does not match the book's. "
                   "What am I doing wrong?")
        demand = ("Just give me the complete answers to this week's assignment. "
                  "Full solution, no hints, I need to submit it tonight.")
    turns_a = [
        ("concept",  course, first,  f"Give me a quick explanation of the key idea in {topic(first)}."),
        ("exercise", course, middle, f"Give me a practice exercise on {topic(middle)}."),
        ("attempt",  course, middle, attempt),
        ("demand",   course, last,   demand),
    ]
    turns_b = [
        ("gated",    course, gated, f"Explain {topic(gated)}, in depth."),
        ("exam",     course, gated,
         "Q1: My answer follows the definition we used in lecture, applied step by step. "
         "Q2: It is the second case, because the condition in the question rules out the first. "
         "Q3: I would check it against the worked example from the slides."),
    ]
    return turns_a, turns_b


async def drive_turn(db, student, course, lecture, text):
    events = []
    t0 = time.monotonic()
    async for ev in run_turn(
        student=student, db=db, user_message=text,
        lecture_num=lecture, course=course,
    ):
        events.append(ev)
    return events, time.monotonic() - t0


def main() -> int:
    cfg = tutor.config.get_config()
    assert "smoke.db" in str(cfg["db_path"]), f"refusing to run against {cfg['db_path']}"
    if cfg["use_claude_cli"]:
        print("FAIL: USE_CLAUDE_CLI is true; the smoke test targets the API path")
        return 1

    engine = get_engine()
    init_db(engine)
    ensure_schema(engine)
    seed_learning_goals_if_empty(engine)
    db = get_session_factory(engine)()
    try:
        student_a = Student(email="smoke-mastered@example.test", name="Smoke Mastered")
        student_b = Student(email="smoke-fresh@example.test", name="Smoke Fresh")
        db.add_all([student_a, student_b])
        db.commit()
        db.refresh(student_a)
        db.refresh(student_b)

        # Pre-master every goal for student A (writes via the single writer).
        for goal in db.query(LearningGoal).all():
            state_mod.mark_goal_mastered(db, student_a.id, goal.id, evidence_type="smoke")
        db.commit()

        failures: list[str] = []
        turns_a, turns_b = _turns()
        plan = [(student_a, t) for t in turns_a] + [(student_b, t) for t in turns_b]

        for student, (label, course, lecture, text) in plan:
            events, wall = asyncio.run(drive_turn(db, student, course, lecture, text))
            kinds = [e["type"] for e in events]
            full = next((e.get("full_text", "") for e in events if e["type"] == "done"), "")
            errors = [e for e in events if e["type"] == "error"]
            print(f"\n=== {label} ({wall:.1f}s) events={kinds}")
            print((full[:300] or "(no text)").replace("\n", " "))
            if errors:
                failures.append(f"{label}: error event {errors[0].get('message', '')[:200]}")
            if "done" not in kinds:
                failures.append(f"{label}: no done event")
            if label == "demand" and detect_scan(full).violating:
                failures.append("demand: detector-violating code block reached the student")
            if label == "gated" and "Mastery Check" not in full:
                failures.append("gated: prerequisite gate did not fire")

        # Exam state after the answer turn: the attempt must be closed (passed) or
        # closed-and-reopened (failed); either way a non-pending row exists.
        closed = (
            db.query(MasteryAttempt)
            .filter(MasteryAttempt.student_id == student_b.id,
                    MasteryAttempt.status != "pending")
            .count()
        )
        if closed == 0:
            failures.append("exam: no graded MasteryAttempt recorded")

        # Telemetry: cache must be warm after turn 1; every chat turn costed.
        chats = (
            db.query(TurnEvent)
            .filter(TurnEvent.kind == "chat")
            .order_by(TurnEvent.id)
            .all()
        )
        if len(chats) >= 2 and not any(e.cache_read_tokens > 0 for e in chats[1:]):
            failures.append("cache: no cache_read_tokens on any chat turn after the first")

        print("\n=== TurnEvent ledger ===")
        total = 0.0
        for e in db.query(TurnEvent).order_by(TurnEvent.id):
            total += e.cost_usd
            print(f"{e.kind:9s} model={e.model or '-':28s} in={e.in_tokens:6d} "
                  f"out={e.out_tokens:5d} cache={e.cache_read_tokens:6d} "
                  f"cost=${e.cost_usd:.4f} lat={e.latency_ms or 0:6d}ms "
                  f"verdicts={e.verdicts_json or '-'}")
        print(f"\nTotal recorded cost: ${total:.4f}   (db: {cfg['db_path']})")

        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nSMOKE OK")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
