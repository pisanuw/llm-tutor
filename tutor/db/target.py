"""Production-database fence for every command-line tool that touches the DB.

``tutor.db.models.get_engine()`` silently honours ``.env``'s DATABASE_URL, and
the maintainer's ``.env`` points at production Supabase, so a command meant for
a local SQLite copy can mutate production instead (this has happened).
Every CLI in ``scripts/`` and ``pipeline/`` goes through these:

  - print_target(engine)   always prints which database is about to be used.
  - require_prod_ok(...)    refuses a WRITE against a non-SQLite (production)
                            DB unless the operator passes --allow-prod or sets
                            TUTOR_ALLOW_PROD_WRITES=1.
  - prepare_schema(...)     creates missing tables on SQLite only; on Postgres
                            the schema belongs to the migrations and app boot.

Read-only and dry-run paths are never blocked; only the write is.
(Plan step 0.9; this module used to live at scripts/_db_target.py, which
now re-exports it.)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

ALLOW_ENV = "TUTOR_ALLOW_PROD_WRITES"


@dataclass(frozen=True)
class DBTarget:
    backend: str       # "sqlite" | "postgresql" | ...
    display: str       # password-masked, human-readable
    is_sqlite: bool


def describe_engine(engine) -> DBTarget:
    """Summarise an engine's target without ever exposing the password."""
    url = engine.url
    backend = url.get_backend_name()
    if backend == "sqlite":
        return DBTarget(backend, f"sqlite: {url.database}", True)
    host = url.host or "?"
    port = f":{url.port}" if url.port else ""
    user = url.username or "?"
    return DBTarget(backend, f"{backend}: {user}@{host}{port}/{url.database or '?'}", False)


def print_target(engine) -> DBTarget:
    """Print (to stderr) which database the script is pointed at; return it."""
    target = describe_engine(engine)
    print(f"Target database -> {target.display}", file=sys.stderr)
    return target


def _opt_in() -> bool:
    return os.getenv(ALLOW_ENV, "").strip().lower() in ("1", "true", "yes")


def require_prod_ok(target: DBTarget, *, allow_prod: bool, action: str) -> None:
    """Refuse a write against a non-SQLite (production) DB without explicit opt-in.

    No-op for SQLite. For any other backend, proceeds only when --allow-prod was
    passed (``allow_prod``) or TUTOR_ALLOW_PROD_WRITES is set; otherwise prints an
    explanation and exits(2) BEFORE the script connects or mutates anything.
    """
    if target.is_sqlite:
        return
    if allow_prod or _opt_in():
        print(f"--allow-prod set: will {action} against {target.display}.",
              file=sys.stderr)
        return
    print(
        f"\nREFUSING to {action} against a non-SQLite database ({target.display}).\n"
        f"DATABASE_URL is set in the environment/.env, so this is almost "
        f"certainly production.\n"
        f"If you really intend to {action} there, re-run with --allow-prod "
        f"(or set {ALLOW_ENV}=1).\n",
        file=sys.stderr,
    )
    raise SystemExit(2)


def prepare_schema(engine, target: DBTarget) -> None:
    """Create missing tables on a local SQLite database, and nowhere else.

    On Postgres, ``create_all`` from a command-line tool would create any table
    the live database lacks with row-level security off, exposed to Supabase's
    anonymous API until the next app boot re-asserts RLS. The migrations and
    the app's startup own the production schema.
    """
    if target.is_sqlite:
        from tutor.db.models import init_db

        init_db(engine)
