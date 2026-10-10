"""tutor/supervision_view.py: what each cell of the admin Supervision table
reads, and which rows the table shows.

The table lists ``turn_events``, where a turn (``chat``) carries the whole
supervision record and a supervisor call (``classify``, ``strategist``,
``judge``, ``cache_warm``, ...) carries only its tokens and cost. In
production that was 97 turns among 376 rows, so six columns read "—" on three
rows in four and looked like missing data. These checks pin the wording:
allow / revise / block for a verdict, a detector that says "clean" when it ran
and removed nothing, a ceiling that shows the band it moved from, and "n/a"
where a column cannot apply.
"""
from __future__ import annotations

from tutor import supervision_view as view


# ---------------------------------------------------------------------------
# Verdict: the three the judge can return
# ---------------------------------------------------------------------------

def test_a_verdict_is_allow_revise_or_block():
    assert view.verdict_cell("chat", {"verdict": "allow"}).text == "allow"
    assert view.verdict_cell("chat", {"verdict": "revise"}).text == "revise"
    assert view.verdict_cell("chat", {"verdict": "block"}).text == "block"


def test_a_detector_cut_reads_revise_because_the_draft_was_changed():
    cell = view.verdict_cell("chat", {"verdict": "detector_cut", "detector_cut": True})
    assert cell.text == "revise" and cell.tone == "warn"
    assert "did not get the draft" in cell.title


def test_a_turn_still_running_or_failed_is_not_a_verdict():
    assert view.verdict_cell("chat", {"verdict": "in_progress"}).text == "running"
    for failure in ("stream_error", "empty_reply", "crash"):
        cell = view.verdict_cell("chat", {"verdict": failure})
        assert cell.text == "error" and failure in cell.title


def test_an_allow_says_whether_the_judge_ran():
    assert "judge does not run" in view.verdict_cell("chat", {"verdict": "allow"}).title
    assert view.verdict_cell("chat", {"verdict": "allow", "judged": True}).title == "the judge allowed it"


def test_a_supervisor_call_has_no_verdict_and_says_so():
    for kind in ("classify", "strategist", "judge", "cache_warm", "evaluate"):
        cell = view.verdict_cell(kind, {})
        assert cell.text == "n/a" and "not a turn" in cell.title and cell.tone == "muted"
    # the strategist's row carries its move, which is not a verdict
    assert view.verdict_cell("strategist", {"move": "PROBE_PRIOR"}).text == "n/a"


def test_a_graded_row_shows_its_score():
    assert view.verdict_cell("exam", {"score": 0.75}).text == "score 0.75"
    assert view.verdict_cell("exercise", {"score": 1.0}).text == "score 1.0"


# ---------------------------------------------------------------------------
# Detector: ran and found nothing is not the same as never ran
# ---------------------------------------------------------------------------

def test_clean_means_it_ran_and_removed_nothing():
    cell = view.detector_cell("chat", {"verdict": "allow"}, {})
    assert cell.text == "clean" and "removed nothing" in cell.title
    assert view.detector_cell("chat", {"verdict": "allow"}, {"cut": False}).text == "clean"


def test_cut_means_it_removed_something():
    assert view.detector_cell("chat", {"detector_cut": True}, {}).text == "cut"
    assert view.detector_cell("chat", {}, {"cut": True}).text == "cut"
    assert view.detector_cell("chat", {"detector_cut": True}, {}).tone == "bad"


def test_a_turn_with_no_reply_never_reached_the_detector():
    for failure in ("in_progress", "stream_error", "empty_reply", "crash"):
        assert view.detector_cell("chat", {"verdict": failure}, {}).text == "not run"


def test_off_a_turn_there_is_no_reply_to_scan():
    assert view.detector_cell("classify", {}, {}).text == "n/a"


# ---------------------------------------------------------------------------
# Rung: the ceiling, and the band it moved from
# ---------------------------------------------------------------------------

def test_one_number_when_nothing_moved_the_band_ceiling():
    cell = view.rung_cell("chat", 5, {"max_rung": 5, "rung_base": 5, "reason": "mastery band ceiling H5"})
    assert cell.text == "H5" and cell.title == "mastery band ceiling H5"


def test_both_numbers_when_a_floor_or_a_cap_moved_it():
    cell = view.rung_cell("chat", 4, {"max_rung": 4, "rung_base": 2, "reason": "floors ceiling at H4"})
    assert cell.text == "H2→H4" and cell.tone == "warn"
    # an exam lockdown tightens instead of lifting; the pair reads the same way
    assert view.rung_cell("chat", 1, {"max_rung": 1, "rung_base": 5}).text == "H5→H1"


def test_a_row_from_before_rung_base_shows_the_enforced_ceiling_alone():
    assert view.rung_cell("chat", 5, {"max_rung": 5}).text == "H5"


def test_the_envelope_ceiling_stands_in_when_the_row_has_no_rung():
    assert view.rung_cell("chat", None, {"max_rung": 3}).text == "H3"
    assert view.rung_cell("chat", None, {}).text == "—"
    assert view.rung_cell("classify", None, {}).text == "n/a"


# ---------------------------------------------------------------------------
# The kind filter
# ---------------------------------------------------------------------------

def test_the_default_is_the_turns_and_anything_odd_falls_back_to_it():
    available = ["chat", "classify", "strategist"]
    assert view.normalize_kind("classify", available) == "classify"
    assert view.normalize_kind("all", available) == view.ALL
    for odd in (None, "", "   ", "nonsense", "x" * 500):
        assert view.normalize_kind(odd, available) == "chat"
    # a kind the table does not hold is not offered
    assert view.normalize_kind("exam", available) == "chat"
    # ... and "all" works even on an empty table
    assert view.normalize_kind("all", []) == view.ALL


def test_the_choices_put_the_turns_first_and_all_kinds_last():
    choices = view.kind_choices([("classify", 102), ("chat", 97), ("judge", 36)], "chat")
    assert [c.kind for c in choices] == ["chat", "classify", "judge", "all"]
    assert [c.count for c in choices] == [97, 102, 36, 235]
    assert [c.active for c in choices] == [True, False, False, False]
    assert view.kind_choices([("chat", 1)], "all")[-1].active is True


def test_is_turn_knows_a_turn_from_a_call():
    assert view.is_turn("chat") is True
    for call in ("classify", "strategist", "judge", "cache_warm", None, ""):
        assert view.is_turn(call) is False
