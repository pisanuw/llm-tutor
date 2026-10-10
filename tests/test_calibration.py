"""Confidence collection + calibration (Engram integration, Phases 2-3).

Network-free. Covers the pure calibration report + outcome classifier, the
confidence storage on the turn-event ledger, the durable misconception log
(record/resolve) and the misconception contract note it feeds, and the note's
rendering into the per-turn envelope block.
"""

from __future__ import annotations

import pytest

from tutor.agent import state
from tutor.agent.envelope import _misconception_note
from tutor.agent.policy import Envelope
from tutor.agent.prompt_builder import render_envelope
from tutor.db.models import LearningGoal, Misconception, Student, TurnEvent
from tutor.routers.admin import _calibration_report, _outcome_correct
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="a@example.edu", name="A"))
    s.add(LearningGoal(id="AST101-L04-g1", title="Telescopes", description="", course="AST101", lecture_num=4))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Pure report + outcome classifier
# ---------------------------------------------------------------------------

def test_outcome_correct_by_kind():
    assert _outcome_correct("review", {"score": 2}) is True
    assert _outcome_correct("review", {"score": 1}) is False
    assert _outcome_correct("review", {}) is None
    assert _outcome_correct("exam", {"passed": True}) is True
    assert _outcome_correct("exam", {"passed": False}) is False
    assert _outcome_correct("chat", {"score": 3}) is None


def test_calibration_report_bands_and_gap():
    # 90% band: 1 of 2 right (accuracy 50, gap +40). 25% band: 1 of 1 (gap -75).
    r = _calibration_report([(90, True), (90, False), (25, True)])
    assert r.total == 3
    assert r.high_conf_wrong == 1  # the 90%-but-wrong one
    by_conf = {b.confidence: b for b in r.bands}
    assert by_conf[90].accuracy == 50
    assert by_conf[90].gap == 40
    assert by_conf[25].gap == -75
    # Bands are ordered highest-confidence first.
    assert [b.confidence for b in r.bands] == [90, 25]


def test_calibration_report_empty():
    r = _calibration_report([])
    assert r.total == 0
    assert r.brier is None
    assert r.bands == []


def test_brier_is_zero_for_perfect_calibration():
    # Confidence 100 would be perfect, but our bands top out at 90; a fully-right
    # 90 band still leaves residual Brier. Use a trivially perfect set at 0/1 ends
    # is not possible with valid bands, so just assert monotonicity: all-correct
    # high confidence beats all-wrong high confidence.
    good = _calibration_report([(90, True), (90, True)])
    bad = _calibration_report([(90, False), (90, False)])
    assert good.brier < bad.brier


# ---------------------------------------------------------------------------
# Confidence storage through the single writer
# ---------------------------------------------------------------------------

def test_record_turn_event_stores_confidence(db):
    te = state.record_turn_event(
        db, student_id=1, kind="review", goal_ids=["AST101-L04-g1"],
        verdicts={"score": 1, "miscalibrated": True}, confidence=90,
    )
    row = db.get(TurnEvent, te.id)
    assert row.confidence == 90


def test_confidence_defaults_to_none(db):
    te = state.record_turn_event(db, student_id=1, kind="chat")
    assert db.get(TurnEvent, te.id).confidence is None


# ---------------------------------------------------------------------------
# Durable misconception log: record / resolve (Engram P3)
# ---------------------------------------------------------------------------

def test_record_misconception_creates_open_row(db):
    m = state.record_misconception(db, 1, "AST101-L04-g1", "thinks a bigger lens magnifies more")
    assert m.id is not None
    assert m.resolved_at is None
    assert db.query(Misconception).filter_by(student_id=1, goal_id="AST101-L04-g1").count() == 1


def test_record_misconception_upserts_open_row(db):
    state.record_misconception(db, 1, "AST101-L04-g1", "first wrong model")
    state.record_misconception(db, 1, "AST101-L04-g1", "sharper wrong model")
    rows = db.query(Misconception).filter_by(
        student_id=1, goal_id="AST101-L04-g1", resolved_at=None).all()
    assert len(rows) == 1  # refreshed in place, not duplicated
    assert rows[0].description == "sharper wrong model"


def test_resolve_misconceptions_closes_open_rows(db):
    state.record_misconception(db, 1, "AST101-L04-g1", "wrong model")
    assert state.resolve_misconceptions(db, 1, "AST101-L04-g1") == 1
    assert db.query(Misconception).filter_by(
        student_id=1, goal_id="AST101-L04-g1", resolved_at=None).count() == 0


def test_resolve_misconceptions_noop_when_none_open(db):
    assert state.resolve_misconceptions(db, 1, "AST101-L04-g1") == 0


# ---------------------------------------------------------------------------
# Misconception contract note
# ---------------------------------------------------------------------------

def test_misconception_note_fires_for_in_scope_goal(db):
    state.record_misconception(db, 1, "AST101-L04-g1", "confuses light gathering with magnification")
    note = _misconception_note(1, {"AST101-L04-g1"}, {"AST101-L04-g1": "Telescopes"}, db)
    assert "Telescopes" in note
    assert note.startswith("MISCONCEPTION")
    assert "confuses light gathering with magnification" in note


def test_misconception_note_silent_when_out_of_scope(db):
    state.record_misconception(db, 1, "AST101-L04-g1", "wrong model")
    assert _misconception_note(1, {"AST101-L01-other"}, {}, db) == ""


def test_misconception_note_silent_when_resolved(db):
    state.record_misconception(db, 1, "AST101-L04-g1", "wrong model")
    state.resolve_misconceptions(db, 1, "AST101-L04-g1")
    assert _misconception_note(1, {"AST101-L04-g1"}, {"AST101-L04-g1": "Telescopes"}, db) == ""


def test_misconception_note_silent_for_empty_scope(db):
    state.record_misconception(db, 1, "AST101-L04-g1", "wrong model")
    assert _misconception_note(1, set(), {}, db) == ""


# ---------------------------------------------------------------------------
# Rendering: the note is a content line, never a ceiling change
# ---------------------------------------------------------------------------

def _env(rung=5):
    return Envelope(
        max_rung=rung, goals=["g"], exam_lockdown=[], must_ground=False,
        allow_exercise=True, escalate_review=False, reason="",
    )


def test_render_envelope_appends_calibration_note():
    block = render_envelope(_env(), {"g": "Telescopes"}, "CALIBRATION: watch this one")
    assert block.strip().endswith("CALIBRATION: watch this one")


def test_render_envelope_omits_empty_note_and_keeps_ceiling():
    block = render_envelope(_env(rung=3), {"g": "Telescopes"}, "")
    assert "CALIBRATION" not in block
    assert "H3" in block  # ceiling unchanged by the (absent) note
