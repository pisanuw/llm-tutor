"""Tests for the Alembic migrations (migrations/).

Guards two things:
  1. Every ORM table is RLS-enabled by some revision: the union of the
     revisions' _RLS_TABLES lists must equal the ORM metadata, so a newly added
     table cannot silently ship RLS-disabled (the exact bug the migration exists
     to prevent). A new revision that creates tables declares its own list.
  2. `alembic upgrade head` brings up the full schema on a fresh SQLite DB.
  3. No drift (plan step 2.8): a migrated database, and a database the app
     built at boot then adopted by tutor.migrate, both match the models in
     columns, nullability, and indexes; migrating leaves app loggers on.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from tutor.db.models import Base

_VERSIONS = (
    pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"
)


def _load_revisions() -> list:
    """Import every revision module under migrations/versions/."""
    mods = []
    for path in sorted(_VERSIONS.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"rev_{path.stem}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods.append(mod)
    assert mods, "no migration revisions found"
    return mods


def test_rls_table_list_matches_models():
    """Every ORM table must be RLS-enabled by some revision, and vice versa.

    If this fails after adding a model, add the new table to the _RLS_TABLES of
    the revision that creates it (with ALTER ... ENABLE ROW LEVEL SECURITY) so
    it is born secure.
    """
    rls_tables: set[str] = set()
    for mod in _load_revisions():
        rls_tables |= set(getattr(mod, "_RLS_TABLES", ()))
    model_tables = set(Base.metadata.tables.keys())
    assert rls_tables == model_tables, (
        f"RLS list out of sync with models. "
        f"Missing from _RLS_TABLES: {model_tables - rls_tables}; "
        f"stale in _RLS_TABLES: {rls_tables - model_tables}"
    )


def test_every_revision_declares_its_rls_tables():
    """A revision that creates tables must name them in _RLS_TABLES.

    Cheap structural check: any revision whose upgrade() calls create_table has
    to carry a non-empty _RLS_TABLES, so the security step cannot be forgotten
    in a new migration.
    """
    import ast

    for path in sorted(_VERSIONS.glob("*.py")):
        src = path.read_text()
        if "op.create_table(" not in src:
            continue
        tree = ast.parse(src)
        names = {
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        assert "_RLS_TABLES" in names, f"{path.name} creates tables without _RLS_TABLES"


def test_alembic_upgrade_head_creates_schema(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    import tutor.config as config

    db = tmp_path / "mig.db"
    monkeypatch.setenv("TUTOR_DB", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # get_config() is lru_cached; bust it so get_engine() (via env.py) picks up
    # our TUTOR_DB rather than a path cached by an earlier test.
    config.get_config.cache_clear()

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))

    try:
        command.upgrade(cfg, "head")
    finally:
        config.get_config.cache_clear()

    import sqlite3

    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    # All ORM tables plus alembic's bookkeeping table.
    assert set(Base.metadata.tables.keys()) <= names
    assert "alembic_version" in names


# ---------------------------------------------------------------------------
# Plan step 2.8 (findings C6, C7, C25, C13, E7): no drift between the
# migrations, the boot setup, and the models.
# ---------------------------------------------------------------------------

def _shape(engine) -> dict:
    """{table: (columns with nullability, index column tuples)}."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    return {
        t: ({c["name"]: c["nullable"] for c in insp.get_columns(t)},
            {tuple(i["column_names"]) for i in insp.get_indexes(t)})
        for t in insp.get_table_names() if t != "alembic_version"
    }


def _models_shape() -> dict:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return _shape(engine)


def _head() -> str:
    from alembic.script import ScriptDirectory

    from tutor import migrate

    return ScriptDirectory.from_config(migrate.alembic_config()).get_current_head()


@pytest.fixture
def mig_db(tmp_path, monkeypatch):
    import tutor.config as config

    db = tmp_path / "mig.db"
    monkeypatch.setenv("TUTOR_DB", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config.get_config.cache_clear()
    yield db
    config.get_config.cache_clear()


def test_a_migrated_database_matches_the_models(mig_db):
    from tutor import migrate
    from tutor.db.models import get_engine

    assert migrate.migrate() == "upgraded"
    engine = get_engine()
    assert _shape(engine) == _models_shape()
    engine.dispose()
    assert migrate.migrate() == "upgraded"          # a second run changes nothing


def test_a_database_the_app_built_at_boot_is_adopted(mig_db):
    """The live database: baseline tables, no alembic_version row, columns
    added nullable by the boot ALTER, newer tables from create_all."""
    from alembic import command
    from sqlalchemy import text

    from tutor import migrate
    from tutor.db.models import get_engine
    from tutor.startup import ensure_schema

    command.upgrade(migrate.alembic_config(), migrate.BASELINE)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text(
            "INSERT INTO students (email, name, created_at, last_seen, login_count, restricted) "
            "VALUES ('a@b', 'A', '2026-01-01', '2026-01-01', 0, 0)"))
    ensure_schema(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE students SET is_instructor = NULL"))
    engine.dispose()

    assert migrate.needs_adoption() is True
    assert migrate.migrate().startswith("stamped at the baseline")
    engine = get_engine()
    assert _shape(engine) == _models_shape()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _head()
        assert conn.execute(text("SELECT is_instructor FROM students")).scalar() == 0
    engine.dispose()


def test_running_migrations_leaves_app_loggers_on(mig_db):
    """C13: fileConfig's default disabled every existing logger, so running
    migrations in-process silenced the app (and broke the logging tests
    when test_migrations ran first)."""
    import logging

    from tutor import migrate

    app_logger = logging.getLogger("tutor.migration-probe")
    migrate.migrate()
    assert app_logger.disabled is False
    assert logging.getLogger("tutor").disabled is False
