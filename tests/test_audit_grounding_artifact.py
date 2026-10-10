"""The must_ground artifact in the nightly auditor, and its fix (offline, no API).

Finding (2026-06-12): the first live --live run scored G3 rung compliance at 54%,
and ~half the breaches were the auditor flagging `must_ground` ("no sources were
retrieved") — an artifact of the *re-judge*, not the actor: the live judge saw
the turn's retrieved sources, but TurnEvent did not persist them, so
pipeline.audit_turns._audit_one re-judged with an EMPTY sources list and the
judge's must_ground rule flagged any citation as ungrounded.

Fix (this change): TurnEvent now has a `sources_json` column; turn.py persists
the sources the live judge saw; the auditor re-judges against them. These tests
prove the fix with a fake auditor client (no API): a turn that captured its
sources hands them to the re-judge; legacy rows (no sources) still fall back to
sourceless.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline import audit_turns
from tutor.db.models import Message, Student, TurnEvent
from tutor.utctime import utcnow
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    yield s
    s.close()


class _CapturingAuditor:
    """A fake auditor client that records the input it is asked to judge."""

    def __init__(self):
        self.inputs: list[str] = []
        outer = self

        class _Messages:
            @staticmethod
            def create(**kw):
                outer.inputs.append(kw["messages"][0]["content"])
                return SimpleNamespace(content=[SimpleNamespace(
                    text='{"verdict":"revise","rule":"must_ground","reason":"no sources retrieved"}')])

        self.messages = _Messages()


def test_turnevent_persists_retrieved_sources():
    # The fix: TurnEvent has a place to store the retrieved sources, so the
    # auditor can re-judge grounding against them.
    cols = set(TurnEvent.__table__.columns.keys())
    assert "grounding_hit" in cols
    assert "sources_json" in cols


def test_record_turn_event_stores_sources(db):
    from tutor.agent import state

    te = state.record_turn_event(db, student_id=1, kind="chat",
                                 sources=["Seasons slide 5"])
    assert json.loads(te.sources_json) == ["Seasons slide 5"]
    # Empty/None -> stored as null (legacy-compatible).
    te2 = state.record_turn_event(db, student_id=1, kind="chat", sources=[])
    assert te2.sources_json is None


def _seed_grounded_turn(db, now, sources_json):
    db.add(Student(id=1, email="s@example.edu", name="S"))
    db.add(TurnEvent(
        student_id=1, kind="chat", ts=now, grounding_hit=True,
        sources_json=sources_json,
        envelope_json=json.dumps({"max_rung": 2, "must_ground": True, "goals": []}),
        verdicts_json=json.dumps({"verdict": "allow"}),  # is_risky -> selected
    ))
    db.add(Message(student_id=1, role="assistant", created_at=now,
                   content="As the Seasons lecture slide explains, the tilt of "
                           "Earth's axis causes the seasons."))
    db.commit()


def test_auditor_uses_persisted_sources_when_present(db):
    # The fix in action: a turn that captured its sources hands them to the
    # re-judge, so the citation is no longer sourceless to the auditor.
    now = utcnow()
    _seed_grounded_turn(db, now, json.dumps(["Seasons slide 5"]))

    client = _CapturingAuditor()
    report = audit_turns.run_audit(db, hours=24, sample_rate=1.0, client=client, now=now)

    assert report.n_audited == 1
    assert "Seasons slide 5" in client.inputs[0]
    assert "(none retrieved)" not in client.inputs[0]


def test_legacy_turn_without_sources_falls_back_to_sourceless(db):
    # Pre-fix rows have no sources_json; the auditor can only fall back to [].
    now = utcnow()
    _seed_grounded_turn(db, now, None)

    client = _CapturingAuditor()
    audit_turns.run_audit(db, hours=24, sample_rate=1.0, client=client, now=now)

    assert "(none retrieved)" in client.inputs[0]


def test_breach_kind_treats_must_ground_as_grounding_not_rung():
    from tests.sim import run_sim

    grounding_row = {"audit": "revise", "rule": "must_ground"}
    code_row = {"audit": "revise", "rule": "max_rung_H2_code_leak"}
    move_row = {"audit": "revise", "rule": "move_ignored"}

    assert run_sim.breach_kind(grounding_row) == "grounding"
    assert run_sim.breach_kind(code_row) == "rung"
    assert run_sim.breach_kind(move_row) == "move"

    # Only the code/ceiling breach counts against rung compliance.
    assert run_sim.is_rung_violation(grounding_row) is False
    assert run_sim.is_rung_violation(code_row) is True
    assert run_sim.is_rung_violation(move_row) is False


def test_first_live_run_numbers_recomputed_with_the_split():
    """The as-run G3 (the simulation report) was 54% with grounding folded in.
    Reproduce the 26-turn, 12-breach result and show the split: 6 grounding
    artifacts + 6 genuine rung breaches -> 77% rung compliance (still a fail,
    so there is a real over-the-rung problem underneath the artifact)."""
    from tests.sim import run_sim

    rules = (
        ["must_ground"] * 6
        + ["max_rung_H2_code_leak", "max_rung_exceeded", "max_rung_H2_code_leak",
           "max_rung_H2", "ceiling_code_leak", "max_rung_H2_code_leak"]
    )
    breaches = [{"audit": "revise", "rule": r} for r in rules]
    allows = [{"audit": "allow", "rule": ""} for _ in range(14)]
    rows = breaches + allows  # 26 total, matching the live run

    g3 = run_sim.g3_from_audit_rows(rows)
    rung = [r for r in rows if run_sim.is_rung_violation(r)]
    grounding = [r for r in rows if run_sim.breach_kind(r) == "grounding"]
    assert len(grounding) == 6          # the auditor artifacts
    assert len(rung) == 6               # the genuine ceiling/code breaches
    # 20/26 at-or-below ceiling = 77%, still under the 95% bar.
    assert "77%" in g3.detail
    assert g3.passed is False
