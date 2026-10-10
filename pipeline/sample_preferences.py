"""Sample tutor responses into pairwise-preference items (Session 25).

Draws (student turn, tutor response) pairs from the ledger and forms blind A/B
items for a TA to rate (which of two replies teaches better). The pairing and
stratification are pure (testable); the DB collection wraps them.

    python -m pipeline sample-preferences --n 50
    python -m pipeline sample-preferences --dry-run

Writes docs/sim/preference-pairs.jsonl. A real pilot replays each context under
two configs (strategist on/off, etc.); until then this temporal-pairs mode pairs
two real responses on the same goal so the rating workflow can be exercised.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import click
from rich.console import Console

ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "sim" / "preference-pairs.jsonl"
console = Console()


def make_pairs(items: list[dict], n: int, seed: int = 0) -> list[dict]:
    """Form up to ``n`` blind A/B pairs from response items.

    Each item: {goal_id, rung, context, response}. Pairs two *different*
    responses on the same goal so the comparison is apples-to-apples. Pure.
    """
    by_goal: dict[str, list[dict]] = {}
    for it in items:
        by_goal.setdefault(it.get("goal_id") or "_none", []).append(it)

    rng = random.Random(seed)
    pairs: list[dict] = []
    for goal, group in sorted(by_goal.items()):
        if len(group) < 2:
            continue
        shuffled = group[:]
        rng.shuffle(shuffled)
        for i in range(0, len(shuffled) - 1, 2):
            a, b = shuffled[i], shuffled[i + 1]
            pairs.append({
                "pair_id": f"{goal}-{i//2}",
                "goal_id": goal,
                "rung": a.get("rung"),
                "context": a.get("context", ""),
                "response_a": a.get("response", ""),
                "response_b": b.get("response", ""),
            })
    rng.shuffle(pairs)
    return pairs[:n]


def collect_items(db, limit: int = 500) -> list[dict]:
    """Pull recent (student turn -> tutor reply) items from the ledger."""
    from tutor.db.models import Message

    items: list[dict] = []
    assistants = (
        db.query(Message)
        .filter(Message.role == "assistant")
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    for m in assistants:
        # The preceding user message is the context (best-effort).
        prev = (
            db.query(Message)
            .filter(Message.student_id == m.student_id, Message.role == "user",
                    Message.id < m.id)
            .order_by(Message.id.desc())
            .first()
        )
        # Strip PII: only the message bodies, no names/emails.
        items.append({
            "goal_id": (m.course or "") + "-L" + str(m.lecture_num or 0),
            "rung": None,
            "context": (prev.content[:500] if prev else ""),
            "response": m.content[:2000],
        })
    return items


@click.command("sample-preferences")
@click.option("--n", default=50, show_default=True, help="Number of pairs to draw.")
@click.option("--seed", default=0, show_default=True)
@click.option("--dry-run", is_flag=True, help="Report counts, write nothing.")
def sample_preferences(n: int, seed: int, dry_run: bool) -> None:
    from tutor.db.models import get_engine, get_session_factory
    from tutor.db.target import prepare_schema, print_target

    engine = get_engine()
    prepare_schema(engine, print_target(engine))   # read-only report
    db = get_session_factory(engine)()
    try:
        items = collect_items(db)
    finally:
        db.close()

    pairs = make_pairs(items, n=n, seed=seed)
    console.print(f"[bold]{len(items)}[/bold] responses -> [bold]{len(pairs)}[/bold] pairs")
    if dry_run or not pairs:
        if not pairs:
            console.print("[yellow]No pairs (need >=2 responses per goal). "
                          "Nothing written.[/yellow]")
        return
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    console.print(f"[green]Wrote {len(pairs)} pairs to {OUT_PATH.relative_to(ROOT)}[/green]")


if __name__ == "__main__":
    sample_preferences()
