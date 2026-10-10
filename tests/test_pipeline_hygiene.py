"""Plan step 5.8: pipeline hygiene (findings E11, E12, E17, E20, E23, E24,
E25, E26, B25).

E11: the collection list lived in four places that disagreed. E12, E23:
generate-all --resume and status disagreed on a finished lecture, the deck
was never rendered, out-of-range lectures were billed, and a content.md with
no slide stalled --resume. E17: default paths depended on the working
directory. E20: config.yaml keys that nothing read. E24: three slide
numberings. E25: local Whisper chunked by an API size cap. E26: small
operator-script items. B25: tracks listed goals ahead of same-lecture
prerequisites and carried no graph_version.
Done when: `python -m pipeline status` agrees with `generate-all --resume`
(test_status_agrees_with_generate_all_resume).
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib
import re
import sys
import types
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from pipeline import __main__ as cli_mod
from pipeline import lectures
from pipeline.order_tracks import lecture_prereqs, order_track, ordered, violations
from pipeline.render import html_renderer
from tutor import kb
from tests.helpers import make_session

ROOT = pathlib.Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipeline"


def _py_files(*tops: str):
    for top in tops:
        yield from sorted((ROOT / top).rglob("*.py"))


# ---------------------------------------------------------------------------
# E11: one collection list
# ---------------------------------------------------------------------------

def test_the_indexer_builds_exactly_the_collections_find_topic_searches():
    from pipeline.knowledge_base import indexer
    from tutor.agent import tools

    assert set(indexer.BUILDERS) == set(kb.COLLECTIONS)
    assert tools._SEARCH_COLLECTIONS == list(kb.COLLECTIONS)
    assert tools._COMMUNITY_COLLECTIONS <= set(kb.COLLECTIONS)


def test_build_index_runs_every_builder_in_order(tmp_path):
    from pipeline.knowledge_base import indexer

    seen = []
    fake = {name: (lambda n: lambda client, raw, verbose, log: seen.append(n) or 0)(name)
            for name in indexer.BUILDERS}
    with patch.object(indexer, "BUILDERS", fake), patch.object(indexer, "get_client", return_value=object()):
        counts = indexer.build_index(raw_dir=tmp_path / "raw", db_dir=tmp_path / "db", verbose=False)
    assert list(counts) == list(kb.COLLECTIONS) == seen


def test_every_collection_the_generators_query_exists():
    queried = set()
    for path in _py_files("pipeline", "scripts"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                arg = {"kb_query": 0, "_rag_context": 1}.get(node.func.id)
                if arg is not None and len(node.args) > arg and isinstance(node.args[arg], ast.Constant):
                    queried.add(node.args[arg].value)
    from pipeline.generate import teachme_generator

    queried |= set(teachme_generator._GROUNDING_COLLECTIONS)
    assert queried and queried <= set(kb.COLLECTIONS), queried - set(kb.COLLECTIONS)


def test_the_stale_lists_are_gone():
    from pipeline.knowledge_base import vectorstore

    assert not hasattr(vectorstore, "COLLECTIONS") and not hasattr(vectorstore, "query_all_collections")
    assert "collections" not in yaml.safe_load((ROOT / "config.yaml").read_text())["knowledge_base"]
    assert "queries 7" not in (ROOT / "CLAUDE.md").read_text()


# ---------------------------------------------------------------------------
# E20: every config.yaml key has a reader
# ---------------------------------------------------------------------------

# Keys a pipeline/config.py helper reads for its callers: the helper must
# have a caller outside config.py.
_READ_BY_HELPER = {"lectures_per_quarter": "lecture_numbers"}


def _leaf_keys(cfg: dict) -> set[str]:
    keys = set()
    for section, body in cfg.items():
        if section == "courses":
            for course in body.values():
                keys |= set(course)
        elif isinstance(body, dict):
            keys |= set(body)
    return keys


def _names_read_outside_config() -> tuple[set[str], set[str]]:
    """(string keys read by subscript or .get, names called) across pipeline/
    and scripts/, except the config module itself."""
    keys, calls = set(), set()
    for path in _py_files("pipeline", "scripts"):
        if path == PIPELINE / "config.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                keys.add(node.slice.value)
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "get" and node.args \
                        and isinstance(node.args[0], ast.Constant):
                    keys.add(node.args[0].value)
                calls.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
            elif isinstance(node, ast.ImportFrom):
                calls |= {a.name for a in node.names}
    return keys, calls


def test_every_config_key_has_a_reader():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    keys, calls = _names_read_outside_config()
    unread = sorted(k for k in _leaf_keys(cfg)
                    if k not in keys and not (k in _READ_BY_HELPER and _READ_BY_HELPER[k] in calls))
    assert unread == []


def test_every_config_accessor_has_a_caller():
    tree = ast.parse((PIPELINE / "config.py").read_text())
    accessors = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    _, calls = _names_read_outside_config()
    assert accessors and sorted(accessors - calls) == []


def test_the_scan_would_catch_a_dead_key():
    cfg = {"whisper": {"model": "medium", "chunk_duration_ms": 600000}}
    keys, _ = _names_read_outside_config()
    assert "chunk_duration_ms" in _leaf_keys(cfg) and "chunk_duration_ms" not in keys


def test_the_dead_keys_are_gone():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    for dead in ("embeddings_model", "chunk_size_tokens", "chunk_overlap_tokens", "prompts_dir",
                 "chunk_duration_ms", "supported_formats", "lecture_duration_minutes", "style_guide",
                 "examples_library", "standards"):
        assert dead not in _leaf_keys(cfg), dead


# ---------------------------------------------------------------------------
# E17: default paths do not depend on the working directory
# ---------------------------------------------------------------------------

def test_no_cli_default_is_a_relative_path():
    text = (PIPELINE / "__main__.py").read_text()
    assert not re.search(r'default="(source-materials|knowledge-base|output|curriculum)', text)
    assert not re.search(r'Path\(f?"(source-materials|knowledge-base|output|curriculum)', text)


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """Run the CLI from a directory that is not the repository."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_transcribe_reads_the_configured_folder_and_model_from_anywhere(elsewhere):
    from pipeline.config import get_recordings_config, get_whisper_config

    seen = {}
    fake = types.ModuleType("pipeline.ingest.transcriber")
    fake.batch_transcribe = lambda **kw: seen.update(kw) or []
    with patch.dict(sys.modules, {"pipeline.ingest.transcriber": fake}):
        result = CliRunner().invoke(cli_mod.cli, ["transcribe", "--quiet"])
    assert result.exit_code == 0, result.output
    assert seen["recordings_dir"] == get_recordings_config()["dir"]
    assert seen["recordings_dir"].is_absolute() and ROOT in seen["recordings_dir"].parents
    assert seen["model_size"] == get_whisper_config()["model"] == "medium"
    assert seen["language"] == "en"


def test_index_transcripts_reads_the_same_folders_as_index_from_anywhere(elsewhere, monkeypatch):
    import pipeline.config as config
    from pipeline.knowledge_base import indexer

    def run() -> list:
        seen = []
        with patch("pipeline.knowledge_base.vectorstore.get_client", return_value=object()), \
             patch.object(indexer, "_index_transcripts_from_dir",
                          side_effect=lambda client, coll, d, verbose=False, **kw: seen.append(d) or 0):
            result = CliRunner().invoke(cli_mod.cli, ["index-transcripts", "--quiet"])
        assert result.exit_code == 0, result.output
        return seen

    seen = run()
    expected = indexer.transcript_folders(config.get_kb_config()["raw_dir"])
    assert [d for d in expected if d.exists()] == seen
    assert all(d.is_absolute() for d in expected)

    # The checkout may hold no transcripts; a knowledge base that does: the
    # pipeline's own folder and a course corpus, never a folder named for no course.
    kb_root = elsewhere / "kb"
    for folder in ("raw/transcripts", "transcripts/AST102-2026", "transcripts/notes-2026"):
        (kb_root / folder).mkdir(parents=True)
    kb_cfg = {**config.get_kb_config(), "raw_dir": kb_root / "raw", "db_dir": kb_root / "chromadb"}
    monkeypatch.setattr(config, "get_kb_config", lambda: kb_cfg)
    assert run() == indexer.transcript_folders(kb_root / "raw") == [
        kb_root / "raw" / "transcripts", kb_root / "transcripts" / "AST102-2026"]


# ---------------------------------------------------------------------------
# E12, E23: status and generate-all --resume share one test
# ---------------------------------------------------------------------------

TWO_SLIDES = ("## Slide 1: The Night Sky\ntype: title\ntitle: The Night Sky\n\n---\n\n"
              "## Slide 2: Rising and setting\ntype: content\ntitle: Rising and setting\n"
              "bullets:\n  - the Earth turns\n")


@pytest.fixture
def course_dir(tmp_path, monkeypatch):
    """A course whose lectures sit in six states, and generators that record
    their calls instead of calling a model. The fixture course has four
    lectures; this one is given six, one per state."""
    import pipeline.config as config
    from pipeline.generate import script_generator, slide_generator

    out = tmp_path / "output" / "AST101"
    real = config.get_course_config

    def fake_config(course):
        cfg = real(course)
        cfg["output_dir"] = out
        cfg["lectures"] = 6
        return cfg

    monkeypatch.setattr(config, "get_course_config", fake_config)
    calls: list[tuple[str, int]] = []

    def gen_content(lecture_num, output_dir, **_):
        calls.append(("content", lecture_num))
        d = output_dir / f"lecture-{lecture_num:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "content.md").write_text(TWO_SLIDES)

    def gen_script(lecture_num, output_dir, **_):
        calls.append(("script", lecture_num))
        (output_dir / f"lecture-{lecture_num:02d}" / "script.md").write_text("<!-- ADVANCE SLIDE 1 -->\n")

    monkeypatch.setattr(slide_generator, "generate_slide_content", gen_content)
    monkeypatch.setattr(script_generator, "generate_script", gen_script)

    def lecture(n, content=None, html=False, script=False):
        d = out / f"lecture-{n:02d}"
        d.mkdir(parents=True, exist_ok=True)
        if content is not None:
            (d / "content.md").write_text(content)
        if html:
            (d / "index.html").write_text("<html></html>")
        if script:
            (d / "script.md").write_text("script")

    lecture(1, TWO_SLIDES, html=True, script=True)     # complete
    lecture(2, TWO_SLIDES, html=True)                  # complete, no script
    lecture(3, TWO_SLIDES)                             # content only
    lecture(4, "## no slides here\n", html=True)       # content that parses to nothing
    lecture(5, html=True)                              # a deck without its source
    # lecture 6: nothing at all
    return out, calls


def _status(out: pathlib.Path, lectures_: range) -> dict[int, str]:
    result = CliRunner().invoke(cli_mod.cli, ["status", "--course", "AST101"])
    assert result.exit_code == 0, result.output
    found = dict(re.findall(r"L(\d+): (complete|missing|partial \(\S+\))", result.output))
    return {n: found[f"{n:02d}"] for n in lectures_}


def test_status_agrees_with_generate_all_resume(course_dir):
    out, calls = course_dir
    before = _status(out, range(1, 7))
    assert before == {1: "complete", 2: "complete", 3: "partial (C--)", 4: "partial (-H-)",
                      5: "partial (-H-)", 6: "missing"}

    result = CliRunner().invoke(cli_mod.cli, ["generate-all", "--course", "AST101", "--lectures", "1-6", "--resume"])
    assert result.exit_code == 0, result.output
    did = {n: [step for step, m in calls if m == n] for n in range(1, 7)}
    # A lecture status calls complete keeps its slides and deck.
    assert all("content" not in did[n] for n, s in before.items() if s == "complete")
    assert did == {1: [], 2: ["script"], 3: ["script"], 4: ["content", "script"],
                   5: ["content", "script"], 6: ["content", "script"]}
    # The deck was rendered wherever it was missing or stale.
    assert "Rising and setting" in (out / "lecture-03" / "index.html").read_text()
    assert "Rising and setting" in (out / "lecture-04" / "index.html").read_text()   # regenerated, re-rendered
    assert _status(out, range(1, 7)) == {n: "complete" for n in range(1, 7)}
    assert not list(out.rglob("slides.pptx"))                              # pptx only with --pptx


def test_a_rerun_with_resume_does_nothing(course_dir):
    out, calls = course_dir
    runner = CliRunner()
    runner.invoke(cli_mod.cli, ["generate-all", "--course", "AST101", "--lectures", "1-6", "--resume"])
    calls.clear()
    result = runner.invoke(cli_mod.cli, ["generate-all", "--course", "AST101", "--lectures", "1-6", "--resume"])
    assert result.exit_code == 0 and calls == []
    assert result.output.count("already complete") == 6


@pytest.mark.parametrize("spec", ["0-6", "1-7", "7", "5-3", "one", "3,99"])
def test_generate_all_refuses_lectures_that_do_not_exist(course_dir, spec):
    _, calls = course_dir
    result = CliRunner().invoke(cli_mod.cli, ["generate-all", "--course", "AST101", "--lectures", spec])
    assert result.exit_code == 2, result.output
    assert "--lectures" in result.output and calls == []


def test_single_lecture_commands_check_the_number(course_dir):
    for cmd in (["slides", "--lecture", "0"], ["script", "--lecture", "7"]):
        result = CliRunner().invoke(cli_mod.cli, cmd + ["--course", "AST101"])
        assert result.exit_code == 2 and "AST 101 has lectures 1 to 6" in result.output, cmd


def test_steps_to_run():
    st = lectures.LectureState
    assert lectures.steps_to_run(st(2, True, True), resume=False) == ["content", "html", "script"]
    assert lectures.steps_to_run(st(2, True, True), resume=False, pptx=True) == ["content", "html", "pptx", "script"]
    assert lectures.steps_to_run(st(2, True, True), resume=True) == []
    assert lectures.steps_to_run(st(2, False, True), resume=True) == ["html"]
    assert lectures.steps_to_run(st(0, True, True), resume=True) == ["content", "html", "script"]
    assert lectures.steps_to_run(st(2, True, True, has_pptx=False), resume=True, pptx=True) == ["pptx"]


# ---------------------------------------------------------------------------
# E24: slides are numbered once, by position
# ---------------------------------------------------------------------------

SKIPPED = ("## Slide 1: One\ntitle: One\n\n---\n\n## Slide 2: Two\ntitle: Two\n\n---\n\n"
           "## Slide 4: Three\ntitle: Three\nbody: said inline\n")


def test_slides_are_numbered_by_position_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="pipeline.render"):
        slides = html_renderer.parse_slides(SKIPPED)
    assert [s.number for s in slides] == [1, 2, 3]
    assert "slide 3 is headed 4" in caplog.text
    assert slides[2].body == "said inline"                    # E26: inline body kept


def test_the_script_manifest_counts_like_the_deck(tmp_path):
    from pipeline.generate.script_generator import _parse_deck_slides

    content = tmp_path / "content.md"
    content.write_text(SKIPPED)
    assert _parse_deck_slides(content) == [(1, "One"), (2, "Two"), (3, "Three")]


def test_the_deck_plays_the_clip_for_its_position(tmp_path):
    lec = tmp_path / "AST101" / "lecture-01"
    (lec / "audio").mkdir(parents=True)
    (lec / "audio" / "slide-01.mp3").write_bytes(b"x")
    (lec / "content.md").write_text(SKIPPED)
    assert html_renderer.render_lecture(lec / "content.md", lec / "index.html") == 3
    html = (lec / "index.html").read_text()
    assert "audio/slide-03.mp3" in html and "audio/slide-04.mp3" not in html


# ---------------------------------------------------------------------------
# E25: local Whisper, no chunking
# ---------------------------------------------------------------------------

def test_transcription_is_one_pass_with_whisper_timestamps(tmp_path):
    from pipeline.ingest import transcriber

    assert not hasattr(transcriber, "_chunk_audio") and not hasattr(transcriber, "_needs_chunking")
    audio = tmp_path / "lecture.mp4"
    audio.write_bytes(b"\0" * (30 * 1024 * 1024))            # over the old 24 MB cut
    calls = []

    class Model:
        def transcribe(self, path, language):
            calls.append(path)
            return {"language": "en", "segments": [{"start": 0.0, "end": 4.0, "text": "a"},
                                                   {"start": 1300.0, "end": 1304.5, "text": "b"}]}

    fake = types.ModuleType("whisper")
    fake.load_model = lambda size: Model()
    with patch.dict(sys.modules, {"whisper": fake}):
        t = transcriber.transcribe_file(audio, model_size="tiny")
    assert calls == [str(audio)]
    assert [(s.start, s.end) for s in t.segments] == [(0.0, 4.0), (1300.0, 1304.5)]
    assert "pydub==" not in (ROOT / "requirements.txt").read_text()


# ---------------------------------------------------------------------------
# E26: operator-script items
# ---------------------------------------------------------------------------

def test_concat_list_quotes_an_apostrophe():
    from pipeline.render.audio_renderer import _concat_quote

    assert _concat_quote(pathlib.Path("/audio/Morgan's lecture/a.mp3")) == "'/audio/Morgan'\\''s lecture/a.mp3'"


def test_the_audio_model_is_named_once():
    assert "eleven_" not in (PIPELINE / "__main__.py").read_text()


def test_upload_needs_an_explicit_release_tag():
    result = CliRunner().invoke(cli_mod.cli, ["upload-mastery-exams"])
    assert result.exit_code == 2 and "--tag" in result.output
    assert "deploy-assets-2026" not in (PIPELINE / "__main__.py").read_text()


def test_mark_slides_uses_the_course_lectures(tmp_path):
    from pipeline.generate.slide_marker import mark_course

    for n in (1, 21):
        d = tmp_path / f"lecture-{n:02d}"
        d.mkdir()
        (d / "script.md").write_text("### Slide 1: Intro\nhello\n")
    assert set(mark_course(tmp_path, lectures=list(range(1, 22)))) == {1, 21}


def test_canvas_reads_its_settings_from_config():
    from pipeline.config import get_canvas_config
    from pipeline.ingest import canvas_reader

    assert canvas_reader.CANVAS_BASE_URL == "https://canvas.example.edu"
    assert canvas_reader.TOKEN_FILE == get_canvas_config()["token_file"]
    assert "~" not in str(canvas_reader.TOKEN_FILE)


# ---------------------------------------------------------------------------
# B25: tracks follow their same-lecture prerequisites
# ---------------------------------------------------------------------------

GOALS = json.loads((ROOT / "curriculum" / "learning_goals.json").read_text())
TRACKS = json.loads((ROOT / "curriculum" / "tracks.json").read_text())


def test_the_committed_tracks_are_ordered_and_versioned():
    prereqs = lecture_prereqs(GOALS)
    for t in TRACKS["tracks"]:
        assert violations(t["goal_ids"], prereqs) == [], t["id"]
    assert TRACKS["graph_version"] == GOALS["graph_version"]
    assert ordered(GOALS, TRACKS) == TRACKS                      # the command would change nothing


def test_order_track_moves_goals_only_within_their_lecture():
    lecture_of = {"a1": 1, "a2": 1, "a3": 1, "b1": 2, "b2": 2}
    prereqs = {"a1": ["a3"], "b1": ["b2"], "a2": ["b1"]}         # a2's prerequisite is in lecture 2
    got = order_track(["b1", "a1", "a2", "b2", "a3"], prereqs, lecture_of)
    assert [lecture_of[g] for g in got] == [2, 1, 1, 2, 1]      # the slots stay
    assert got == ["b2", "a2", "a3", "b1", "a1"]
    assert order_track(got, prereqs, lecture_of) == got          # idempotent


def test_order_track_refuses_a_cycle():
    with pytest.raises(ValueError, match="cycle"):
        order_track(["x", "y"], {"x": ["y"], "y": ["x"]}, {"x": 1, "y": 1})


def test_order_tracks_check_mode(tmp_path, monkeypatch):
    from pipeline import order_tracks

    tracks = tmp_path / "tracks.json"
    tracks.write_text(json.dumps({"tracks": [{"id": "t", "course": "AST101", "title": "t",
                                              "goal_ids": ["AST101-L01-celestial-sphere"]}]}))
    monkeypatch.setattr(order_tracks, "TRACKS_PATH", tracks)
    assert CliRunner().invoke(cli_mod.cli, ["order-tracks", "--check"]).exit_code == 1   # no version
    assert CliRunner().invoke(cli_mod.cli, ["order-tracks"]).exit_code == 0
    assert json.loads(tracks.read_text())["graph_version"] == GOALS["graph_version"]
    assert CliRunner().invoke(cli_mod.cli, ["order-tracks", "--check"]).exit_code == 0


def test_startup_updates_the_graph_version_of_existing_tracks():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from tutor import startup
    from tutor.db.models import Base, Track

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        db.add(Track(id="AST101-lecture-order", course="AST101", title="old", description="",
                     goal_ids_json="[]", graph_version=0))
        db.commit()
    startup.seed_tracks_if_empty(engine)
    with sessionmaker(bind=engine)() as db:
        row = db.get(Track, "AST101-lecture-order")
        assert row.graph_version == GOALS["graph_version"] and row.goal_ids()[0].startswith("AST101-L01")


def test_a_malformed_track_reads_as_empty():

    from tutor.agent.navigator import _track_goal_order
    from tutor.db.models import Track

    with make_session() as db:
        db.add(Track(id="t", course="AST101", title="t", description="", goal_ids_json="{not json"))
        db.commit()
        assert _track_goal_order("t", db) == []
