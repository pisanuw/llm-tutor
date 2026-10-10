"""Plan step 2.7 (findings C4, E5): boot survives its externals.

C4: a failed slide fetch (``set -e`` plus ``curl | tar``) or a database that
was down at boot stopped the app, so Render restarted it in a loop in which
/healthz and its self-heal never ran. E5: a truncated ChromaDB download left a
partial store that every later boot treated as complete.
Done when: a failed slide fetch starts uvicorn; a truncated chroma tarball is
retried on the next boot.

The entrypoint runs for real under dash (the image's /bin/sh) with file://
seed URLs and a stub ``uvicorn`` on PATH.
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from tests.helpers import make_session

ROOT = Path(__file__).resolve().parent.parent
DASH = shutil.which("dash") or "/bin/sh"


# ---------------------------------------------------------------------------
# Seed fixtures
# ---------------------------------------------------------------------------

def _make_store(path: Path, slides: int = 1500) -> None:
    import chromadb

    rng = random.Random(7)
    client = chromadb.PersistentClient(path=str(path))
    col = client.get_or_create_collection("slides")
    col.add(ids=[f"s{i}" for i in range(slides)],
            embeddings=[[rng.random() for _ in range(32)] for _ in range(slides)],
            documents=[f"slide {i}" for i in range(slides)])
    del client


def _tgz(src: Path, out: Path) -> Path:
    with tarfile.open(out, "w:gz") as tar:
        for entry in sorted(src.iterdir()):
            tar.add(entry, arcname=entry.name)
    return out


@pytest.fixture(scope="module")
def seeds(tmp_path_factory):
    base = tmp_path_factory.mktemp("seeds")
    store = base / "store"
    _make_store(store)
    good = _tgz(store, base / "chroma.tgz")
    data = good.read_bytes()
    (base / "chroma-truncated.tgz").write_bytes(data[: len(data) // 2])
    half = len(data) // 2
    (base / "chroma.part0").write_bytes(data[:half])
    (base / "chroma.part1").write_bytes(data[half:])

    for name, page in (("deck-a", "lecture-01"), ("deck-b", "lecture-02")):
        deck = base / name / page
        deck.mkdir(parents=True)
        (deck / "index.html").write_text(f"<html>{page}</html>")
        _tgz(base / name, base / f"{name}.tgz")

    audio = base / "audio"
    audio.mkdir()
    (audio / "slide-01.mp3").write_bytes(b"ID3fake")
    _tgz(audio, base / "audio.tgz")

    (base / "exams.json").write_text('{"G1": {"q1": []}}')
    (base / "exams-bad.json").write_text('{"G1": ')

    stub = base / "bin"
    stub.mkdir()
    (stub / "uvicorn").write_text('#!/bin/sh\necho "UVICORN STARTED $*"\n')
    (stub / "uvicorn").chmod(0o755)
    return base


def _url(seeds: Path, *names: str) -> str:
    return " ".join(f"file://{seeds / n}" for n in names)


def _boot(tmp: Path, seeds: Path, **env: str) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    full.update({
        "PATH": f"{seeds / 'bin'}:{os.environ['PATH']}",
        "CHROMA_DIR": str(tmp / "disk"),
        "AUDIO_DIR": str(tmp / "disk" / "lecture-audio"),
        "OUTPUT_DIR": str(tmp / "output"),
        "MASTERY_PATH": str(tmp / "curriculum" / "mastery_exams.json"),
        "BOOT_WARNINGS": str(tmp / "warnings.txt"),
        "SEED_RETRIES": "0",
        "RUN_MIGRATIONS": "0",                     # the migration tests turn it on
        "TUTOR_DB": str(tmp / "app.db"),
    })
    for key in ("CHROMA_SEED_URL", "AUDIO_SEED_URL", "OUTPUT_SEED_URL", "MASTERY_EXAMS_URL"):
        full.pop(key, None)
    full.update(env)
    return subprocess.run([DASH, str(ROOT / "entrypoint.sh")], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=300)


def _started(run: subprocess.CompletedProcess) -> bool:
    return run.returncode == 0 and "UVICORN STARTED tutor.main:app" in run.stdout


def _warnings(tmp: Path) -> str:
    return (tmp / "warnings.txt").read_text()


def _store_ok(path: Path) -> bool:
    """The entrypoint's own check, in a fresh process: chromadb caches an
    opened store per path, so an in-process check would see stale files."""
    run = subprocess.run(["python3", "-m", "tutor.seed_check", "chroma", str(path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    return run.returncode == 0


# ---------------------------------------------------------------------------
# The entrypoint
# ---------------------------------------------------------------------------

def test_a_failed_slide_fetch_still_starts_uvicorn(tmp_path, seeds):
    run = _boot(tmp_path, seeds, OUTPUT_SEED_URL=_url(seeds, "missing.tgz"))
    assert _started(run), run.stderr
    assert "Slide seed failed" in _warnings(tmp_path)
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob("output.*"))                 # no scratch left behind


def test_a_truncated_chroma_seed_is_retried_on_the_next_boot(tmp_path, seeds):
    first = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "chroma-truncated.tgz"))
    assert _started(first), first.stderr
    assert "ChromaDB seed failed" in _warnings(tmp_path)
    disk = tmp_path / "disk"
    assert sorted(p.name for p in disk.iterdir()) == []         # no partial store, no marker

    second = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "chroma.tgz"))
    assert _started(second), second.stderr
    assert _warnings(tmp_path) == ""
    assert (disk / ".seed-ok").is_file() and _store_ok(disk)

    third = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "missing.tgz"))
    assert "already seeded" in third.stdout and _warnings(tmp_path) == ""


def test_split_chroma_parts_are_joined_in_order(tmp_path, seeds):
    run = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "chroma.part0", "chroma.part1"))
    assert _started(run), run.stderr
    assert _store_ok(tmp_path / "disk")


def test_a_readable_store_without_a_marker_is_adopted(tmp_path, seeds):
    shutil.copytree(seeds / "store", tmp_path / "disk")
    run = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "missing.tgz"))
    assert _started(run) and "marked as seeded" in run.stdout
    assert (tmp_path / "disk" / ".seed-ok").is_file() and _warnings(tmp_path) == ""


def test_an_unreadable_store_is_replaced_and_the_audio_kept(tmp_path, seeds):
    disk = tmp_path / "disk"
    shutil.copytree(seeds / "store", disk)
    for index in disk.glob("*/data_level0.bin"):
        with open(index, "r+b") as fh:
            fh.truncate(100)                                   # a short index file
    (disk / "lecture-audio").mkdir()
    (disk / "lecture-audio" / "keep.mp3").write_bytes(b"ID3")
    assert not _store_ok(disk)

    run = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "chroma.tgz"))
    assert _started(run), run.stderr
    assert _store_ok(disk) and (disk / ".seed-ok").is_file()
    assert (disk / "lecture-audio" / "keep.mp3").read_bytes() == b"ID3"


def test_a_forced_reseed_keeps_the_voice_cache(tmp_path, seeds):
    """TTS_CACHE_DIR points at <mount>/tts-cache in production (render.yaml) so
    the pre-rendered clips (python -m tutor.prewarm_tts) survive a deploy; a
    store refresh must not sweep them away with the old store."""
    disk = tmp_path / "disk"
    shutil.copytree(seeds / "store", disk)
    for index in disk.glob("*/data_level0.bin"):
        with open(index, "r+b") as fh:
            fh.truncate(100)
    (disk / "tts-cache").mkdir()
    (disk / "tts-cache" / "0abc.mp3").write_bytes(b"ID3")

    run = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "chroma.tgz"))
    assert _started(run), run.stderr
    assert _store_ok(disk) and (disk / ".seed-ok").is_file()
    assert (disk / "tts-cache" / "0abc.mp3").read_bytes() == b"ID3"


def test_a_failed_new_seed_keeps_the_old_store(tmp_path, seeds):
    disk = tmp_path / "disk"
    shutil.copytree(seeds / "store", disk)
    for index in disk.glob("*/data_level0.bin"):
        with open(index, "r+b") as fh:
            fh.truncate(100)
    before = sorted(p.name for p in disk.iterdir())
    run = _boot(tmp_path, seeds, CHROMA_SEED_URL=_url(seeds, "chroma-truncated.tgz"))
    assert _started(run)
    assert sorted(p.name for p in disk.iterdir()) == before     # nothing deleted, no marker


def test_audio_and_slides_seed_atomically(tmp_path, seeds):
    run = _boot(tmp_path, seeds,
                AUDIO_SEED_URL=_url(seeds, "audio.tgz"),
                OUTPUT_SEED_URL=_url(seeds, "deck-a.tgz", "deck-b.tgz"))
    assert _started(run), run.stderr
    audio, output = tmp_path / "disk" / "lecture-audio", tmp_path / "output"
    assert (audio / "slide-01.mp3").is_file() and (audio / ".seed-ok").is_file()
    assert (output / "lecture-01" / "index.html").is_file()
    assert (output / "lecture-02" / "index.html").is_file()

    failed = tmp_path / "second"
    run = _boot(failed, seeds, AUDIO_SEED_URL=_url(seeds, "missing.tgz"))
    assert _started(run) and "Lecture audio seed failed" in _warnings(failed)
    assert not (failed / "disk" / "lecture-audio").exists()


def test_a_non_empty_output_directory_is_never_replaced(tmp_path, seeds):
    (tmp_path / "output" / "lecture-09").mkdir(parents=True)
    (tmp_path / "output" / "lecture-09" / "content.md").write_text("mine")
    run = _boot(tmp_path, seeds, OUTPUT_SEED_URL=_url(seeds, "deck-a.tgz"))
    assert _started(run)
    assert (tmp_path / "output" / "lecture-09" / "content.md").read_text() == "mine"
    assert not (tmp_path / "output" / "lecture-01").exists()


def test_only_a_valid_exam_bank_is_installed(tmp_path, seeds):
    bank = tmp_path / "curriculum" / "mastery_exams.json"
    run = _boot(tmp_path, seeds, MASTERY_EXAMS_URL=_url(seeds, "exams-bad.json"))
    assert _started(run) and not bank.exists()
    assert "Mastery exam bank download failed" in _warnings(tmp_path)
    assert not list(bank.parent.glob("*.download"))
    run = _boot(tmp_path, seeds, MASTERY_EXAMS_URL=_url(seeds, "exams.json"))
    assert _started(run) and bank.read_text() == '{"G1": {"q1": []}}'


def test_boot_migrates_the_database_to_head(tmp_path, seeds):
    """Plan step 2.8 (decision 8 default): the entrypoint runs the migrations."""
    import sqlite3

    run = _boot(tmp_path, seeds, RUN_MIGRATIONS="1")
    assert _started(run), run.stderr
    assert "now at head" in run.stdout
    from alembic.script import ScriptDirectory

    from tutor import migrate

    head = ScriptDirectory.from_config(migrate.alembic_config()).get_current_head()
    con = sqlite3.connect(tmp_path / "app.db")
    assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
    con.close()


def test_a_failed_migration_still_starts_uvicorn(tmp_path, seeds):
    run = _boot(tmp_path, seeds, RUN_MIGRATIONS="1",
                TUTOR_DB=str(tmp_path / "no-such-dir" / "app.db"))
    assert _started(run)
    assert "[migrate] FAILED" in run.stderr
    assert "Database migration failed" in _warnings(tmp_path)


# ---------------------------------------------------------------------------
# The app: a database that is down at boot
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_startup(monkeypatch, tmp_path):
    import tutor.config as config
    from tutor import alerts, selfheal, startup
    from tutor.routers import status as status_router

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TUTOR_DB", str(tmp_path / "boot.db"))
    config.get_config.cache_clear()
    startup._reset_for_tests()
    status_router._reset_for_tests()
    selfheal._reset_for_tests()
    alerts._reset_for_tests()
    sent: list = []
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: sent.append((key, body)))
    yield sent
    startup._reset_for_tests()
    status_router._reset_for_tests()
    config.get_config.cache_clear()


def _wait_for(pred, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_the_app_starts_on_a_down_database_and_healthz_finishes_the_setup(fresh_startup, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    from tutor import startup
    from tutor.db.models import app_engine
    from tutor.main import app

    calls: list = []

    def down(engine):
        calls.append("down")
        raise OperationalError("SELECT 1", {}, Exception("the database system is starting up"))

    monkeypatch.setattr(startup, "prepare_database", down)
    with TestClient(app) as client:                            # the lifespan survives
        assert client.get("/auth/login").status_code == 200
        assert startup.database_ready(app_engine()) is False
        assert _wait_for(lambda: any(k == "boot-degraded" for k, _ in fresh_startup))
        body = dict(fresh_startup)["boot-degraded"]
        assert "Database setup failed at boot" in body

        monkeypatch.setattr(startup, "prepare_database", lambda engine: calls.append("up"))
        assert client.get("/healthz").json()["db"] == "ok"
        assert startup.database_ready(app_engine()) is True
        client.get("/healthz")
    assert calls == ["down", "up"]                            # set up once, then never again


def test_seed_warnings_from_the_entrypoint_are_emailed(fresh_startup, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tutor.main import app

    warnings = tmp_path / "warnings.txt"
    warnings.write_text("Slide seed failed; /slides/... returns 404 until the next boot.\n")
    monkeypatch.setenv("BOOT_WARNINGS", str(warnings))
    with TestClient(app):
        assert _wait_for(lambda: any(k == "boot-degraded" for k, _ in fresh_startup))
    assert "Slide seed failed" in dict(fresh_startup)["boot-degraded"]


def test_a_sandbox_without_containment_is_a_boot_warning(fresh_startup, monkeypatch, tmp_path):
    """Plan step 6.9 (finding B8): code execution on without the namespace
    sandbox, or refused beside a database, is said at boot."""
    from fastapi.testclient import TestClient

    from tutor.agent import run_code
    from tutor.main import app

    (tmp_path / "warnings.txt").write_text("")
    monkeypatch.setenv("BOOT_WARNINGS", str(tmp_path / "warnings.txt"))
    monkeypatch.setattr(run_code, "boot_warnings", lambda: ["ENABLE_CODE_EXEC is on without namespaces"])
    with TestClient(app):
        assert _wait_for(lambda: any(k == "boot-degraded" for k, _ in fresh_startup))
    assert "without namespaces" in dict(fresh_startup)["boot-degraded"]


def test_a_clean_boot_sends_nothing(fresh_startup, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tutor import startup
    from tutor.db.models import app_engine
    from tutor.main import app

    (tmp_path / "warnings.txt").write_text("")
    monkeypatch.setenv("BOOT_WARNINGS", str(tmp_path / "warnings.txt"))
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    with TestClient(app):
        time.sleep(0.2)
        assert startup.database_ready(app_engine()) is True
    assert [k for k, _ in fresh_startup if k == "boot-degraded"] == []


# ---------------------------------------------------------------------------
# /healthz reports the retrieval store
# ---------------------------------------------------------------------------

def test_slides_status(monkeypatch, tmp_path, seeds):
    import chromadb

    from tutor import health
    from tutor.health import slides_status

    def at(path):
        monkeypatch.setattr(health, "get_config", lambda: {"chroma_dir": path})
        return slides_status()

    assert at(tmp_path / "nothing") == "missing"
    assert not (tmp_path / "nothing").exists()                 # never creates a store
    other = tmp_path / "other"
    chromadb.PersistentClient(path=str(other)).get_or_create_collection("textbook")
    assert at(other) == "empty"
    assert at(seeds / "store") == "ok"


def test_healthz_says_degraded_when_retrieval_is_empty(fresh_startup, monkeypatch):
    from fastapi.testclient import TestClient

    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor import health

    monkeypatch.setattr(health, "slides_status", lambda: "empty")
    session = make_session()
    app.dependency_overrides[get_db] = lambda: session
    try:
        r = TestClient(app).get("/healthz")
    finally:
        app.dependency_overrides.clear()
        session.close()
    assert r.status_code == 200                                # the database is fine
    assert r.json()["status"] == "degraded" and r.json()["kb"] == "empty" and r.json()["db"] == "ok"
