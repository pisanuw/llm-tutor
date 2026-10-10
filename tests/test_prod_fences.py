"""Command-line tools cannot write to production by accident (plan step 0.9).

get_engine() follows DATABASE_URL, which the maintainer's .env points at
production. Every CLI write path goes through tutor/db/target.py: it prints
the target, refuses a write to a non-SQLite database without --allow-prod,
and never runs create_all there (a table born that way has row-level
security off). These use a Postgres engine object, which never connects.
"""

from __future__ import annotations

import json
import pathlib
import re
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, inspect

from tutor.db.models import Base, JudgeSample, Student, TurnEvent, get_engine, get_session_factory
from tutor.db.target import describe_engine, prepare_schema
from tutor.utctime import utcnow

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROD = create_engine("postgresql+psycopg://user:secret@db.example.supabase.co:5432/postgres")


@pytest.fixture
def local_db(monkeypatch, tmp_path):
    """A throwaway SQLite file behind get_engine() for this test only."""
    import tutor.config as config

    monkeypatch.setenv("TUTOR_DB", str(tmp_path / "cli.db"))
    config.get_config.cache_clear()
    engine = get_engine()
    prepare_schema(engine, describe_engine(engine))
    return engine


def test_prepare_schema_creates_tables_on_sqlite_only(tmp_path):
    local = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    prepare_schema(local, describe_engine(local))
    assert "students" in inspect(local).get_table_names()
    with patch.object(Base.metadata, "create_all", side_effect=AssertionError("create_all on prod")):
        prepare_schema(PROD, describe_engine(PROD))   # a no-op, and never connects


def test_target_display_never_shows_the_password():
    assert "secret" not in describe_engine(PROD).display


def test_no_cli_calls_init_db_directly():
    """New tools must use prepare_schema; live_smoke pins its own SQLite file."""
    offenders = []
    for path in list((ROOT / "pipeline").rglob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        if path.name == "live_smoke.py":
            continue
        if re.search(r"\binit_db\(", path.read_text()):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_seed_goals_refuses_production_without_allow_prod(tmp_path):
    from pipeline import seed_goals

    goals = tmp_path / "goals.json"
    goals.write_text(json.dumps({"goals": [], "edges": []}))
    with patch.object(seed_goals, "get_engine", return_value=PROD):
        with pytest.raises(SystemExit) as exc:
            seed_goals.seed(goals)
        assert exc.value.code == 2
        seed_goals.seed(goals, dry_run=True)   # a dry run never touches the database


def test_mining_curation_refuses_production_without_allow_prod():
    from scripts import mine_misconceptions

    with patch("tutor.db.models.get_engine", return_value=PROD):
        with pytest.raises(SystemExit) as exc:
            mine_misconceptions.main(["--approve", "3"])
    assert exc.value.code == 2


def test_mining_curation_runs_on_local_sqlite(local_db, capsys):
    from scripts import mine_misconceptions

    assert mine_misconceptions.main(["--approve", "3"]) == 0
    assert "approve 3: not found" in capsys.readouterr().out


def _seed_risky_turn(engine):
    db = get_session_factory(engine)()
    db.add(Student(id=1, email="s@example.edu", name="S"))
    db.add(TurnEvent(student_id=1, kind="chat", ts=utcnow(),
                     verdicts_json=json.dumps({"verdict": "revise"})))
    db.commit()
    db.close()


def test_audit_dry_run_makes_no_model_calls_and_no_writes(local_db):
    from pipeline.audit_turns import audit_turns

    _seed_risky_turn(local_db)
    with patch("anthropic.Anthropic", side_effect=AssertionError("dry run built a client")):
        result = CliRunner().invoke(audit_turns, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "1 risky" in result.output
    db = get_session_factory(local_db)()
    assert db.query(JudgeSample).count() == 0
    db.close()


def test_audit_refuses_production_without_allow_prod():
    from pipeline.audit_turns import audit_turns

    with patch("tutor.db.models.get_engine", return_value=PROD):
        result = CliRunner().invoke(audit_turns, [])
    assert result.exit_code == 2
