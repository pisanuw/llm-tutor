"""The lecture catalog finds the narration where production keeps it.

The decks come from the slide seed under output/; the per-slide clips live
under LECTURE_AUDIO_DIR (the persistent disk in production, output/ locally).
The catalog and the viewer used to look for the clips beside the deck, so in
production every lecture read "slides only" and the viewer never showed its
narration hint, though the deck's own Play button worked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.browser_support import app_client, student_db
from tutor import config
from tutor.routers import lectures as lectures_mod


@pytest.fixture
def db():
    session = student_db()
    yield session
    session.close()


@pytest.fixture
def client(db):
    with app_client(db) as c:
        yield c


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """A deck directory and a separate clip directory, as in production."""
    out, audio = tmp_path / "output", tmp_path / "disk" / "lecture-audio"
    cfg = dict(config.get_config())
    cfg["output_dir"], cfg["lecture_audio_dir"] = out, audio
    monkeypatch.setattr(lectures_mod, "get_config", lambda: cfg)
    return out, audio


def _deck(out: Path, course: str, num: int) -> Path:
    lec = out / course / f"lecture-{num:02d}"
    lec.mkdir(parents=True)
    (lec / "index.html").write_text("<html></html>")
    (lec / "content.md").write_text(f"## Slide 1: Lecture {num} title\n")
    return lec


def _clips(root: Path, course: str, num: int, timing: dict | None = None) -> Path:
    lec = root / course / f"lecture-{num:02d}"
    (lec / "audio").mkdir(parents=True)
    (lec / "audio" / "slide-01.mp3").write_bytes(b"ID3")
    if timing is not None:
        (lec / "timing.json").write_text(json.dumps(timing))
    return lec


def test_the_catalog_finds_the_clips_under_lecture_audio_dir(client, dirs):
    out, audio = dirs
    _deck(out, "AST101", 1)
    _deck(out, "AST101", 2)
    _clips(audio, "AST101", 1, timing={"1": 90.0, "2": 30.0})

    page = client.get("/lectures").text
    assert "🔊 narrated · 2 min" in page
    assert page.count("slides only") == 1          # lecture 2


def test_the_minutes_can_come_from_the_slide_seed(client, dirs):
    out, audio = dirs
    lec = _deck(out, "AST101", 1)
    _clips(audio, "AST101", 1)                        # no timing beside the clips
    (lec / "timing.json").write_text(json.dumps({"1": 600.0, "2": 120.0}))

    assert "🔊 narrated · 12 min" in client.get("/lectures").text


def test_a_bad_timing_file_still_shows_the_badge(client, dirs):
    out, audio = dirs
    _deck(out, "AST101", 1)
    lec = _clips(audio, "AST101", 1)
    (lec / "timing.json").write_text('{"1": "soon"}')

    page = client.get("/lectures").text
    assert "🔊 narrated" in page and "min" not in page.split("🔊 narrated", 1)[1][:12]


def test_the_viewer_shows_the_narration_hint_only_with_clips(client, dirs):
    out, audio = dirs
    _deck(out, "AST101", 1)
    _deck(out, "AST101", 2)
    _clips(audio, "AST101", 1)

    assert "Play narration" in client.get("/lectures/AST101/1").text
    assert "Play narration" not in client.get("/lectures/AST101/2").text


def test_locally_the_clips_sit_beside_the_deck(client, dirs, monkeypatch):
    """The default: lecture_audio_dir is output/, where render-audio writes."""
    out, _ = dirs
    cfg = dict(lectures_mod.get_config())
    cfg["lecture_audio_dir"] = out
    monkeypatch.setattr(lectures_mod, "get_config", lambda: cfg)
    _deck(out, "AST102", 3)
    _clips(out, "AST102", 3, timing={"1": 60.0})

    assert "🔊 narrated · 1 min" in client.get("/lectures").text


def test_lecture_audio_dir_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("LECTURE_AUDIO_DIR", "/app/knowledge-base/chromadb/lecture-audio")
    config.get_config.cache_clear()
    assert config.get_config()["lecture_audio_dir"] == Path("/app/knowledge-base/chromadb/lecture-audio")

    monkeypatch.delenv("LECTURE_AUDIO_DIR")
    config.get_config.cache_clear()
    # Unset, the clips live beside the decks (output/, or TUTOR_OUTPUT_DIR).
    assert config.get_config()["lecture_audio_dir"] == config.get_config()["output_dir"]
