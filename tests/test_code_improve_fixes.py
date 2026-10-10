"""Regression tests for the code-improve order-of-work fixes.

Each test pins a specific defect the sweep found and this branch fixed, so a
later refactor cannot silently reintroduce it. Pure / SQLite-only, network-free.
"""

from __future__ import annotations

import pytest

from tutor.agent import mastery
from tutor.agent.llm_response import strip_code_fence, text_from
from tutor.db.models import LearningGoal, MasteryAttempt, Student, Track, safe_json_loads
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# llm_response: tolerate empty / non-text-first model responses (item 2)
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, text=None, type="text"):
        if text is not None:
            self.text = text
        self.type = type


class _Resp:
    def __init__(self, content):
        self.content = content


def test_text_from_empty_content_returns_empty():
    # content=[] would make resp.content[0].text raise (the old fail-open path).
    assert text_from(_Resp([])) == ""
    assert text_from(_Resp(None)) == ""


def test_text_from_recovers_text_after_a_non_text_block():
    # A leading tool/other block must not hide the text in a later block.
    resp = _Resp([_Block(type="tool_use"), _Block(text='{"verdict":"revise"}')])
    assert text_from(resp) == '{"verdict":"revise"}'


def test_text_from_concatenates_all_text_blocks():
    assert text_from(_Resp([_Block(text="a"), _Block(text="b")])) == "ab"


@pytest.mark.parametrize("raw,expected", [
    ('```json\n{"x":1}\n```', '{"x":1}'),
    ('```\n{"x":1}\n```', '{"x":1}'),
    ('  {"x":1}  ', '{"x":1}'),
])
def test_strip_code_fence(raw, expected):
    assert strip_code_fence(raw) == expected


# ---------------------------------------------------------------------------
# safe_json_loads: a malformed column degrades to default, not a 500 (item 6)
# ---------------------------------------------------------------------------

def test_safe_json_loads_tolerates_bad_input():
    assert safe_json_loads(None) is None
    assert safe_json_loads("") is None
    assert safe_json_loads("not json", default=[]) == []
    assert safe_json_loads('{"a": 1}') == {"a": 1}


def test_model_anchor_helpers_never_raise():
    g = LearningGoal(
        id="G1", title="t", description="", course="AST101", lecture_num=1,
        slide_anchor="{bad json", book_anchor='{"chapter":"3"}',
    )
    assert g.slide_anchor_data() is None          # malformed -> None, not raise
    assert g.book_anchor_data() == {"chapter": "3"}
    assert Track(id="t", course="AST101", title="T", goal_ids_json="nope").goal_ids() == []


# ---------------------------------------------------------------------------
# mastery: start_exam is idempotent for a pending exam (item 4a)
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="a@example.edu", name="A"))
    s.add(LearningGoal(id="G1", title="Telescopes", description="", course="AST101", lecture_num=4))
    s.commit()
    yield s
    s.close()


_BANK = {"G1": {"questions": [
    {"id": f"c{i}", "kind": "concept", "question": f"concept-{i}?", "rubric": "r"} for i in range(6)
] + [{"id": "k0", "kind": "code", "question": "code-0?", "rubric": "r", "starter": ""}]}}


def test_start_exam_does_not_create_a_second_pending(db, monkeypatch):
    monkeypatch.setattr(mastery, "_load_exam_bank", lambda: _BANK)
    goal = db.get(LearningGoal, "G1")
    first = mastery.start_exam(1, goal, db)
    second = mastery.start_exam(1, goal, db)   # second gated request, same goal
    assert first is not None and second is not None
    assert second.id == first.id               # resumed, not duplicated
    pending = db.query(MasteryAttempt).filter_by(
        student_id=1, goal_id="G1", status="pending"
    ).count()
    assert pending == 1


def test_question_texts_handles_both_shapes():
    triple = {"q1": {"question": "a"}, "q2": {"question": "b"},
              "code": {"question": "c"}}
    pool = {"questions": [{"question": "x"}, {"question": "y"}]}
    assert mastery._question_texts(triple) == {"a", "b", "c"}
    assert mastery._question_texts(pool) == {"x", "y"}
    assert mastery._question_texts({}) == set()
