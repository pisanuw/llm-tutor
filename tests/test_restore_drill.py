"""Plan step 6.4 (finding E3): the database half of the restore drill.

The workflow (.github/workflows/db-restore-drill.yml) dumps, encrypts and
decrypts, and restores into a scratch Postgres; scripts/restore_drill.py seeds
the synthetic source and checks the restore. These tests run the seed and the
check on SQLite; the row level security part of the check needs Postgres and
runs in the drill itself.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from scripts import restore_drill
from tests.helpers import make_session


@pytest.fixture
def restored():
    """A database shaped like a good restore: every table, one revision at the
    head, and the seed's rows."""
    session = make_session()
    engine = session.get_bind()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": restore_drill.migrations_head()})
    restore_drill.seed(engine)
    yield engine
    session.close()


def test_a_good_restore_passes(restored):
    report = restore_drill.check(restored, nonempty=("students", "messages"), expect_head=True)
    assert report.failures == []
    assert report.counts["students"] == 1 and report.counts["messages"] == 2
    assert report.revision == restore_drill.migrations_head()


def test_the_seed_fills_the_tables_a_restore_must_bring_back(restored):
    counts = restore_drill.check(restored).counts
    for table, n in {"students": 1, "learning_goals": 1, "messages": 2, "turn_events": 1,
                     "student_progress": 1}.items():
        assert counts[table] == n


def test_a_missing_table_fails(restored):
    with restored.begin() as conn:
        conn.execute(text("DROP TABLE message_feedback"))
    assert restore_drill.check(restored).failures == ["tables missing after the restore: message_feedback"]


def test_an_empty_table_that_must_have_rows_fails(restored):
    with restored.begin() as conn:
        conn.execute(text("DELETE FROM messages"))
    failures = restore_drill.check(restored, nonempty=("students", "messages")).failures
    assert failures == ["messages is empty after the restore"]


def test_the_revision_must_be_there_and_single(restored):
    with restored.begin() as conn:
        conn.execute(text("INSERT INTO alembic_version VALUES ('0000deadbeef')"))
    assert "alembic_version holds 2 rows, not 1" in restore_drill.check(restored).failures
    with restored.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    assert any("no alembic_version" in f for f in restore_drill.check(restored).failures)


def test_an_older_revision_fails_only_when_the_head_is_expected(restored):
    """Synthetic mode builds the source from the migrations, so it must be at
    the head; production may trail main while a deploy is pending."""
    with restored.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'bb5bc75ceadf'"))
    lenient = restore_drill.check(restored)
    assert lenient.failures == [] and "is at revision bb5bc75ceadf" in lenient.notes[0]
    assert "is at revision bb5bc75ceadf" in restore_drill.check(restored, expect_head=True).failures[0]


def test_the_output_is_counts_never_rows(restored, capsys, monkeypatch):
    monkeypatch.setattr(restore_drill, "_engine", lambda dsn: restored)
    assert restore_drill.main(["check", "--dsn", "sqlite://", "--nonempty", "students,messages"]) == 0
    out = capsys.readouterr().out
    assert "students: 1" in out and "messages: 2" in out
    assert "drill@example.test" not in out and "What does a restore prove" not in out


def test_the_check_exits_1_on_a_failure(restored, capsys, monkeypatch):
    monkeypatch.setattr(restore_drill, "_engine", lambda dsn: restored)
    with restored.begin() as conn:
        conn.execute(text("DELETE FROM student_progress"))
        conn.execute(text("DELETE FROM turn_events"))
        conn.execute(text("DELETE FROM messages"))
        conn.execute(text("DELETE FROM students"))
    assert restore_drill.main(["check", "--dsn", "sqlite://"]) == 1
    assert "::error::students is empty after the restore" in capsys.readouterr().out


def test_a_plain_postgres_url_gets_the_app_driver():
    engine = restore_drill._engine("postgresql://u:p@localhost:5433/postgres")
    assert engine.url.drivername == "postgresql+psycopg" and engine.url.port == 5433
