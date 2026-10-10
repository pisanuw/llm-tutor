"""The database half of the monthly restore drill (plan step 6.4, finding E3).

.github/workflows/db-restore-drill.yml dumps a source database, puts the dump
through the same age encryption the nightly backup uses (with a throwaway
key), restores its public schema into a scratch Postgres, and runs ``check``
on the result. In synthetic mode (a push or pull request that changes the
drill or the migrations) the source is built by the migrations and ``seed``;
on the monthly schedule it is production, read with pg_dump only.

It prints table names and row counts, never rows: a production restore holds
every student's transcripts.

    python scripts/restore_drill.py seed  --dsn postgresql+psycopg://...
    python scripts/restore_drill.py check --dsn postgresql+psycopg://... --nonempty students [--expect-head]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _engine(dsn: str) -> Engine:
    # psycopg 3 is the driver the app ships (requirements-web.txt).
    if dsn.startswith("postgresql://"):
        dsn = "postgresql+psycopg://" + dsn[len("postgresql://"):]
    return create_engine(dsn)


def migrations_head() -> str:
    from alembic.script import ScriptDirectory

    from tutor.migrate import alembic_config

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def seed(engine: Engine) -> dict[str, int]:
    """Make a database the migrations built look like production: the app's
    boot-time row level security pass (which also covers alembic_version),
    then made-up rows in the tables a restore must bring back. Returns the
    rows added per table."""
    from sqlalchemy.orm import Session

    from tutor.courses import default_course
    from tutor.db.models import LearningGoal, Message, Student, StudentProgress, TurnEvent
    from tutor.startup import ensure_rls

    course = default_course()
    ensure_rls(engine)
    with Session(engine) as db:
        student = Student(email="drill@example.test", name="Restore Drill")
        goal = LearningGoal(id=f"{course}-L01-drill", title="Drill goal", description="", course=course,
                            lecture_num=1)
        db.add_all([student, goal])
        db.flush()
        db.add_all([
            Message(student_id=student.id, role="user", content="What does a restore prove?", course=course),
            Message(student_id=student.id, role="assistant", content="That the backup is usable.", course=course),
            TurnEvent(student_id=student.id, kind="chat", intent="QUESTION", rung_used=1),
            StudentProgress(student_id=student.id, goal_id=goal.id, status="in_progress"),
        ])
        db.commit()
    return {"students": 1, "learning_goals": 1, "messages": 2, "turn_events": 1, "student_progress": 1}


@dataclass
class DrillReport:
    counts: dict[str, int] = field(default_factory=dict)
    revision: str | None = None
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check(engine: Engine, *, nonempty: tuple[str, ...] = ("students",), expect_head: bool = False) -> DrillReport:
    """Does the restored database hold the app? Every model table present,
    one Alembic revision, the named tables non-empty, and (on Postgres) row
    level security still on for every public table."""
    from tutor.db.models import Base

    report = DrillReport()
    present = set(inspect(engine).get_table_names())
    expected = set(Base.metadata.tables)
    missing = sorted(expected - present)
    if missing:
        report.failures.append(f"tables missing after the restore: {', '.join(missing)}")
    with engine.connect() as conn:
        for name in sorted(expected & present):
            report.counts[name] = conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
        if "alembic_version" in present:
            revisions = [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]
            if len(revisions) != 1:
                report.failures.append(f"alembic_version holds {len(revisions)} rows, not 1")
            else:
                report.revision = revisions[0]
        else:
            report.failures.append("no alembic_version table: the migrations never ran on the source")
        if engine.dialect.name == "postgresql":
            open_tables = [r[0] for r in conn.execute(text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"))]
            if open_tables:
                report.failures.append("row level security is off after the restore on: "
                                       + ", ".join(sorted(open_tables)))
    for name in nonempty:
        if report.counts.get(name, 0) == 0:
            report.failures.append(f"{name} is empty after the restore")
    if report.revision is not None:
        head = migrations_head()
        if report.revision != head:
            message = f"the restored database is at revision {report.revision}; the migrations' head is {head}"
            (report.failures if expect_head else report.notes).append(message)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("seed", help="add made-up rows to a database the migrations built")
    s.add_argument("--dsn", required=True)
    c = sub.add_parser("check", help="check a restored database")
    c.add_argument("--dsn", required=True)
    c.add_argument("--nonempty", default="students", help="comma-separated tables that must have rows")
    c.add_argument("--expect-head", action="store_true",
                   help="fail unless the restore is at the migrations' head (synthetic mode)")
    args = parser.parse_args(argv)

    engine = _engine(args.dsn)
    if args.command == "seed":
        added = seed(engine)
        print("seeded: " + ", ".join(f"{t} {n}" for t, n in added.items()))
        return 0

    report = check(engine, nonempty=tuple(t for t in args.nonempty.split(",") if t),
                   expect_head=args.expect_head)
    print(f"restored {len(report.counts)} app tables at revision {report.revision}")
    for name, n in sorted(report.counts.items()):
        print(f"  {name}: {n}")
    for note in report.notes:
        print(f"note: {note}")
    for failure in report.failures:
        print(f"::error::{failure}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
