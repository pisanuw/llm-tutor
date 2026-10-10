"""Session 11 tests: the pedagogy playbook loader.

Exit gate: playbook loads cleanly, all moves covered, min_rung matches
policy, strategist and actor prompt renderers produce non-empty output
with every move name present, and the fail-soft contract holds on bad
input.
"""

from __future__ import annotations

import pytest

from tutor.agent import playbook as pb_module
from tutor.agent.playbook import (
    Playbook,
    _parse,
    actor_move_vocabulary,
    load_playbook,
    strategist_system,
)
from tutor.agent.policy import MOVE_MIN_RUNG, Move


# ---------------------------------------------------------------------------
# Load + validation
# ---------------------------------------------------------------------------

def test_playbook_loads_successfully():
    pb = load_playbook()
    assert pb is not None
    assert isinstance(pb, Playbook)
    assert pb.version == 1


def test_playbook_covers_all_policy_moves():
    pb = load_playbook()
    assert pb is not None
    assert {m.move for m in pb.moves} == set(Move)


def test_playbook_min_rung_matches_policy():
    pb = load_playbook()
    assert pb is not None
    for m in pb.moves:
        expected = MOVE_MIN_RUNG.get(m.move, 0)
        assert m.min_rung == expected, (
            f"{m.move.value}: playbook min_rung={m.min_rung} but policy says {expected}"
        )


def test_every_move_has_required_fields():
    pb = load_playbook()
    assert pb is not None
    for m in pb.moves:
        assert m.definition, f"{m.move.value}: missing definition"
        assert m.when, f"{m.move.value}: missing when signals"
        assert len(m.openers) >= 2, f"{m.move.value}: needs >= 2 openers"


def test_playbook_has_heuristics():
    pb = load_playbook()
    assert pb is not None
    assert len(pb.heuristics) >= 5


def test_playbook_get_returns_correct_move():
    pb = load_playbook()
    assert pb is not None
    m = pb.get(Move.WORKED_ANALOGY)
    assert m is not None
    assert m.move is Move.WORKED_ANALOGY
    assert m.min_rung == 5


# ---------------------------------------------------------------------------
# Rendered prompt fragments
# ---------------------------------------------------------------------------

def test_strategist_system_contains_all_moves():
    s = strategist_system()
    assert s is not None
    for m in Move:
        assert m.value in s, f"strategist system missing {m.value}"


def test_strategist_system_contains_heuristics():
    s = strategist_system()
    assert s is not None
    assert "null" in s  # the "return null" fallback instruction


def test_actor_vocabulary_contains_all_moves():
    v = actor_move_vocabulary()
    assert v is not None
    for m in Move:
        assert m.value in v, f"actor vocabulary missing {m.value}"


def test_actor_vocabulary_contains_openers():
    v = actor_move_vocabulary()
    assert v is not None
    assert 'e.g.' in v


# ---------------------------------------------------------------------------
# Fail-soft: _parse raises on bad data; load_playbook returns None
# ---------------------------------------------------------------------------

def _minimal_raw(overrides: dict | None = None) -> dict:
    """Build a minimal but valid raw playbook dict for _parse."""
    moves = {}
    for m in Move:
        moves[m.value] = {
            "definition": f"Definition of {m.value}.",
            "when": ["some signal"],
            "openers": ["Opener one.", "Opener two."],
        }
    for move, rung in MOVE_MIN_RUNG.items():
        moves[move.value]["min_rung"] = rung
    raw = {"version": 1, "moves": moves, "strategist_heuristics": ["heuristic one"]}
    if overrides:
        raw.update(overrides)
    return raw


def test_parse_accepts_valid_minimal_raw():
    pb = _parse(_minimal_raw())
    assert {m.move for m in pb.moves} == set(Move)


def test_parse_rejects_unknown_move():
    raw = _minimal_raw()
    raw["moves"]["MADE_UP_MOVE"] = {"definition": "x", "when": ["y"], "openers": ["a", "b"]}
    with pytest.raises(ValueError, match="unknown"):
        _parse(raw)


def test_parse_rejects_missing_move():
    raw = _minimal_raw()
    del raw["moves"][Move.ADVANCE.value]
    with pytest.raises(ValueError, match="missing"):
        _parse(raw)


def test_parse_rejects_wrong_min_rung():
    raw = _minimal_raw()
    raw["moves"][Move.WORKED_ANALOGY.value]["min_rung"] = 99
    with pytest.raises(ValueError, match="min_rung"):
        _parse(raw)


def test_parse_rejects_missing_openers():
    raw = _minimal_raw()
    raw["moves"][Move.ENCOURAGE_ATTEMPT.value]["openers"] = ["only one"]
    with pytest.raises(ValueError, match="openers"):
        _parse(raw)


def test_parse_rejects_empty_heuristics():
    raw = _minimal_raw({"strategist_heuristics": []})
    with pytest.raises(ValueError, match="heuristics"):
        _parse(raw)


def test_load_playbook_returns_none_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_module, "PLAYBOOK_PATH", tmp_path / "nonexistent.yaml")
    pb_module.load_playbook.cache_clear()
    try:
        assert pb_module.load_playbook() is None
    finally:
        pb_module.load_playbook.cache_clear()  # so other tests get the real playbook
