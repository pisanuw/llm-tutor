"""Erase all data for a student (FERPA right to erasure, Session 26).

    python -m scripts.delete_student_data ada@example.edu          # dry-run (counts only)
    python -m scripts.delete_student_data ada@example.edu --confirm # delete (SQLite)
    python -m scripts.delete_student_data ada@example.edu --confirm --allow-prod  # prod
    python -m scripts.delete_student_data --list                   # who has data

    python -m scripts.delete_student_data demo@example.edu --messages-only           # dry-run
    python -m scripts.delete_student_data demo@example.edu --messages-only --confirm

``--messages-only`` is a reset of what was said, not an erasure of the student:
it removes the transcript, the feedback on it and the rolling conversation
summary (state.CONVERSATION_TABLES), and keeps the account, the whitelist, the
class enrolment, progress and mastery, and the telemetry ledger. The summary is
part of it because clearing the transcript alone leaves it feeding the previous
conversation into the next prompt -- the page looks blank while the tutor still
remembers. Written for a clean demo or screenshot; it is not the erasure path.

Deletes transcripts (Message), telemetry (TurnEvent + JudgeSample), graded
events, exercise/mastery attempts, progress, paths, sessions, magic links, and
the Student row. Goes through state.delete_student_data. Irreversible; requires
--confirm. The retention policy (docs/retention-policy.md) governs when to run it.

The target database (from DATABASE_URL, else local SQLite) is printed on every
run. Deleting against a non-SQLite (production) database additionally requires
--allow-prod (or TUTOR_ALLOW_PROD_WRITES=1), so a --confirm meant for a local
copy cannot silently erase prod via .env.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", nargs="?", help="student email (exact match)")
    parser.add_argument("--confirm", action="store_true", help="actually delete (else dry-run)")
    parser.add_argument("--list", action="store_true", help="list students with data")
    parser.add_argument("--messages-only", action="store_true",
                        help="clear the transcript, its feedback and the rolling summary; keep the "
                             "account, progress and telemetry")
    parser.add_argument("--allow-prod", action="store_true",
                        help="permit deletion against a non-SQLite (production) database")
    args = parser.parse_args()

    from tutor.db.target import prepare_schema, print_target, require_prod_ok
    from tutor.agent import state
    from tutor.db.models import Student, get_engine, get_session_factory

    engine = get_engine()
    target = print_target(engine)
    # Gate the destructive path before we connect: a --confirm meant for a local
    # SQLite copy must not silently erase prod when .env sets DATABASE_URL.
    if args.confirm:
        require_prod_ok(
            target, allow_prod=args.allow_prod,
            action="clear a student's conversation" if args.messages_only else "delete student data",
        )
    prepare_schema(engine, target)
    db = get_session_factory(engine)()
    try:
        if args.list:
            for s in db.query(Student).order_by(Student.email).all():
                print(f"{s.email}  (id {s.id})")
            return 0

        if not args.email:
            parser.error("email required unless --list")

        student = db.query(Student).filter_by(email=args.email.strip().lower()).first()
        if student is None:
            print(f"No student with email {args.email!r}", file=sys.stderr)
            return 1

        if args.messages_only:
            from sqlalchemy import func, select

            from tutor.db.models import Base

            scope = state.erasure_scope(student.id)
            tables = [t for t in reversed(Base.metadata.sorted_tables)
                      if t.name in state.CONVERSATION_TABLES]
            if not args.confirm:
                print(f"DRY RUN for {student.email} (id {student.id}). Would clear:")
                for table in tables:
                    n = db.execute(select(func.count()).select_from(table)
                                   .where(scope[table.name])).scalar()
                    print(f"  {table.name}: {n or 0}")
                print("The account, progress and mastery, and the telemetry ledger stay.")
                print("Re-run with --confirm to clear.")
                return 0
            counts = state.clear_conversation(db, student.id)
            db.commit()
            print(f"Cleared the conversation for {student.email}:")
            for table, n in counts.items():
                if n:
                    print(f"  {table}: {n}")
            print("The account, progress and mastery, and the telemetry ledger are untouched.")
            return 0

        if not args.confirm:
            # Dry-run: count exactly what delete_student_data would remove,
            # from the same schema-derived scope, without touching anything.
            from sqlalchemy import func, select

            from tutor.db.models import Base

            scope = state.erasure_scope(student.id)
            print(f"DRY RUN for {student.email} (id {student.id}). Would delete:")
            for table in reversed(Base.metadata.sorted_tables):
                where = scope.get(table.name)
                if where is None:
                    continue
                n = db.execute(select(func.count()).select_from(table).where(where)).scalar()
                print(f"  {table.name}: {n or 0}")
            print("It also removes this student's notes from goal_misconceptions examples.")
            print("Re-run with --confirm to delete.")
            return 0

        counts = state.delete_student_data(db, student.id)
        db.commit()
        print(f"Deleted data for {args.email}:")
        for table, n in counts.items():
            if n:
                print(f"  {table}: {n}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
