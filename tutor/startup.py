"""One-time startup tasks run inside the app lifespan.

MIGRATIONS NOTE: Alembic is the source of truth for schema + RLS (see
migrations/). entrypoint.sh applies it before the app starts (python -m
tutor.migrate, plan step 2.8). The two functions below stay as an idempotent
safety net for a boot whose migration failed and for local runs without the
entrypoint; on a migrated database they are no-ops. New columns/RLS are
authored as guarded Alembic revisions, NOT added to the lists here.

ensure_schema(engine)
    Idempotently add columns that create_all cannot add to pre-existing tables.
    Safe to call on every boot, on Postgres and SQLite. (Transitional; prefer an
    Alembic revision per new column.)

ensure_rls(engine)
    Enable row-level security on any public table that lacks it (Postgres only).
    create_all makes new tables RLS-disabled, which on Supabase exposes them via
    the anon PostgREST API; this re-asserts RLS on every boot. No-op on SQLite.
    (Transitional: the baseline migration now creates tables born RLS-enabled, so
    there is no exposure window once migrations are applied.)

seed_learning_goals_if_empty(engine)
    Reads curriculum/learning_goals.json and upserts goals + edges into the DB.
    No-ops if the learning_goals table already has rows (safe to call on every boot).

ensure_database_ready(engine)
    Runs all of the above once, fail-soft: boot calls it, and /healthz calls it
    again after a healthy probe until it has succeeded (plan step 2.7).
"""
from __future__ import annotations

import json
import logging
import os
import threading

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from tutor.paths import CURRICULUM_DIR

logger = logging.getLogger("tutor")

_GOALS_JSON = CURRICULUM_DIR / "learning_goals.json"
_TRACKS_JSON = CURRICULUM_DIR / "tracks.json"

# Columns added after a table first shipped. create_all() makes new TABLES but
# never alters existing ones, so each (table, column, type-by-dialect) here is
# applied with a guarded ALTER. Keep entries forever; they are no-ops once run.
_ADDED_COLUMNS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("student_progress", "mastery_score", {"postgresql": "DOUBLE PRECISION", "sqlite": "FLOAT"}),
    ("mastery_attempts", "trigger_message_id", {"postgresql": "INTEGER", "sqlite": "INTEGER"}),
    ("messages", "course", {"postgresql": "VARCHAR(8)", "sqlite": "VARCHAR(8)"}),
    ("messages", "tool_summary", {"postgresql": "TEXT", "sqlite": "TEXT"}),
    ("student_progress", "last_evidence_at", {"postgresql": "TIMESTAMP", "sqlite": "DATETIME"}),
    ("students", "restricted", {"postgresql": "BOOLEAN DEFAULT FALSE", "sqlite": "BOOLEAN DEFAULT 0"}),
    ("learning_goals", "slide_anchor", {"postgresql": "TEXT", "sqlite": "TEXT"}),
    ("learning_goals", "book_anchor", {"postgresql": "TEXT", "sqlite": "TEXT"}),
    ("learning_goals", "hook", {"postgresql": "TEXT", "sqlite": "TEXT"}),
    ("learning_goals", "threshold", {"postgresql": "BOOLEAN DEFAULT FALSE", "sqlite": "BOOLEAN DEFAULT 0"}),
    ("student_progress", "seen", {"postgresql": "BOOLEAN DEFAULT FALSE", "sqlite": "BOOLEAN DEFAULT 0"}),
    ("students", "last_login", {"postgresql": "TIMESTAMP", "sqlite": "DATETIME"}),
    ("students", "login_count", {"postgresql": "INTEGER DEFAULT 0", "sqlite": "INTEGER DEFAULT 0"}),
    ("sessions", "method", {"postgresql": "VARCHAR(16)", "sqlite": "VARCHAR(16)"}),
    # The 6-digit sign-in code beside the link (2026-10-02; revision b7c2d9e4f1a3).
    ("magic_links", "code", {"postgresql": "VARCHAR(8)", "sqlite": "VARCHAR(8)"}),
    ("magic_links", "code_attempts", {"postgresql": "INTEGER NOT NULL DEFAULT 0",
                                      "sqlite": "INTEGER NOT NULL DEFAULT 0"}),
    ("turn_events", "sources_json", {"postgresql": "TEXT", "sqlite": "TEXT"}),
    ("students", "anonymized_at", {"postgresql": "TIMESTAMP", "sqlite": "DATETIME"}),
    ("student_progress", "mastered_at", {"postgresql": "TIMESTAMP", "sqlite": "DATETIME"}),
    ("student_progress", "confirmed_at", {"postgresql": "TIMESTAMP", "sqlite": "DATETIME"}),
    # FSRS-4.5 spaced-repetition state (Engram integration, Phase 1).
    ("student_progress", "fsrs_stability", {"postgresql": "DOUBLE PRECISION", "sqlite": "FLOAT"}),
    ("student_progress", "fsrs_difficulty", {"postgresql": "DOUBLE PRECISION", "sqlite": "FLOAT"}),
    ("student_progress", "fsrs_due", {"postgresql": "TIMESTAMP", "sqlite": "DATETIME"}),
    ("student_progress", "fsrs_reps", {"postgresql": "INTEGER DEFAULT 0", "sqlite": "INTEGER DEFAULT 0"}),
    ("student_progress", "fsrs_lapses", {"postgresql": "INTEGER DEFAULT 0", "sqlite": "INTEGER DEFAULT 0"}),
    # Pre-reveal stated confidence (Engram integration, Phase 2).
    ("turn_events", "confidence", {"postgresql": "INTEGER", "sqlite": "INTEGER"}),
    # The turn id shared with the chat stream and the browser's activity
    # events (revision a9c4e7f1b2d3).
    ("turn_events", "turn_id", {"postgresql": "VARCHAR(32)", "sqlite": "VARCHAR(32)"}),
    # Deferred-exam snooze (gate fix, 2026-09-04).
    ("mastery_attempts", "deferred_at", {"postgresql": "TIMESTAMP", "sqlite": "DATETIME"}),
    # Instructor role (2026-09-24). The classes/class_enrollments tables are
    # created by create_all (new tables), so only the column needs an ALTER.
    ("students", "is_instructor", {"postgresql": "BOOLEAN DEFAULT FALSE", "sqlite": "BOOLEAN DEFAULT 0"}),
    # Prompt-cache writes in the cost ledger (plan step 2.1).
    ("turn_events", "cache_write_tokens", {"postgresql": "INTEGER", "sqlite": "INTEGER"}),
    # A magic link carries its email until the first login (plan step 2.9).
    ("magic_links", "email", {"postgresql": "VARCHAR(255)", "sqlite": "VARCHAR(255)"}),
)


def ensure_schema(engine: Engine) -> None:
    """Add any missing post-ship columns. Idempotent; introspection-guarded."""
    insp = inspect(engine)
    dialect = engine.dialect.name
    existing_tables = set(insp.get_table_names())
    for table, column, type_by_dialect in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue  # create_all will build it fresh, with the column
        cols = {c["name"] for c in insp.get_columns(table)}
        if column in cols:
            continue
        coltype = type_by_dialect.get(dialect, "FLOAT")
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
        logger.info("startup: added %s.%s (%s)", table, column, coltype)


def ensure_rls(engine: Engine) -> None:
    """Enable row-level security on every public table that lacks it (Postgres only).

    Supabase auto-exposes the `public` schema over PostgREST, reachable with the
    publishable anon key; with RLS off a table is world-readable/writable/deletable.
    The app connects as the `postgres` role (BYPASSRLS + table owner), so enabling
    RLS with *no* policies locks out the anon/authenticated roles while leaving the
    app's own access untouched. create_all() makes new tables RLS-disabled, so we
    re-assert this on every boot. Idempotent (only touches tables currently RLS-off)
    and fail-soft (a failure on one table is logged and never blocks startup).

    SQLite has no RLS, so this is a no-op there.
    """
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.connect() as conn:
            # quote_ident handles mixed-case names like "User"/"Session".
            # Names come from pg_catalog (our own schema), not user input.
            rows = conn.execute(text(
                "SELECT quote_ident(c.relname) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND c.relrowsecurity = false"
            )).fetchall()
    except Exception as exc:  # pragma: no cover - prod-only diagnostics
        logger.warning("startup: could not inspect RLS state: %s", exc)
        return

    enabled: list[str] = []
    for (qname,) in rows:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE public.{qname} ENABLE ROW LEVEL SECURITY"))
            enabled.append(qname)
        except Exception as exc:  # pragma: no cover - prod-only diagnostics
            logger.warning("startup: could not enable RLS on public.%s: %s", qname, exc)
    if enabled:
        logger.info("startup: enabled RLS on %d public table(s): %s",
                    len(enabled), ", ".join(enabled))


def seed_learning_goals_if_empty(engine: Engine) -> None:
    if not _GOALS_JSON.exists():
        logger.warning("startup: %s not found — learning goals map will be empty", _GOALS_JSON)
        return

    from tutor.db.models import GoalPrerequisite, LearningGoal, get_session_factory

    Session = get_session_factory(engine)
    with Session() as db:
        if db.query(LearningGoal).count() > 0:
            logger.info("startup: learning_goals already seeded — skipping")
            return

        data = json.loads(_GOALS_JSON.read_text())
        goals = data.get("goals", [])
        edges = data.get("edges", [])

        for g in goals:
            sa = g.get("slide_anchor")
            ba = g.get("book_anchor")
            db.add(LearningGoal(
                id=g["id"],
                title=g["title"],
                description=g.get("description", ""),
                course=g["course"],
                lecture_num=int(g["lecture"]),
                slide_anchor=json.dumps(sa) if sa else None,
                book_anchor=json.dumps(ba) if ba else None,
                hook=g.get("hook"),
                threshold=bool(g.get("threshold", False)),
            ))
        db.flush()

        valid_ids = {g["id"] for g in goals}
        for e in edges:
            if e["from"] in valid_ids and e["to"] in valid_ids:
                db.add(GoalPrerequisite(from_goal_id=e["from"], to_goal_id=e["to"]))

        db.commit()
        logger.info("startup: seeded %d learning goals and %d edges", len(goals), len(edges))


def seed_tracks_if_empty(engine: Engine) -> None:
    """Upsert curriculum/tracks.json into the tracks table.

    Unlike learning goals, tracks are always re-seeded when the file is newer
    than the DB rows (or when the table is empty) so curriculum changes flow
    through without manual intervention.
    """
    if not _TRACKS_JSON.exists():
        logger.warning("startup: %s not found — tracks table will be empty", _TRACKS_JSON)
        return

    from tutor.db.models import Track, get_session_factory

    Session = get_session_factory(engine)
    data = json.loads(_TRACKS_JSON.read_text())
    tracks = data.get("tracks", [])

    with Session() as db:
        for t in tracks:
            existing = db.get(Track, t["id"])
            goal_ids_json = json.dumps(t["goal_ids"])
            if existing is None:
                db.add(Track(
                    id=t["id"],
                    course=t["course"],
                    title=t["title"],
                    description=t.get("description", ""),
                    goal_ids_json=goal_ids_json,
                    graph_version=data.get("graph_version", 0),
                ))
            else:
                existing.title = t["title"]
                existing.description = t.get("description", "")
                existing.goal_ids_json = goal_ids_json
                existing.graph_version = data.get("graph_version", 0)
        db.commit()
    logger.info("startup: upserted %d tracks", len(tracks))


# ---------------------------------------------------------------------------
# Fail-soft database setup (plan step 2.7, finding C4)
# ---------------------------------------------------------------------------

_ready: set[str] = set()     # database URLs whose setup has run
_ready_lock = threading.Lock()


def prepare_database(engine: Engine) -> None:
    """Create tables, add missing columns, assert RLS, seed goals and tracks."""
    from tutor.db.models import init_db

    init_db(engine)
    ensure_schema(engine)
    ensure_rls(engine)
    seed_learning_goals_if_empty(engine)
    seed_tracks_if_empty(engine)


def _key(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=True)


def database_ready(engine: Engine) -> bool:
    return _key(engine) in _ready


def ensure_database_ready(engine: Engine) -> bool:
    """Run :func:`prepare_database` until it succeeds once. Never raises.

    Boot calls it; a paused or unreachable database used to fail the
    lifespan, so uvicorn exited and Render restarted the container in a loop
    in which /healthz (and the self-heal it drives) could never run. Now the
    app starts anyway, and /healthz calls this again after each successful
    probe until the setup has run.
    """
    key = _key(engine)
    if key in _ready:
        return True
    with _ready_lock:
        if key in _ready:
            return True
        try:
            prepare_database(engine)
        except Exception:  # noqa: BLE001 - boot must survive a database outage
            logger.exception("database setup failed; the app starts and /healthz retries it")
            return False
        _ready.add(key)
        _close_interrupted_turns(engine)
        _close_interrupted_turns_later(engine)
        return True


_SWEEP_LATER_S = 16 * 60
_sweep_later_armed = False


def _close_interrupted_turns_later(engine: Engine) -> None:
    """Once more after the 15-minute floor: the turns the restart that booted
    this process cut off are younger than the floor at boot, and used to
    wait for the next deploy (the Stage 4 review). Once per process; a
    daemon timer, so it never holds the exit."""
    global _sweep_later_armed
    if _sweep_later_armed or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _sweep_later_armed = True
    timer = threading.Timer(_SWEEP_LATER_S, _close_interrupted_turns, args=(engine,))
    timer.daemon = True
    timer.start()


def _close_interrupted_turns(engine: Engine) -> None:
    """Close the turns a restart cut off (R31). Best effort: boot never stops on it."""
    from sqlalchemy.orm import sessionmaker

    from tutor.agent import state

    try:
        with sessionmaker(bind=engine)() as db:
            closed = state.close_interrupted_turns(db)
        if closed:
            logger.info("boot: closed %d turn(s) a restart cut off", closed)
    except Exception:  # noqa: BLE001
        logger.exception("boot: could not close interrupted turns")


def _reset_for_tests() -> None:
    _ready.clear()
