"""``python -m tutor.analytics``: export the data, then analyse it offline.

    python -m tutor.analytics export --out ~/tutor-data/tutor.sqlite
    python -m tutor.analytics export --out ... --since 2026-09-20 --class-id 3
    python -m tutor.analytics export --out ... --with-emails              # identifiable roster
    python -m tutor.analytics export --out ... --from-url https://tutor.example.edu   # over HTTP, admin token

The export reads the database ``DATABASE_URL`` names (production, from the
maintainer's .env), read-only, and says so; or, with ``--from-url``, the
app's admin export route with the token in ``--admin-token`` /
``TUTOR_ADMIN_TOKEN`` / ``ADMIN_TOKEN``. Everything else reads the local
file only. The default file is ``tutor-analytics.sqlite`` in the working
directory; keep it out of the repository (it is student data, pseudonymous
or not).
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path
from typing import Optional

DEFAULT_STORE = "tutor-analytics.sqlite"


def _date(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"--since wants YYYY-MM-DD, not {value!r}") from None


def _export(args: argparse.Namespace) -> int:
    from tutor.analytics import store

    options = store.ExportOptions(
        since=_date(args.since), class_id=args.class_id, with_emails=args.with_emails,
        with_content=args.with_content, full=args.full,
    )
    con = store.open_store(Path(args.out))
    log = (lambda m: print(m, file=sys.stderr)) if not args.quiet else (lambda m: None)
    try:
        if args.from_url:
            token = args.admin_token or os.getenv("TUTOR_ADMIN_TOKEN") or os.getenv("ADMIN_TOKEN") or ""
            if not token:
                print("--from-url needs an admin token (--admin-token, TUTOR_ADMIN_TOKEN or ADMIN_TOKEN).",
                      file=sys.stderr)
                return 2
            print(f"Source -> {args.from_url} (admin export route)", file=sys.stderr)
            reader = store.http_reader(args.from_url, token, options)
            counts = store.export(con, reader, options, log)
        else:
            from tutor.db.models import get_engine, get_session_factory
            from tutor.db.target import print_target

            engine = get_engine()
            print_target(engine)          # read-only: never blocked, always named
            db = get_session_factory(engine)()
            try:
                counts = store.export(con, store.db_reader(db, options), options, log)
            finally:
                db.close()
    finally:
        con.close()
    total = sum(counts.values())
    flags = [f for f, on in (("emails", args.with_emails), ("content", args.with_content)) if on]
    print(f"Wrote {total} rows to {args.out}" + (f" (with {', '.join(flags)})" if flags else ""),
          file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tutor.analytics", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="copy the data into the local SQLite store")
    exp.add_argument("--out", default=DEFAULT_STORE, help=f"the SQLite file (default {DEFAULT_STORE})")
    exp.add_argument("--since", help="keep only rows from this date on (YYYY-MM-DD) on the first pull")
    exp.add_argument("--class-id", type=int, help="only one class's roster (and that class)")
    exp.add_argument("--with-emails", action="store_true", help="include emails and names (identifiable)")
    exp.add_argument("--with-content", action="store_true", help="include message text")
    exp.add_argument("--full", action="store_true", help="re-read every table from the start")
    exp.add_argument("--from-url", help="read from the app's admin export route instead of the database")
    exp.add_argument("--admin-token", help="the admin token for --from-url (else TUTOR_ADMIN_TOKEN, ADMIN_TOKEN)")
    exp.add_argument("--quiet", action="store_true")
    exp.set_defaults(func=_export)

    from tutor.analytics import cli as analysis_cli

    analysis_cli.add_commands(sub)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
