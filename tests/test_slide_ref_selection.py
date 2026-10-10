"""Which retrieved hit the slides panel follows (find_topic's slide_ref).

The panel moves only when find_topic returns a slide_ref with a deck URL
(actor_loop emits the `slide` event from it). slide_ref used to be chosen from
the top_k excerpts handed to the actor, but transcript chunks outscore slide
chunks for nearly every question -- against the real store the best slide hit
for a typical question was rank 44 of 80 -- so in practice the
panel never moved. The selection now scans every hit, bounded by a relevance
floor so a question the course does not cover cannot drag a deck onto screen.

Covers:
- a slide hit past top_k still drives the panel, and the excerpts stay top_k
- a slide hit far below the best hit's score is ignored (relative floor)
- a weakly-matching query promotes nothing (absolute floor)
- a slide whose deck does not exist yields metadata but no URL, so no jump
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tutor.agent import tools

# 'week3-seasons.pptx' is lecture 03 for AST101 in the fixture's curriculum/source-lecture-map.json.
SLIDE_SOURCE = "week3-seasons.pptx"
SLIDE_LECTURE = 3


class _Fake:
    """A collection returning fixed (document, metadata, score) rows."""

    def __init__(self, rows):
        self._rows = rows

    def count(self):
        return len(self._rows) or 1

    def query(self, **kw):
        n = kw.get("n_results", len(self._rows))
        rows = self._rows[:n]
        return {
            "documents": [[r[0] for r in rows]],
            "metadatas": [[r[1] for r in rows]],
            "distances": [[1.0 - r[2] for r in rows]],
        }


def _run(tmp_path, slide_score, transcript_scores=(0.80, 0.79, 0.78, 0.77, 0.76),
         make_deck=True, top_k=5):
    """find_topic over one transcript collection and one slide collection."""
    if make_deck:
        deck = tmp_path / "AST101" / f"lecture-{SLIDE_LECTURE:02d}"
        deck.mkdir(parents=True, exist_ok=True)
        (deck / "index.html").write_text("<html></html>")

    transcripts = _Fake([
        (f"lecture audio {i}", {"source_file": f"t{i}.json", "lecture_title": "Talk"}, s)
        for i, s in enumerate(transcript_scores)
    ])
    slides = _Fake([
        ("the Sun's path across the sky at the solstices",
         {"source_file": SLIDE_SOURCE, "slide_number": 12, "lecture_title": "Seasons"},
         slide_score),
    ])
    empty = _Fake([])

    def _collection(name):
        if name == "transcripts":
            return transcripts
        if name == "slides":
            return slides
        return empty

    tools._source_lecture_map.cache_clear()
    with patch.object(tools, "_collection", side_effect=_collection), \
         patch.object(tools, "_embed_query", return_value=[[0.0]]), \
         patch.object(tools, "get_config", return_value={"output_dir": tmp_path}):
        return tools.find_topic("why are there seasons", "AST101", top_k=top_k)


def test_a_slide_hit_past_top_k_still_moves_the_panel(tmp_path):
    """The regression: five better-scoring transcripts fill top_k, and the slide
    at rank 6 is still the one the panel follows."""
    out = _run(tmp_path, slide_score=0.60)

    assert [r["collection"] for r in out["results"]] == ["transcripts"] * 5, \
        "the excerpts are the best hits, which are all transcripts"
    ref = out["slide_ref"]
    assert ref is not None, "a slide outside top_k must still be found"
    assert ref["slide_url"] == f"/slides/AST101/lecture-{SLIDE_LECTURE:02d}/index.html"
    assert ref["slide_number"] == 12


def test_the_excerpts_handed_to_the_actor_stay_top_k(tmp_path):
    """Scanning deeper for a slide must not widen the actor's context."""
    out = _run(tmp_path, slide_score=0.60, top_k=3)
    assert len(out["results"]) == 3


@pytest.mark.parametrize("slide_score", [0.47, 0.20])
def test_a_slide_far_below_the_best_hit_is_ignored(tmp_path, slide_score):
    """Relative floor: 0.6 x the top hit (0.80) is 0.48, so 0.47 is out."""
    out = _run(tmp_path, slide_score=slide_score)
    assert out["slide_ref"] is None


def test_a_slide_near_the_best_hit_is_taken(tmp_path):
    """Just above the same floor, the slide is chosen: the boundary is real."""
    out = _run(tmp_path, slide_score=0.49)
    assert out["slide_ref"] is not None


def test_a_query_nothing_matches_well_promotes_nothing(tmp_path):
    """Absolute floor: every hit is weak, so the relative floor alone would
    still promote the slide. Nothing should reach the panel."""
    out = _run(tmp_path, slide_score=0.25, transcript_scores=(0.28, 0.27, 0.26))
    assert out["slide_ref"] is None


def test_a_slide_whose_deck_is_missing_gives_metadata_but_no_jump(tmp_path):
    """Scope refinement still wants the lecture; the panel must stay put."""
    out = _run(tmp_path, slide_score=0.60, make_deck=False)
    ref = out["slide_ref"]
    assert ref is not None and ref["slide_url"] is None
    assert ref["lecture_num"] == SLIDE_LECTURE
