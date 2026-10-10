"""Misconception library consumers (pedagogy item 12 follow-ups): error-detection
review probes built from approved distractors, db-backed probe lookup that
refuses non-approved rows, the review grader's known-misconception context,
and the admin curation tab/endpoint."""

from __future__ import annotations

import json

import pytest

from tutor.db.models import GoalMisconception, LearningGoal, Student
from tutor.routers import review
from tests.helpers import make_session


@pytest.fixture()
def db():
    s = make_session()
    s.add(LearningGoal(id="g1", title="Moon phases", description="", course="AST101", lecture_num=1))
    s.add(GoalMisconception(id=1, goal_id="g1", description="Phases are caused by the shadow of the Earth",
                            distractor="A crescent Moon is the part the shadow of the Earth leaves lit.", status="approved", evidence_count=5))
    s.add(GoalMisconception(id=2, goal_id="g1", description="candidate only",
                            distractor="The Moon makes its own light.", status="candidate", evidence_count=9))
    s.add(GoalMisconception(id=3, goal_id="g1", description="approved, no distractor",
                            distractor="", status="approved", evidence_count=7))
    s.commit()
    yield s
    s.close()


def _bank(goal_id):
    return {"id": "bank-1", "question": "Explain the phases of the Moon.", "rubric": "r"}


def test_probe_for_item_alternates_bank_and_error_detection(db):
    assert review.probe_for_item("g1", 0, db, _bank)["id"] == "bank-1"
    mc = review.probe_for_item("g1", 1, db, _bank)
    assert mc["id"] == "mc-1"
    assert "A crescent Moon is the part the shadow of the Earth leaves lit." in mc["question"] and "What is wrong" in mc["question"]
    assert "Phases are caused by the shadow of the Earth" in mc["rubric"]          # rubric carries the model
    assert review.probe_for_item("g1", 2, db, _bank)["id"] == "bank-1"


def test_probe_for_item_falls_back_to_bank_without_approved_distractors(db):
    db.query(GoalMisconception).filter_by(id=1).update({"status": "rejected"})
    db.commit()
    # Only a candidate (ignored) and an approved row without a distractor remain.
    assert review.probe_for_item("g1", 1, db, _bank)["id"] == "bank-1"


def test_lookup_probe_resolves_only_approved_library_rows(db):
    assert review._lookup_probe("g1", "mc-1", db)["id"] == "mc-1"
    assert review._lookup_probe("g1", "mc-2", db) is None        # candidate
    assert review._lookup_probe("g1", "mc-3", db) is None        # no distractor
    assert review._lookup_probe("g1", "mc-999", db) is None
    assert review._lookup_probe("g1", "mc-x", db) is None
    assert review._lookup_probe("other", "mc-1", db) is None    # wrong goal
    # After an instructor reverts approval, the probe can no longer be graded.
    db.query(GoalMisconception).filter_by(id=1).update({"status": "candidate"})
    db.commit()
    assert review._lookup_probe("g1", "mc-1", db) is None


def test_grade_probe_context_names_known_misconceptions():
    from tutor.agent import mastery
    goal = LearningGoal(id="g1", title="Moon phases", description="d", course="AST101", lecture_num=1)
    captured = {}

    class _C:
        class messages:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)

                class _B:
                    type = "text"
                    text = '{"score": 2, "feedback": "ok"}'

                class _R:
                    content = [_B()]
                return _R()

    mastery.grade_probe(goal, "q", "r", "ans", client=_C(),
                        known_misconceptions=("Phases are caused by the shadow of the Earth",))
    text = json.dumps(captured.get("messages", ""))
    assert "Known misconceptions for this goal" in text and "Phases are caused by the shadow of the Earth" in text
    captured.clear()
    mastery.grade_probe(goal, "q", "r", "ans", client=_C())
    assert "Known misconceptions" not in json.dumps(captured.get("messages", ""))


# ---------------------------------------------------------------------------
# Admin curation
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_client(admin_client, db):
    """The shared admin client (tests/conftest.py), its database, and a student."""
    db.add(Student(id=1, email="s@example.edu", name="S"))
    db.commit()
    return admin_client, db


def test_admin_tab_lists_library(admin_client):
    client, _db = admin_client
    r = client.get("/admin?tab=misconceptions")
    assert r.status_code == 200
    assert "Misconception library (3)" in r.text
    assert "Phases are caused by the shadow of the Earth" in r.text and "candidate only" in r.text
    assert "/admin/misconceptions/set" in r.text


def test_admin_set_status_and_reject_bad_values(admin_client):
    client, db = admin_client
    r = client.post("/admin/misconceptions/set", data={"row_id": 2, "status": "approved"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("tab=misconceptions")
    db.expire_all()
    assert db.get(GoalMisconception, 2).status == "approved"
    r = client.post("/admin/misconceptions/set", data={"row_id": 2, "status": "bogus"},
                    follow_redirects=False)
    assert r.status_code == 400


def test_admin_set_status_requires_admin(app_client, db):
    r = app_client.post("/admin/misconceptions/set", data={"row_id": 1, "status": "rejected"},
                        follow_redirects=False)
    assert r.status_code in (401, 403, 303) and db.get(GoalMisconception, 1).status == "approved"
