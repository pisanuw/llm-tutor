"""Scaffold deterministic test cases for the exercise bank (Session 19).

`curriculum/exercise_tests.json` holds the stdin/stdout cases that run_code uses
to judge a submission deterministically. This command emits a *stub* entry for
every code exercise that has none yet (so an author or an LLM pass can fill in
the I/O contract), and never overwrites an entry that already has tests.

    python -m pipeline build-exercise-tests            # add stubs in place
    python -m pipeline build-exercise-tests --dry-run  # report only

The handoff (Task 6) calls for a batch Haiku pass that reads each exercise's
expected_behavior and proposes test cases. That LLM pass is a documented
follow-up: it would consume these stubs, write concrete {stdin, expected}
rows, and a human spot-checks ~20. Per the repo cost rule it should run through
the local `claude` CLI, not the API. The stubs here are the reproducible
seed it starts from; nothing in this command calls an LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from tutor.paths import CURRICULUM_DIR

ROOT = Path(__file__).parent.parent
EXERCISES_PATH = CURRICULUM_DIR / "exercises.json"
TESTS_PATH = CURRICULUM_DIR / "exercise_tests.json"

console = Console()


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


@click.command("build-exercise-tests")
@click.option("--dry-run", is_flag=True, help="Report what would be added, write nothing.")
def build_exercise_tests(dry_run: bool) -> None:
    bank = json.loads(EXERCISES_PATH.read_text())["exercises"]
    tests = _load(TESTS_PATH)

    code_exercises = [e for e in bank if e["difficulty"] == "code"]
    have = {k for k, v in tests.items() if v.get("tests")}
    missing = [e for e in code_exercises if e["id"] not in have]

    console.print(f"[bold]{len(code_exercises)}[/bold] code exercises; "
                  f"[green]{len(have & {e['id'] for e in code_exercises})}[/green] have tests, "
                  f"[yellow]{len(missing)}[/yellow] need stubs")

    if dry_run:
        for e in missing[:10]:
            console.print(f"  stub: {e['id']}")
        if len(missing) > 10:
            console.print(f"  ... and {len(missing) - 10} more")
        return

    added = 0
    for e in missing:
        tests[e["id"]] = {
            # The expected_behavior is the author's / LLM's starting point for a
            # concrete I/O contract. Empty tests => run_code does a smoke run only.
            "contract": "",
            "expected_behavior": e["expected_behavior"],
            "tests": [],
        }
        added += 1

    TESTS_PATH.write_text(json.dumps(tests, indent=2) + "\n")
    console.print(f"[green]Added {added} stub entries to {TESTS_PATH.relative_to(ROOT)}[/green]")


if __name__ == "__main__":
    build_exercise_tests()
