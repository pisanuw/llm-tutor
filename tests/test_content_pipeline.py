"""Plan step 6.2 (findings F19, E22): the content pipeline's student-facing output.

F19: 32 of 39 pipeline modules had no test, the Reveal renderer that makes
what students see and the indexer that builds the retrieval store among
them. Here those two are pinned by golden files, and every module whose
output a student sees or hears has a network-free test, most of them a
contract test: the generator's output read back by the app's own reader.
STUDENT_FACING lists them, and test_every_pipeline_module_is_placed fails
when a module is added to pipeline/ without being placed in it or in
NOT_STUDENT_FACING.

After an intended change to a golden output, regenerate it with
UPDATE_GOLDEN=1 and review the diff.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import shutil
import types
from unittest.mock import patch

import pytest
from click.testing import CliRunner

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
GOLDEN = TESTS / "golden"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"
HERE = "test_content_pipeline.py"


def _golden(path: pathlib.Path, actual: str) -> None:
    if UPDATE:
        path.write_text(actual)
    assert path.exists(), f"{path} is missing; create it with UPDATE_GOLDEN=1"
    assert actual == path.read_text(), f"{path.name} changed; review it, then rerun with UPDATE_GOLDEN=1"


# ---------------------------------------------------------------------------
# Golden files: the deck and the index
# ---------------------------------------------------------------------------

def test_the_deck_matches_its_golden_file(tmp_path):
    """Every slide type the renderer knows, as the student's browser gets it."""
    from pipeline.render.html_renderer import render_lecture

    lec = tmp_path / "AST101" / "lecture-04"
    lec.mkdir(parents=True)
    shutil.copy(GOLDEN / "deck" / "content.md", lec / "content.md")
    assert render_lecture(lec / "content.md", lec / "index.html", "AST 101 Lecture 04") == 6
    _golden(GOLDEN / "deck" / "index.html", (lec / "index.html").read_text())


def test_the_index_matches_its_golden_file(tmp_path):
    """What every collection would hold for a small raw tree: the text, the
    metadata find_topic cites, the ids; both transcript formats included."""
    from pipeline.knowledge_base import indexer
    from tutor.kb import COLLECTIONS

    captured: dict[str, list[dict]] = {}

    class Client:
        def get_or_create_collection(self, name, metadata, embedding_function):
            assert metadata == {"hnsw:space": "cosine"}
            # The app's query embedder (plan step 6.7, finding E8).
            assert type(embedding_function).__name__ == "DefaultEmbeddingFunction"
            return types.SimpleNamespace(name=name)

    def upsert(collection, documents, metadatas, ids):
        captured.setdefault(collection.name, []).extend(
            {"id": i, "text": d, "meta": m} for d, m, i in zip(documents, metadatas, ids))

    with patch.object(indexer, "get_client", return_value=Client()), \
         patch.object(indexer, "upsert_documents", upsert):
        counts = indexer.build_index(raw_dir=GOLDEN / "kb" / "knowledge-base" / "raw",
                                     db_dir=tmp_path / "db", verbose=False)
    assert list(counts) == list(COLLECTIONS) and all(counts.values())
    actual = json.dumps({"counts": counts,
                         "documents": {k: sorted(v, key=lambda r: r["id"]) for k, v in sorted(captured.items())}},
                        indent=2, sort_keys=True) + "\n"
    _golden(GOLDEN / "kb" / "indexed.json", actual)


# ---------------------------------------------------------------------------
# What students hear: narration from the script
# ---------------------------------------------------------------------------

SCRIPT = """# Lecture 4: Light and Telescopes

Welcome back, everyone.

<!-- ADVANCE SLIDE 1 -->
### Slide 1: Light
**What to say:**
> "Today we follow light from a star."

**Talking Points:**
- the inverse-square law

<!-- ADVANCE SLIDE 2 -->
### Slide 2: Brightness
**What to say:**
"Twice as far away, the same star looks a quarter as bright."
*[point at the spheres]*

**Transition:** "Now let's collect that light with a telescope."

<!-- ADVANCE SLIDE 3 -->
### Slide 3: Picture
*[silent: let them look]*
"""


def test_narration_is_the_spoken_lines_per_slide():
    from pipeline.render.audio_renderer import parse_narration

    assert parse_narration(SCRIPT) == {
        1: "Today we follow light from a star.",
        2: "Twice as far away, the same star looks a quarter as bright.\n\nNow let's collect that light with a telescope.",
    }                                              # talking points and stage directions stay silent


# ---------------------------------------------------------------------------
# What students read: generated content, read back by the app
# ---------------------------------------------------------------------------

def _fake_anthropic(text: str, seen: dict):
    class Stream:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.text_stream = [text[: len(text) // 2], text[len(text) // 2:]]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Client:
        def __init__(self, api_key=None):
            self.messages = types.SimpleNamespace(stream=lambda **kw: Stream(**kw))

    return Client


PLAN = """# AST 101 plan

## Lecture 01: The Sky Above Us
**Learning Objectives:** By the end of this lecture, students will be able to:
1. Locate a star on the celestial sphere
2. Measure an angle with an outstretched hand
**Prerequisite Lectures:** none

## Lecture 02: Phases of the Moon
**Learning Objectives:** By the end of this lecture, students will be able to:
1. Explain the phases of the Moon from its orbit
**Prerequisite Lectures:** 1

## Lecture 03: Midterm Review
**Prerequisite Lectures:** 1-2
"""


def test_the_lecture_plan_is_what_the_tutor_outlines(tmp_path):
    from pipeline.generate import curriculum_planner
    from tutor.agent import tools

    seen: dict = {}
    (tmp_path / "topics.md").write_text("the celestial sphere, phases of the Moon")
    (tmp_path / "catalog.txt").write_text("AST 101 catalog text")
    cc = {"course_number": "AST101", "course_name": "AST 101: The Night Sky", "lectures": 3,
          "source_topic_list": str(tmp_path / "topics.md"), "course_description": str(tmp_path / "catalog.txt")}
    with patch.object(curriculum_planner.anthropic, "Anthropic", _fake_anthropic(PLAN, seen)):
        out = curriculum_planner.generate_curriculum_plan(cc, tmp_path / "curriculum", tmp_path / "profile",
                                                          tmp_path / "raw")
    assert out == tmp_path / "curriculum" / "AST101-lecture-plan.md" and out.read_text() == PLAN
    prompt = seen["messages"][0]["content"]
    assert "AST 101 catalog text" in prompt and "Exactly 3 lectures, numbered 01 to 03" in prompt
    with patch.object(tools, "lecture_plan_path", lambda course: tmp_path / "curriculum" / f"{course}-lecture-plan.md"):
        outline = tools.get_lecture_outline("AST101", 2)
    assert outline["outline"].startswith("## Lecture 02: Phases of the Moon") and "its orbit" in outline["outline"]


def test_a_truncated_plan_is_extended_in_the_same_format(tmp_path, monkeypatch):
    from pipeline.generate import script_generator as sg
    from pipeline.utils import extend_plan as ext
    from tutor.agent import tools

    (tmp_path / "curriculum").mkdir()
    plan = tmp_path / "curriculum" / "AST101-lecture-plan.md"
    plan.write_text("\n".join(f"## Lecture {n:02d}: Topic {n}\n**Topics:**\n- t{n}\n\n---\n" for n in range(1, 3)))
    added = "\n".join(f"## Lecture {n:02d}: Topic {n}\n**Topics:**\n- t{n}\n\n---\n" for n in (3, 4))
    # The fixture's AST101 has 4 lectures (courses.yaml); the plan stops at 2.
    monkeypatch.setattr(ext, "lecture_plan_path", lambda course: tmp_path / "curriculum" / f"{course}-lecture-plan.md")
    monkeypatch.setattr(tools, "lecture_plan_path", lambda course: tmp_path / "curriculum" / f"{course}-lecture-plan.md")
    runs = []

    def generate(system, user, model="sonnet", timeout=900):       # the launch: test_cli_hardening.py
        runs.append(user)
        return added

    monkeypatch.setattr(sg, "_generate_via_cli", generate)
    ext.extend_plan("AST101")
    assert "missing lecture(s) 3-4" in runs[0]
    assert tools.get_lecture_outline("AST101", 4)["outline"].startswith("## Lecture 04: Topic 4")
    assert tools.get_lecture_outline("AST101", 2)["outline"].startswith("## Lecture 02: Topic 2")
    ext.extend_plan("AST101")                                            # already 4: no call
    assert len(runs) == 1


def test_generated_slides_are_what_the_deck_renders(tmp_path):
    from pipeline.generate import slide_generator
    from pipeline.render.html_renderer import parse_slides

    content = (GOLDEN / "deck" / "content.md").read_text()
    seen: dict = {}
    (tmp_path / "curriculum").mkdir()
    (tmp_path / "curriculum" / "AST101-lecture-plan.md").write_text(PLAN)
    cc = {"course_number": "AST101", "course_name": "AST 101"}
    with patch.object(slide_generator.anthropic, "Anthropic", _fake_anthropic(content, seen)), \
         patch("pipeline.knowledge_base.vectorstore.query", return_value=[{"text": "retrieved excerpt"}]):
        out = slide_generator.generate_slide_content(2, cc, tmp_path / "curriculum", tmp_path / "profile",
                                                     tmp_path / "output")
    assert out == tmp_path / "output" / "lecture-02" / "content.md"
    assert len(parse_slides(out.read_text())) == 6
    prompt = seen["messages"][0]["content"]
    assert "Explain the phases of the Moon" in prompt and "retrieved excerpt" in prompt


def _deck(tmp_path) -> pathlib.Path:
    out = tmp_path / "output"
    (out / "lecture-04").mkdir(parents=True)
    shutil.copy(GOLDEN / "deck" / "content.md", out / "lecture-04" / "content.md")
    (tmp_path / "curriculum").mkdir(exist_ok=True)
    (tmp_path / "curriculum" / "AST101-lecture-plan.md").write_text(PLAN)
    return out


def _script(n: int) -> str:
    return "\n".join(f"<!-- ADVANCE SLIDE {i} -->\n### Slide {i}: s\n**What to say:**\n> \"Line {i}.\"\n"
                     for i in range(1, n + 1))


def test_a_script_is_written_only_when_it_follows_the_deck(tmp_path, monkeypatch):
    from pipeline.generate import script_generator as sg

    out = _deck(tmp_path)
    monkeypatch.setattr(sg, "kb_query", lambda *a, **k: [])
    answers = iter([_script(5), _script(6)])                        # one short, then aligned
    monkeypatch.setattr(sg, "_generate_via_cli", lambda system, user, model="sonnet": next(answers))
    path = sg.generate_script(4, {"course_number": "AST101"}, tmp_path / "curriculum", tmp_path / "profile", out)
    assert path.read_text() == _script(6)

    from pipeline.render.audio_renderer import parse_narration

    assert sorted(parse_narration(path.read_text())) == list(range(1, 7))   # a clip per slide


def test_a_misaligned_script_is_never_written(tmp_path, monkeypatch):
    from pipeline.generate import script_generator as sg

    out = _deck(tmp_path)
    monkeypatch.setattr(sg, "kb_query", lambda *a, **k: [])
    monkeypatch.setattr(sg, "_generate_via_cli", lambda system, user, model="sonnet": _script(4))
    with pytest.raises(RuntimeError, match="not slide-aligned"):
        sg.generate_script(4, {"course_number": "AST101"}, tmp_path / "curriculum", tmp_path / "profile", out)
    assert not (out / "lecture-04" / "script.md").exists()


VALID_VARIANTS = "===BRIEF===\nShort.\n===STANDARD===\nMedium length.\n===THOROUGH===\nThe long one."


def test_teachme_openers_are_what_the_app_serves(tmp_path, monkeypatch):
    from pipeline.generate import teachme_generator as tg
    from tutor.agent import teachme

    monkeypatch.setattr(tg, "TEACHME_DIR", tmp_path)
    monkeypatch.setattr(tg, "_grounding", lambda goal, **kw: "[slides] excerpt")
    monkeypatch.setattr(tg, "_load_persona", lambda: "persona")
    answers = iter(["no delimiters at all", VALID_VARIANTS, "junk", "junk"])
    monkeypatch.setattr(tg, "_generate_via_cli", lambda system, user, model="sonnet": next(answers))
    goals = [{"id": "AST101-L03-seasons", "title": "Seasons", "description": "d", "lecture": 3},
             {"id": "AST101-L03-broken", "title": "Broken", "description": "d", "lecture": 3}]
    tracks = [{"id": "AST101-lecture-order", "title": "Lecture order", "description": ""}]
    summary = tg.generate_teachme(goals, tracks, "AST101", verbose=False)
    assert (summary["written"], summary["failed"]) == (1, ["AST101-L03-broken"])   # a retry, then a failure

    monkeypatch.setattr(teachme, "_TEACHME_DIR", tmp_path)
    teachme._load_goal.cache_clear()
    try:
        got = teachme.get_opener("AST101-L03-seasons", "AST101", track_id="AST101-lecture-order", depth="brief")
        assert got == {"opener": "Short.", "depth": "brief", "track": "AST101-lecture-order", "title": "Seasons"}
        assert teachme.get_opener("AST101-L03-broken", "AST101") is None
    finally:
        teachme._load_goal.cache_clear()


EXAM_JSON = ('```json\n{"questions": [{"id": "c1", "kind": "concept", "question": "What causes the seasons?", '
             '"rubric": "the tilt of the axis"}, {"id": "k1", "kind": "code", "question": "Write noon_altitude(lat).", '
             '"starter": "double noon_altitude(double lat);", "rubric": "90 minus the latitude plus the tilt"}]}\n```')


def test_exam_questions_are_what_the_exam_draws_from(tmp_path, monkeypatch):
    from pipeline import generate_mastery_exams as gen
    from tutor.agent import mastery

    goals = [{"id": "AST101-L03-seasons", "course": "AST101", "lecture": 3, "title": "Seasons", "description": "d"},
             {"id": "AST101-L03-other", "course": "AST101", "lecture": 3, "title": "Other", "description": "d"}]
    (tmp_path / "goals.json").write_text(json.dumps({"goals": goals}))
    monkeypatch.setattr(gen, "GOALS_PATH", tmp_path / "goals.json")
    monkeypatch.setattr(gen, "OUTPUT_PATH", tmp_path / "mastery_exams.json")
    monkeypatch.setattr(gen, "INTER_SLEEP", 0)
    outputs = iter([EXAM_JSON, "not json at all"])
    commands = []

    class Proc:
        returncode, pid = 0, 1

        def __init__(self, cmd, **kwargs):
            commands.append(cmd)
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]              # the subscription CLI, not the API

        def communicate(self, input, timeout):
            assert "Goal: " in input
            return next(outputs), ""

    monkeypatch.setattr(gen.subprocess, "Popen", Proc)
    assert gen.run_generation(course="AST101") == (1, 1)
    assert "--dangerously-skip-permissions" not in commands[0] and "--disallowed-tools" in commands[0]

    monkeypatch.setattr(mastery, "_EXAM_BANK_PATH", tmp_path / "mastery_exams.json")
    mastery._parse_exam_bank.cache_clear()
    try:
        pool = mastery._question_pool("AST101-L03-seasons")
        assert [(q["id"], q["kind"], q["rubric"] != "") for q in pool] == [("c1", "concept", True),
                                                                           ("k1", "code", True)]
        assert mastery._question_pool("AST101-L03-other") == []
    finally:
        mastery._parse_exam_bank.cache_clear()


def test_exercise_test_stubs_dry_run_writes_nothing():
    from pipeline import __main__ as cli_mod
    from tutor.paths import CURRICULUM_DIR

    tests_path = CURRICULUM_DIR / "exercise_tests.json"             # the fixture's, the file the CLI writes
    before = tests_path.read_bytes()
    result = CliRunner().invoke(cli_mod.cli, ["build-exercise-tests", "--dry-run"])
    assert result.exit_code == 0 and "code exercises" in result.output
    assert tests_path.read_bytes() == before


# ---------------------------------------------------------------------------
# The goal map: extraction, edges, anchors, edits
# ---------------------------------------------------------------------------

def test_lectures_and_edges_come_from_the_plan(tmp_path):
    from pipeline.extract_goals import derive_edges, parse_lectures

    (tmp_path / "plan.md").write_text(PLAN)
    lectures = parse_lectures(tmp_path / "plan.md", "AST101")
    assert [(lec["num"], lec["prereq_lectures"]) for lec in lectures] == [(1, []), (2, [1]), (3, [1, 2])]
    assert lectures[0]["objectives"] == ["Locate a star on the celestial sphere", "Measure an angle with an outstretched hand"]
    goals = [{"id": "AST101-L01-a", "course": "AST101", "lecture": 1}, {"id": "AST101-L01-b", "course": "AST101", "lecture": 1},
             {"id": "AST101-L02-c", "course": "AST101", "lecture": 2}, {"id": "AST101-L03-r", "course": "AST101", "lecture": 3}]
    assert derive_edges(goals, lectures) == [{"from": "AST101-L01-b", "to": "AST101-L02-c"}]   # review lecture skipped


def _goals_file(tmp_path) -> pathlib.Path:
    data = {"graph_version": 3, "goals": [
        {"id": "AST101-L01-a", "title": "A", "description": "", "course": "AST101", "lecture": 1},
        {"id": "AST101-L02-b", "title": "B", "description": "", "course": "AST101", "lecture": 2}],
        "edges": []}
    path = tmp_path / "learning_goals.json"
    path.write_text(json.dumps(data))
    return path


def test_reviewed_edges_are_applied_and_versioned(tmp_path):
    from pipeline import __main__ as cli_mod

    goals = _goals_file(tmp_path)
    diff = tmp_path / "diff.json"
    diff.write_text(json.dumps({"proposed_edges": [{"from": "AST101-L01-a", "to": "AST101-L02-b", "rationale": "r"}]}))
    result = CliRunner().invoke(cli_mod.cli, ["apply-goals-diff", "--diff", str(diff), "--goals", str(goals)])
    assert result.exit_code == 0, result.output
    data = json.loads(goals.read_text())
    assert data["edges"] == [{"from": "AST101-L01-a", "to": "AST101-L02-b"}] and data["graph_version"] == 4
    assert "order-tracks" in result.output

    diff.write_text(json.dumps({"proposed_edges": [{"from": "AST101-L01-a", "to": "no-such-goal"}]}))
    result = CliRunner().invoke(cli_mod.cli, ["apply-goals-diff", "--diff", str(diff), "--goals", str(goals)])
    assert result.exit_code == 1 and json.loads(goals.read_text())["graph_version"] == 4


def test_reviewed_anchors_are_applied(tmp_path):
    from pipeline import __main__ as cli_mod
    from pipeline.anchor_goals import _best_slide, _tokens

    goals = _goals_file(tmp_path)
    anchors = tmp_path / "anchors.json"
    anchors.write_text(json.dumps({"anchors": [{
        "id": "AST101-L01-a", "slide_anchor": {"lecture": 1, "slide": 9, "title": "The celestial sphere"},
        "book_anchor": {"chapter": "01", "section_id": "warmup", "title": "1.1 Warmup"}, "hook": "Find the pole star."}]}))
    dry = CliRunner().invoke(cli_mod.cli, ["apply-anchors", "--anchors", str(anchors), "--goals", str(goals),
                                           "--dry-run"])
    assert dry.exit_code == 0 and "slide_anchor" not in goals.read_text()
    CliRunner().invoke(cli_mod.cli, ["apply-anchors", "--anchors", str(anchors), "--goals", str(goals)])
    first = json.loads(goals.read_text())["goals"][0]
    assert first["slide_anchor"]["slide"] == 9 and first["hook"] == "Find the pole star."

    slides = [{"slide": 3, "title": "Constellations", "text": "patterns and their names"},
              {"slide": 9, "title": "The celestial sphere", "text": "locate a star on the celestial sphere"}]
    assert _best_slide(_tokens("Locate a star on the celestial sphere"), slides)["slide"] == 9


# ---------------------------------------------------------------------------
# Every pipeline module is placed
# ---------------------------------------------------------------------------

STUDENT_FACING: dict[str, list[str]] = {
    "pipeline/render/html_renderer.py": [f"{HERE}::test_the_deck_matches_its_golden_file",
                                         "test_pipeline_hygiene.py::test_the_deck_plays_the_clip_for_its_position"],
    "pipeline/render/book_audio.py": ["test_book_audio.py::test_narration_text_reads_prose_only",
                                      "test_book_audio.py::test_a_spend_writes_one_clip_per_chapter_and_resumes"],
    "pipeline/render/audio_renderer.py": [f"{HERE}::test_narration_is_the_spoken_lines_per_slide",
                                          "test_pipeline_hygiene.py::test_concat_list_quotes_an_apostrophe"],
    "pipeline/knowledge_base/indexer.py": [f"{HERE}::test_the_index_matches_its_golden_file"],
    "pipeline/generate/slide_generator.py": [f"{HERE}::test_generated_slides_are_what_the_deck_renders"],
    "pipeline/generate/script_generator.py": [f"{HERE}::test_a_script_is_written_only_when_it_follows_the_deck",
                                              f"{HERE}::test_a_misaligned_script_is_never_written"],
    "pipeline/generate/slide_marker.py": ["test_pipeline_hygiene.py::test_mark_slides_uses_the_course_lectures"],
    "pipeline/generate/teachme_generator.py": [f"{HERE}::test_teachme_openers_are_what_the_app_serves"],
    "pipeline/generate/curriculum_planner.py": [f"{HERE}::test_the_lecture_plan_is_what_the_tutor_outlines"],
    "pipeline/utils/extend_plan.py": [f"{HERE}::test_a_truncated_plan_is_extended_in_the_same_format"],
    "pipeline/generate_mastery_exams.py": [f"{HERE}::test_exam_questions_are_what_the_exam_draws_from"],
    "pipeline/build_exercises.py": ["test_exercise_bank.py::test_build_is_deterministic",
                                   "test_exercise_bank.py::test_build_leading_verb_classification"],
    "pipeline/build_exercise_tests.py": [f"{HERE}::test_exercise_test_stubs_dry_run_writes_nothing"],
    "pipeline/extract_goals.py": [f"{HERE}::test_lectures_and_edges_come_from_the_plan"],
    "pipeline/audit_goals.py": [f"{HERE}::test_reviewed_edges_are_applied_and_versioned"],
    "pipeline/anchor_goals.py": [f"{HERE}::test_reviewed_anchors_are_applied"],
    "pipeline/order_tracks.py": ["test_pipeline_hygiene.py::test_the_committed_tracks_are_ordered_and_versioned"],
    "pipeline/init_tracks.py": ["test_content_config.py::test_init_tracks_writes_a_lecture_order_track_per_course",
                                "test_content_config.py::test_init_tracks_keeps_existing_tracks_unless_forced"],
    "pipeline/lectures.py": ["test_pipeline_hygiene.py::test_status_agrees_with_generate_all_resume"],
    "pipeline/__main__.py": ["test_pipeline_hygiene.py::test_status_agrees_with_generate_all_resume",
                             "test_pipeline_hygiene.py::test_generate_all_refuses_lectures_that_do_not_exist"],
}

# Modules whose output no student sees directly, and why.
NOT_STUDENT_FACING: dict[str, str] = {
    "pipeline/__init__.py": "package marker",
    "pipeline/config.py": "configuration; test_pipeline_hygiene checks every key has a reader",
    "pipeline/check_content.py": "validates the instructor's content files; tests/test_content_config.py",
    "pipeline/generate/__init__.py": "package marker",
    "pipeline/generate/_utils.py": "reads a lecture's plan entry for the generators",
    "pipeline/generate/pptx_writer.py": "the retired PowerPoint format; tests/test_pptx_writer.py",
    "pipeline/render/__init__.py": "package marker",
    "pipeline/render/slide_renderer.py": "PNG frames: offline, not in the production seed",
    "pipeline/render/video_renderer.py": "MP4 lectures: offline, not served",
    "pipeline/knowledge_base/__init__.py": "package marker",
    "pipeline/knowledge_base/vectorstore.py": "the store client the indexer writes through (step 6.7)",
    "pipeline/analyze/__init__.py": "package marker",
    "pipeline/analyze/curriculum_mapper.py": "instructor-profile notes that feed the generators' prompts",
    "pipeline/analyze/example_extractor.py": "instructor-profile notes that feed the generators' prompts",
    "pipeline/analyze/style_extractor.py": "instructor-profile notes that feed the generators' prompts",
    "pipeline/utils/__init__.py": "package marker",
    "pipeline/audit_turns.py": "an operator report",
    "pipeline/cost_report.py": "an operator report",
    "pipeline/sample_preferences.py": "an operator report",
    "pipeline/seed_goals.py": "loads learning_goals.json into a local database; the app seeds itself",
}
INGEST_READERS = "raw inputs to the index; students see what the indexer builds from them"


def test_every_pipeline_module_is_placed():
    modules = {str(p.relative_to(ROOT)) for p in (ROOT / "pipeline").rglob("*.py")}
    placed = set(STUDENT_FACING) | set(NOT_STUDENT_FACING) | {m for m in modules if m.startswith("pipeline/ingest/")}
    assert sorted(modules - placed) == []
    assert sorted((set(STUDENT_FACING) | set(NOT_STUDENT_FACING)) - modules) == []
    assert not set(STUDENT_FACING) & set(NOT_STUDENT_FACING)


def test_every_student_facing_module_has_its_tests():
    missing = []
    for module, nodes in STUDENT_FACING.items():
        for node in nodes:
            filename, name = node.split("::")
            tree = ast.parse((TESTS / filename).read_text())
            if name not in {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}:
                missing.append(f"{module}: {node}")
    assert missing == []
