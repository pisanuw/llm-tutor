"""Slide source -> goal-graph lecture mapping (ROADMAP item 4).

An instructor's original slide decks are often numbered by week, while the
goal graph and the rendered decks follow the lecture plan. find_topic used to
take the filename prefix as the lecture number, so a hit in a week-3 deck
that teaches lecture 4 opened deck lecture-03 and drove scope and gating off
lecture 3.

Covers (on the fixture course, tests/fixtures/content):
- every mapped lecture exists in the course's lecture plan
- every source named in curriculum/<course>-topic-map.json is either mapped
  or explicitly listed as unmapped (adding a deck without deciding fails CI)
- no source is in both lists; the map is well-formed
- _lecture_num_from_source resolves exact names, case/extension variants,
  returns None for unknown sources, and never returns the week prefix
- _slide_deck_url follows the mapped lecture, not the filename prefix
- find_topic's slide_ref carries the goal-graph lecture (end to end with a
  stubbed retrieval)

The fixture's own decks happen to line up with the plan, so the cases about a
week number that differs from the lecture use a renumbered map of their own
(the ``renumbered`` fixture), patched in where tools reads the map.
"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

from tutor.agent import tools
from tutor.courses import COURSES
from tutor.paths import CURRICULUM_DIR, lecture_plan_path, topic_map_path

MAP_PATH = tools._SOURCE_MAP_PATH
GOALS_PATH = CURRICULUM_DIR / "learning_goals.json"


def _map() -> dict:
    return json.loads(MAP_PATH.read_text())


def _plan_lectures(course: str) -> set[int]:
    """Lecture numbers of the course's lecture plan (the canonical numbering
    that the goal graph and the rendered decks follow; review lectures can have
    decks but no goals, so the plan, not learning_goals.json, is the reference)."""
    plan = lecture_plan_path(course).read_text()
    nums = {int(m.group(1)) for m in re.finditer(r"^## Lecture (\d+):", plan, re.M)}
    goals = json.loads(GOALS_PATH.read_text())["goals"]
    goal_lectures = {int(g["lecture"]) for g in goals if g["course"] == course}
    assert goal_lectures <= nums, f"{course}: goal graph names lectures outside the plan"
    return nums


def _topic_map_sources(course: str) -> set[str]:
    tm = json.loads(topic_map_path(course).read_text())
    return {src for t in tm["topics"] for src in t.get("lectures_covered", [])}


@pytest.fixture
def renumbered(tmp_path, monkeypatch):
    """A map whose decks are numbered by week, not by the plan: the week-3
    deck '03Light.pptx' teaches AST101 lecture 4 and AST102 lecture 1."""
    path = tmp_path / "source-lecture-map.json"
    path.write_text(json.dumps({
        "AST101": {"map": {"02Seasons.pptx": 3, "03Light.pptx": 4}, "unmapped": ["03Feedback.pptx"]},
        "AST102": {"map": {"03Light.pptx": 1}, "unmapped": []},
    }))
    monkeypatch.setattr(tools, "_SOURCE_MAP_PATH", path)
    tools._source_lecture_map.cache_clear()
    yield
    tools._source_lecture_map.cache_clear()


@pytest.mark.parametrize("course", COURSES)
def test_every_mapped_lecture_exists_in_the_plan(course):
    spec = _map()[course]
    lectures = _plan_lectures(course)
    for src, lec in spec["map"].items():
        assert isinstance(lec, int) and lec in lectures, f"{course}: {src} -> L{lec} is not a plan lecture"


@pytest.mark.parametrize("course", COURSES)
def test_every_topic_map_source_has_a_decision(course):
    spec = _map()[course]
    mapped, unmapped = set(spec["map"]), set(spec["unmapped"])
    assert not (mapped & unmapped), f"{course}: in both lists: {sorted(mapped & unmapped)}"
    missing = _topic_map_sources(course) - mapped - unmapped
    assert not missing, (
        f"{course}: sources with no entry in source-lecture-map.json: {sorted(missing)}. "
        "Add each to 'map' (with its lecture) or to 'unmapped'."
    )


def test_map_file_is_well_formed():
    raw = _map()
    assert set(raw) >= set(COURSES)
    for course, spec in raw.items():
        if course.startswith("_"):
            continue
        assert set(spec) == {"map", "unmapped"}
        assert all(isinstance(v, int) for v in spec["map"].values())
        assert len(set(spec["unmapped"])) == len(spec["unmapped"]), "duplicate unmapped entry"


def test_the_fixture_decks_resolve_through_the_map():
    tools._source_lecture_map.cache_clear()
    f = tools._lecture_num_from_source
    assert f("AST101", "week2-moon.pptx") == 2
    assert f("AST101", "WEEK2-MOON.pdf") == 2
    assert f("AST102", "galaxies.pptx") == 3
    assert f("AST101", "midterm-review.pptx") is None          # unmapped on purpose
    assert f("AST102", "week2-moon.pptx") is None              # another course's deck


def test_resolution_uses_the_map_not_the_week_prefix(renumbered):
    f = tools._lecture_num_from_source
    # A week-3 deck about light and telescopes is lecture 04.
    assert f("AST101", "03Light.pptx") == 4
    assert f("AST101", "03Light.pptx") != 3
    # Case and extension variants resolve to the same lecture.
    assert f("AST101", "03light.pdf") == 4
    assert f("AST101", "03LIGHT") == 4
    # Same old deck, different course, different lecture.
    assert f("AST102", "03Light.pptx") == 1
    # Deliberately unmapped and unknown sources give None, never the prefix.
    assert f("AST101", "03Feedback.pptx") is None
    assert f("AST101", "99Mystery.pptx") is None
    assert f("AST101", "") is None
    assert f("999", "01Welcome.pptx") is None


@pytest.mark.parametrize("course", COURSES)
def test_a_number_in_an_unmapped_filename_is_never_the_lecture(course):
    """Guard the specific regression: a deck the map does not name never
    resolves to the number its filename starts with."""
    tools._source_lecture_map.cache_clear()
    for n in sorted(_plan_lectures(course)):
        for name in (f"{n:02d}Unmapped.pptx", f"{n}-unmapped.pptx", f"week{n}-unmapped.pptx"):
            assert tools._lecture_num_from_source(course, name) is None, name


def test_slide_deck_url_follows_mapped_lecture(tmp_path, renumbered):
    for n in (3, 4):
        d = tmp_path / "AST101" / f"lecture-{n:02d}"
        d.mkdir(parents=True)
        (d / "index.html").write_text("<html></html>")
    lec = tools._lecture_num_from_source("AST101", "03Light.pptx")
    assert tools._slide_deck_url(tmp_path, "AST101", lec) == "/slides/AST101/lecture-04/index.html"
    assert tools._slide_deck_url(tmp_path, "AST101", None) is None


def test_find_topic_slide_ref_carries_goal_graph_lecture(tmp_path, renumbered):
    """End to end with a stubbed collection: a week-3 telescope slide hit
    yields lecture 4 and the lecture-04 deck."""
    deck = tmp_path / "AST101" / "lecture-04"
    deck.mkdir(parents=True)
    (deck / "index.html").write_text("<html></html>")

    class _Coll:
        def count(self):
            return 1

        def query(self, **kw):
            return {
                "documents": [["a telescope's light-gathering power grows with its aperture"]],
                "metadatas": [[{"source_file": "03Light.pptx", "slide_number": 12,
                                "lecture_title": "Light and Telescopes"}]],
                "distances": [[0.1]],
            }

    class _Empty:
        def count(self):
            return 0

    def _collection(name):
        return _Coll() if name == "slides" else _Empty()

    with patch.object(tools, "_collection", side_effect=_collection), \
         patch.object(tools, "_embed_query", return_value=[[0.0]]), \
         patch.object(tools, "get_config", return_value={"output_dir": tmp_path}):
        out = tools.find_topic("how does a telescope collect light", "AST101", top_k=3)
    ref = out["slide_ref"]
    assert ref is not None
    assert ref["lecture_num"] == 4
    assert ref["slide_url"] == "/slides/AST101/lecture-04/index.html"
    assert ref["source_file"] == "03Light.pptx"
