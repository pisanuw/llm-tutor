"""Tests for the idempotent schema-ensure migration (Task 5 column)."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from tutor.startup import ensure_rls, ensure_schema


def test_ensure_schema_adds_missing_column():
    engine = create_engine("sqlite:///:memory:")
    # A pre-Task-5 student_progress table (no mastery_score).
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE student_progress "
            "(id INTEGER PRIMARY KEY, student_id INTEGER, goal_id TEXT, status TEXT)"
        ))

    ensure_schema(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("student_progress")}
    assert "mastery_score" in cols

    # Idempotent: a second run is a no-op (no error).
    ensure_schema(engine)


def test_ensure_schema_noop_when_table_absent():
    engine = create_engine("sqlite:///:memory:")
    ensure_schema(engine)  # no student_progress table yet -> nothing to do
    assert "student_progress" not in inspect(engine).get_table_names()


def test_ensure_rls_noop_on_sqlite():
    # SQLite has no row-level security; ensure_rls must short-circuit on the
    # dialect guard without raising. (The Postgres path is verified live.)
    engine = create_engine("sqlite:///:memory:")
    ensure_rls(engine)
