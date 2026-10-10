"""Clear (or set) a student's conduct restriction from the command line.

Until the Phase F admin page grows the button, this is the instructor's
lever for the Session 10 restriction (envelope capped at H1):

    python -m scripts.clear_restriction ada@example.edu          # clear
    python -m scripts.clear_restriction ada@example.edu --set    # re-restrict
    python -m scripts.clear_restriction --list                  # who is restricted

Writes go through state.set_student_restricted (single writer); each change
is recorded as a 'restriction' TurnEvent the admin page can surface.

The target database (from DATABASE_URL, else local SQLite) is printed on every
run. Changing a restriction against a non-SQLite (production) database requires
--allow-prod (or TUTOR_ALLOW_PROD_WRITES=1), so a run meant for a local copy
cannot silently mutate prod via .env. --list is read-only and never gated.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", nargs="?", help="student email (exact match)")
    parser.add_argument("--set", action="store_true", help="restrict instead of clear")
    parser.add_argument("--list", action="store_true", help="list restricted students")
    parser.add_argument("--allow-prod", action="store_true",
                        help="permit a restriction change against a non-SQLite (production) database")
    args = parser.parse_args()

    from tutor.db.target import prepare_schema, print_target, require_prod_ok
    from tutor.agent import state
    from tutor.db.models import Student, get_engine, get_session_factory

    engine = get_engine()
    target = print_target(engine)
    # --list is read-only; setting/clearing a restriction is a write, so gate it
    # before we connect: a run meant for a local SQLite copy must not silently
    # mutate prod when .env sets DATABASE_URL.
    if not args.list:
        require_prod_ok(target, allow_prod=args.allow_prod,
                        action="change a student's restriction")
    prepare_schema(engine, target)
    db = get_session_factory(engine)()
    try:
        if args.list:
            rows = db.query(Student).filter_by(restricted=True).all()
            if not rows:
                print("No restricted students.")
            for s in rows:
                print(f"{s.email}  (id {s.id})")
            return 0

        if not args.email:
            parser.error("email required unless --list")
        student = db.query(Student).filter_by(email=args.email.strip().lower()).first()
        if student is None:
            print(f"No student with email {args.email!r}", file=sys.stderr)
            return 1

        state.set_student_restricted(db, student.id, restricted=args.set)
        db.commit()
        print(f"{student.email}: restricted={'true' if args.set else 'false'}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
