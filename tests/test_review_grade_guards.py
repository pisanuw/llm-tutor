"""Plan step 1.6 (findings B2, C3): POST /review/grade guards and ledger.

Before: any goal could be graded at any time, with no attempt cap, no spend
cap, and a TurnEvent whose cost was 0. Four good answers set ``mastered``
(the prerequisite gate's key) without the exam's 2-of-3 rule, and a pending
exam's own questions could be previewed through it.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor.agent import state
from tutor.db.models import (
    GradedEvent,
    LearningGoal,
    MasteryAttempt,
    Student,
    StudentProgress,
    TurnEvent,
)
from tutor.utctime import utcnow
from tests.helpers import make_session

PROBE = {"id": "p1", "question": "What sets the Moon's phase?", "rubric": "names the Sun-Earth-Moon angle"}
GRADED = {"score": 3, "feedback": "Right.",
          "usage": {"in_tokens": 600, "out_tokens": 90, "cache_read_tokens": 0}}


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    monkeypatch.delenv("DAILY_REVIEW_LIMIT", raising=False)
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    config.get_config.cache_clear()

    from tutor.auth.deps import get_current_student, get_db
    from tutor.main import app

    db = make_session()
    s = Student(email="sam@example.edu", name="Sam")
    db.add(s)
    db.add(LearningGoal(id="G1", title="Moon phases", description="", course="AST101", lecture_num=2))
    db.add(LearningGoal(id="G2", title="Eclipse conditions", description="", course="AST101", lecture_num=2))
    db.flush()
    db.add(StudentProgress(student_id=s.id, goal_id="G1", status="unlocked", mastery_score=0.7,
                           fsrs_stability=2.0, fsrs_difficulty=5.0, fsrs_reps=1,
                           fsrs_due=utcnow() - datetime.timedelta(hours=2),
                           last_evidence_at=utcnow() - datetime.timedelta(days=3)))
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_student] = lambda: s
    with patch("tutor.agent.mastery._question_pool", return_value=[PROBE]):
        yield TestClient(app), s, db
    app.dependency_overrides.clear()
    db.close()
    config.get_config.cache_clear()


def _post(client, goal="G1"):
    return client.post("/review/grade", data={"goal_id": goal, "probe_id": "p1", "answer": "the key"})


def test_due_goal_is_graded_and_its_cost_ledgered(env):
    client, s, db = env
    with patch("tutor.agent.mastery.grade_probe", return_value=GRADED) as grader:
        r = _post(client)
    assert r.status_code == 200, r.text
    grader.assert_called_once()
    ev = db.query(TurnEvent).filter_by(kind="review").one()
    assert ev.cost_usd > 0
    assert ev.in_tokens == 600 and ev.out_tokens == 90 and ev.model


def test_looping_on_the_same_goal_is_refused(env):
    client, _, _ = env
    with patch("tutor.agent.mastery.grade_probe", return_value=GRADED) as grader:
        assert _post(client).status_code == 200
        for _ in range(3):
            r = _post(client)
            assert r.status_code == 409
            # Just graded: the retry says so, with the next review date
            # (R27), and grades nothing.
            assert r.json()["error"] == "already_checked" and r.json()["next_due"]
    grader.assert_called_once()


def test_goal_never_reached_or_not_yet_due_is_refused(env):
    client, s, db = env
    with patch("tutor.agent.mastery.grade_probe", return_value=GRADED) as grader:
        assert _post(client, goal="G2").status_code == 409     # no progress row
        db.add(StudentProgress(student_id=s.id, goal_id="G2", status="unlocked",
                               fsrs_due=utcnow() + datetime.timedelta(days=2)))
        db.commit()
        assert _post(client, goal="G2").status_code == 409     # due in two days
    grader.assert_not_called()


def test_goal_under_a_pending_exam_is_refused(env):
    client, s, db = env
    state.start_exam_attempt(db, s.id, "G1", "{}")
    db.commit()
    with patch("tutor.agent.mastery.grade_probe", return_value=GRADED) as grader:
        r = _post(client)
    assert r.status_code == 409 and r.json()["error"] == "exam_pending"
    grader.assert_not_called()
    assert db.query(MasteryAttempt).filter_by(status="pending").count() == 1


def test_daily_review_cap(env, monkeypatch):
    client, s, db = env
    monkeypatch.setenv("DAILY_REVIEW_LIMIT", "2")
    for _ in range(2):
        state.record_turn_event(db, student_id=s.id, kind="review", verdicts={"score": 2})
    db.commit()
    with patch("tutor.agent.mastery.grade_probe", return_value=GRADED) as grader:
        r = _post(client)
    assert r.status_code == 429 and r.json()["error"] == "limit"
    grader.assert_not_called()


def test_monthly_spend_cap_covers_review(env, monkeypatch):
    client, s, db = env
    monkeypatch.setenv("MONTHLY_COST_CAP_USD", "1.0")
    state.record_turn_event(db, student_id=s.id, kind="chat", cost_usd=1.5)
    db.commit()
    with patch("tutor.agent.mastery.grade_probe", return_value=GRADED) as grader:
        r = _post(client)
    assert r.status_code == 429
    grader.assert_not_called()


def test_unusable_grader_output_is_still_ledgered(env):
    from tutor.agent.mastery import GradingUnavailable

    client, _, db = env
    err = GradingUnavailable("unusable grader output",
                             usage={"in_tokens": 500, "out_tokens": 40, "cache_read_tokens": 0})
    with patch("tutor.agent.mastery.grade_probe", side_effect=err):
        r = _post(client)
    assert r.status_code == 503
    ev = db.query(TurnEvent).filter_by(kind="review").one()
    assert ev.cost_usd > 0
    # Fail closed: no evidence and no schedule change, so the student can resend.
    assert db.query(GradedEvent).count() == 0


def test_review_evidence_never_flips_status(env):
    client, s, db = env
    # Three perfect graded events already: under the old rule a fourth
    # success would have set "mastered" through the review loop alone.
    for _ in range(3):
        db.add(GradedEvent(student_id=s.id, goal_id="G1", success=1.0, source="exam",
                           ts=utcnow() - datetime.timedelta(days=3)))
    db.commit()
    with patch("tutor.agent.mastery.grade_probe", return_value=GRADED):
        assert _post(client).status_code == 200
    row = db.query(StudentProgress).filter_by(student_id=s.id, goal_id="G1").one()
    assert row.status == "unlocked"
    assert db.query(GradedEvent).filter_by(goal_id="G1", source="review").count() == 1
    assert row.fsrs_due > utcnow()          # the schedule still advanced


def test_grade_probe_reports_usage():
    from tutor.agent import mastery

    class _Usage:
        input_tokens = 321
        output_tokens = 45
        cache_read_input_tokens = 0

    class _Resp:
        content = [type("B", (), {"type": "text", "text": '{"score": 2, "feedback": "ok"}'})()]
        usage = _Usage()

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    goal = LearningGoal(id="G1", title="t", description="d", course="AST101", lecture_num=1)
    out = mastery.grade_probe(goal, "q", "r", "a", client=_Client())
    assert out["usage"] == {"in_tokens": 321, "out_tokens": 45, "cache_read_tokens": 0,
                            "cache_write_tokens": 0, "cache_write_1h_tokens": 0}
