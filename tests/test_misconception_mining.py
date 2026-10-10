"""Cohort misconception mining tests (pedagogy item 12). The model
call is always mocked; no tokens are spent."""

from __future__ import annotations

import datetime
import json

import pytest

from tutor.agent import misconception_mining as mm
from tutor.db.models import GoalMisconception, LearningGoal, Misconception, Student
from tests.helpers import make_session

NOW = datetime.datetime(2026, 9, 1, 12, 0, 0)
SINCE = NOW - datetime.timedelta(days=14)


@pytest.fixture()
def db():
    s = make_session()
    for gid in ("g1", "g2"):
        s.add(LearningGoal(id=gid, title=f"Goal {gid}", description="", course="AST101", lecture_num=1))
    s.commit()
    yield s
    s.close()


def _students(db, n):
    out = [Student(email=f"m{i}@example.com", name=f"M{i}") for i in range(n)]
    db.add_all(out)
    db.commit()
    return out


def _note(db, sid, gid, text, when=None):
    db.add(Misconception(student_id=sid, goal_id=gid, description=text,
                         created_at=when or (NOW - datetime.timedelta(days=1))))


class _FakeClient:
    def __init__(self, text):
        self.calls = 0
        outer = self

        class _Block:
            type = "text"

            def __init__(self, t):
                self.text = t

        class _Msg:
            def create(self, **kwargs):
                outer.calls += 1

                class _Resp:
                    content = [_Block(text)]
                return _Resp()

        self.messages = _Msg()


def test_gather_and_threshold(db):
    st = _students(db, 5)
    for s in st[:4]:
        _note(db, s.id, "g1", "thinks the phases come from the shadow of the Earth")
    _note(db, st[4].id, "g2", "only one student here")
    _note(db, st[0].id, "g1", "outside window", when=NOW - datetime.timedelta(days=30))
    db.commit()
    ev = mm.gather(db, SINCE, NOW)
    assert set(ev) == {"g1", "g2"}
    assert len(ev["g1"].students) == 4 and len(ev["g1"].notes) == 4
    todo = mm.eligible(ev.values())
    assert [e.goal_id for e in todo] == ["g1"]   # g2 below MIN_STUDENTS


def test_dry_run_makes_no_model_calls(db):
    for s in _students(db, 4):
        _note(db, s.id, "g1", "wrong model")
    db.commit()
    client = _FakeClient("[]")
    rep = mm.run(db, SINCE, NOW, dry_run=True, client=client)
    assert rep.goals_eligible == 1 and rep.goals_mined == 0 and client.calls == 0
    assert db.query(GoalMisconception).count() == 0


def test_parse_items_strips_fences_and_caps():
    raw = '```json\n' + json.dumps([
        {"description": f"m{i}", "distractor": f"d{i}", "count": 2} for i in range(6)
    ]) + '\n```'
    items = mm._parse_items(raw)
    assert len(items) == mm.MAX_ITEMS_PER_GOAL
    assert items[0] == {"description": "m0", "distractor": "d0", "count": 2}
    assert mm._parse_items("not json") == []
    assert mm._parse_items('{"description": "x"}') == []   # must be a list


def test_run_upserts_candidates_and_dedupes(db):
    for s in _students(db, 4):
        _note(db, s.id, "g1", "thinks the shadow of the Earth makes the phases")
    db.commit()
    payload = json.dumps([
        {"description": "Phases come from the shadow of the Earth.", "distractor": "A half Moon is half in shadow.", "count": 3},
        {"description": "The Moon makes its own light", "distractor": "The Moon glows.", "count": 1},
    ])
    rep = mm.run(db, SINCE, NOW, client=_FakeClient(payload), run_label="w1")
    assert rep.goals_mined == 1 and rep.candidates_new == 2
    rows = db.query(GoalMisconception).all()
    assert {r.status for r in rows} == {"candidate"}
    # Second run: same descriptions (different punctuation/case) refresh, not duplicate.
    payload2 = json.dumps([{"description": "phases come from the shadow of the earth", "distractor": "", "count": 2}])
    rep2 = mm.run(db, SINCE, NOW, client=_FakeClient(payload2), run_label="w2")
    assert rep2.candidates_new == 0 and rep2.candidates_updated == 1
    row = db.query(GoalMisconception).filter_by(description="Phases come from the shadow of the Earth.").one()
    assert row.evidence_count == 5 and row.run_label == "w2"
    assert row.distractor == "A half Moon is half in shadow."   # kept when the refresh has none


def test_only_approved_rows_reach_prompts(db):
    db.add(GoalMisconception(goal_id="g1", description="cand", status="candidate", evidence_count=9))
    db.add(GoalMisconception(goal_id="g1", description="appr", status="approved", evidence_count=2))
    db.add(GoalMisconception(goal_id="g2", description="rej", status="rejected", evidence_count=5))
    db.commit()
    assert mm.approved_for_goals(db, {"g1", "g2"}) == [("g1", "appr")]
    assert mm.approved_for_goals(db, set()) == []


def test_set_status_and_curation_is_sticky(db):
    for s in _students(db, 4):
        _note(db, s.id, "g1", "x")
    db.commit()
    mm.run(db, SINCE, NOW, client=_FakeClient(json.dumps([{"description": "X model", "count": 1}])))
    row = db.query(GoalMisconception).one()
    assert mm.set_status(db, row.id, "approved") is True
    # A later mining run refreshing this row must not touch the instructor's decision.
    mm.run(db, SINCE, NOW, client=_FakeClient(json.dumps([{"description": "x model", "count": 1}])))
    assert db.get(GoalMisconception, row.id).status == "approved"
    assert mm.set_status(db, 9999, "approved") is False
    with pytest.raises(ValueError):
        mm.set_status(db, row.id, "bogus")


def test_envelope_note_includes_library_only_when_approved(db):
    from tutor.agent.envelope import _misconception_note
    (st,) = _students(db, 1)
    db.add(GoalMisconception(goal_id="g1", description="Phases are the shadow of the Earth", status="approved"))
    db.add(GoalMisconception(goal_id="g1", description="not yet", status="candidate"))
    db.commit()
    note = _misconception_note(st.id, {"g1"}, {"g1": "Moon phases"}, db)
    assert "COMMON MISCONCEPTIONS" in note and "Phases are the shadow of the Earth" in note
    assert "not yet" not in note
    # With a per-student row too, both parts are present and the original text leads.
    _note(db, st.id, "g1", "student-specific wrong model")
    db.commit()
    note2 = _misconception_note(st.id, {"g1"}, {"g1": "Moon phases"}, db)
    assert note2.startswith("MISCONCEPTION on record") and "COMMON MISCONCEPTIONS" in note2


def test_grader_context_names_known_misconceptions():
    from tutor.agent import mastery
    goal = LearningGoal(id="g1", title="Moon phases", description="d", course="AST101", lecture_num=1)
    questions = {"q1": {"question": "a", "rubric": "r"}, "q2": {"question": "b", "rubric": "r"},
                 "code": {"question": "c", "rubric": "r", "is_conceptual": True}}
    captured = {}

    class _C:
        class messages:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)

                class _B:
                    type = "text"
                    text = '{"score": 3, "feedback": "ok"}'

                class _R:
                    content = [_B()]
                return _R()

    mastery._grade_with_api(goal, questions, "ans", client=_C(),
                            known_misconceptions=("Phases are the shadow of the Earth",))
    text = json.dumps(captured.get("messages", ""))
    assert "Known misconceptions for this goal" in text and "Phases are the shadow of the Earth" in text
