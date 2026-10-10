"""Bring the database to the Alembic head (plan step 2.8, finding E7).

entrypoint.sh runs ``python -m tutor.migrate`` before the app starts (the
default for decision 8). It handles the three states a database can be in:

* versioned: upgraded as usual;
* built by the app at boot before migrations ran for real (tables from
  create_all, columns from startup.ensure_schema, no ``alembic_version`` row):
  stamped at the baseline, whose tables it already has, then upgraded. Every
  revision after the baseline is guarded, so that is safe;
* empty: built by the migrations, so every table is born with row-level
  security (the reason this runs before the app's create_all).

Exit status 0 on success and 1 on failure; the entrypoint then warns and boots
anyway, and startup.ensure_schema still adds any missing column.
"""

from __future__ import annotations

import sys
from pathlib import Path

BASELINE = "bb5bc75ceadf"
ROOT = Path(__file__).resolve().parent.parent


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def needs_adoption() -> bool:
    """True for a database the app built without the migrations."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect

    from tutor.db.models import get_engine

    engine = get_engine()
    try:
        with engine.connect() as conn:
            if "students" not in inspect(conn).get_table_names():
                return False                     # empty: the migrations build it
            return MigrationContext.configure(conn).get_current_revision() is None
    finally:
        engine.dispose()


def migrate() -> str:
    """Upgrade to head, adopting an unversioned app-built database first."""
    from alembic import command

    cfg = alembic_config()
    adopted = needs_adoption()
    if adopted:
        command.stamp(cfg, BASELINE)
    command.upgrade(cfg, "head")
    return f"stamped at the baseline ({BASELINE}), then upgraded" if adopted else "upgraded"


def main() -> int:
    try:
        outcome = migrate()
    except Exception as exc:  # noqa: BLE001 - boot continues either way
        print(f"[migrate] FAILED: {type(exc).__name__}: {str(exc)[:400]}", file=sys.stderr)
        return 1
    print(f"[migrate] database {outcome}; now at head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
