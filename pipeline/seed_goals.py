"""Seed learning_goals.json into the DB (idempotent upsert).

The app seeds goals only into an empty table (first boot); after an edit to
learning_goals.json, run this against the database to bring it up to date:
titles, descriptions, anchors, hooks and threshold flags are updated, new
goals inserted, and the prerequisite edges replaced. Goals missing from the
file are left in place (students' progress points at them).

Usage:
    python -m pipeline.seed_goals --dry-run
    python -m pipeline.seed_goals                      # local SQLite (TUTOR_DB)
    DATABASE_URL=... python -m pipeline.seed_goals --allow-prod
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

sys.path.insert(0, str(ROOT))

from tutor.db.models import GoalPrerequisite, LearningGoal, get_engine, get_session_factory  # noqa: E402
from tutor.db.target import prepare_schema, print_target, require_prod_ok  # noqa: E402
from tutor.paths import CURRICULUM_DIR  # noqa: E402

DEFAULT_JSON = CURRICULUM_DIR / "learning_goals.json"


def seed(json_path: Path, dry_run: bool = False, allow_prod: bool = False) -> None:
    data = json.loads(json_path.read_text())
    goals = data.get("goals", [])
    edges = data.get("edges", [])

    print(f"Loaded {len(goals)} goals and {len(edges)} edges from {json_path}")

    if dry_run:
        for g in goals[:5]:
            print(f"  Goal: {g['id']} — {g['title']}")
        print(f"  ... (showing first 5 of {len(goals)})")
        for e in edges[:5]:
            print(f"  Edge: {e['from']} -> {e['to']}")
        print(f"  ... (showing first 5 of {len(edges)})")
        print("Dry run — no DB changes.")
        return

    engine = get_engine()
    target = print_target(engine)
    require_prod_ok(target, allow_prod=allow_prod,
                    action="replace learning goals and their prerequisite edges")
    prepare_schema(engine, target)
    SessionFactory = get_session_factory(engine)

    with SessionFactory() as session:
        # Upsert goals
        inserted = updated = 0

        for g in goals:
            obj = session.get(LearningGoal, g["id"])
            if obj is None:
                obj = LearningGoal(id=g["id"])
                session.add(obj)
                inserted += 1
            else:
                updated += 1
            # Every field the app reads, as startup.seed_learning_goals_if_empty
            # writes it on a first boot, so an edited file fully reaches the map.
            obj.title = g["title"]
            obj.description = g.get("description", "")
            obj.course = g["course"]
            obj.lecture_num = int(g["lecture"])
            sa, ba = g.get("slide_anchor"), g.get("book_anchor")
            obj.slide_anchor = json.dumps(sa) if sa else None
            obj.book_anchor = json.dumps(ba) if ba else None
            obj.hook = g.get("hook")
            obj.threshold = bool(g.get("threshold", False))

        session.flush()
        print(f"Goals: {inserted} inserted, {updated} updated.")

        # Upsert edges (delete all then reinsert — simpler than diffing)
        deleted = session.query(GoalPrerequisite).delete()
        print(f"Edges: cleared {deleted} existing edges.")

        valid_ids = {g["id"] for g in goals}
        edge_objs = []
        skipped = 0
        for e in edges:
            if e["from"] not in valid_ids or e["to"] not in valid_ids:
                skipped += 1
                continue
            edge_objs.append(GoalPrerequisite(from_goal_id=e["from"], to_goal_id=e["to"]))

        session.bulk_save_objects(edge_objs)
        print(f"Edges: inserted {len(edge_objs)} ({skipped} skipped — unknown goal IDs).")

        session.commit()
        print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed learning goals into the DB")
    parser.add_argument("--json", default=str(DEFAULT_JSON), help="Path to learning_goals.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-prod", action="store_true",
                        help="Allow writing to a non-SQLite (production) database")
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"Error: {json_path} not found. Run python -m pipeline extract-goals first.", file=sys.stderr)
        sys.exit(1)

    seed(json_path, dry_run=args.dry_run, allow_prod=args.allow_prod)


if __name__ == "__main__":
    main()
