"""Plan step 6.4 (findings E19, E3): the scheduled workflows.

Every scheduled workflow runs one at a time; every cron line says when it
runs in UTC and in both offsets of the class's zone (PDT and PST), and the
words match the schedule; the restore drill reads production only on its
schedule or a manual run.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
CRON_LINE = re.compile(r'^\s*- cron: "(\d+) (\d+|\*/\d+) [^"]*"(.*)$')
CLOCK = r"(\d{{2}}):(\d{{2}}) {zone}"   # str.format fills zone


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _scheduled() -> list[Path]:
    return [p for p in sorted(WORKFLOWS.glob("*.yml")) if "schedule" in (_load(p).get(True) or {})]


def _cron_comments(path: Path) -> list[tuple[int, int, str]]:
    """(minute, hour, comment) per cron line: its trailing comment, or the
    comment lines just above it."""
    lines = path.read_text().splitlines()
    found = []
    for i, line in enumerate(lines):
        m = CRON_LINE.match(line)
        if not m:
            continue
        comment = m.group(3)
        if not comment.strip():
            j = i
            while j > 0 and lines[j - 1].strip().startswith("#"):
                j -= 1
            comment = " ".join(ln.strip().lstrip("# ") for ln in lines[j:i])
        hour = m.group(2)
        found.append((int(m.group(1)), int(hour) if hour.isdigit() else hour, comment))
    return found


def test_there_are_scheduled_workflows():
    names = {p.name for p in _scheduled()}
    assert {"db-backup.yml", "db-restore-drill.yml", "daily-report.yml", "audit.yml"} <= names


@pytest.mark.parametrize("path", _scheduled(), ids=lambda p: p.name)
def test_a_scheduled_workflow_runs_one_at_a_time(path):
    concurrency = _load(path).get("concurrency")
    assert concurrency and "github.workflow" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


@pytest.mark.parametrize("path", _scheduled(), ids=lambda p: p.name)
def test_each_cron_comment_matches_its_schedule(path):
    crons = _cron_comments(path)
    assert crons, f"{path.name} has a schedule but no cron line"
    for minute, hour, comment in crons:
        if isinstance(hour, str):
            # "*/N": the comment says so and names the minute; no clock to check.
            every = int(hour[2:])
            assert f"every {every} hours" in comment, f"{path.name}: {comment!r}"
            assert f":{minute:02d} UTC" in comment, f"{path.name}: {comment!r}"
            continue
        times = {}
        for zone in ("UTC", "PDT", "PST"):
            m = re.search(CLOCK.format(zone=zone), comment)
            assert m, f"{path.name}: the cron comment does not give the {zone} time: {comment!r}"
            times[zone] = int(m.group(1)) * 60 + int(m.group(2))
        assert times["UTC"] == hour * 60 + minute, f"{path.name}: the UTC time is not the schedule's"
        assert times["PDT"] == (times["UTC"] - 7 * 60) % 1440, f"{path.name}: PDT is UTC-7"
        assert times["PST"] == (times["UTC"] - 8 * 60) % 1440, f"{path.name}: PST is UTC-8"


def test_the_drill_reads_production_only_on_its_schedule_or_by_hand():
    drill = _load(WORKFLOWS / "db-restore-drill.yml")
    job = drill["jobs"]["drill"]
    source = job["env"]["SOURCE"]
    assert "github.event_name == 'schedule' && 'production'" in source
    assert source.rstrip(" }").endswith("'synthetic')")
    assert {"push", "pull_request"} <= set(drill[True])
    readers = [s for s in job["steps"] if "secrets.DATABASE_URL" in str(s.get("env", {}))]
    assert len(readers) == 1 and readers[0]["if"] == "env.SOURCE == 'production'"
    assert "pg_restore" not in readers[0]["run"]                  # it dumps; nothing writes back
    restore = next(s for s in job["steps"] if s.get("name") == "Restore into the scratch database")
    assert '"$SCRATCH_URL"' in restore["run"] and "DATABASE_URL" not in restore["run"]
    assert "upload-artifact" not in (WORKFLOWS / "db-restore-drill.yml").read_text()


def test_the_drill_runs_when_the_migrations_change():
    paths = _load(WORKFLOWS / "db-restore-drill.yml")[True]["push"]["paths"]
    assert "migrations/**" in paths and "scripts/restore_drill.py" in paths
