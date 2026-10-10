"""Admin Calibration tab renders (Engram integration, Phase 2)."""
from __future__ import annotations

import pytest

from tutor.agent import state
from tutor.db.models import LearningGoal, Student
from tests.helpers import make_session


@pytest.fixture
def db():
    session = make_session(
        Student(id=1, email="s@example.edu", name="S"),
        LearningGoal(id="AST101-L04-g1", title="Telescopes", description="", course="AST101", lecture_num=4),
    )
    yield session
    session.close()


@pytest.fixture
def admin_client(admin_client, db):
    """The shared admin client (tests/conftest.py) and its database."""
    return admin_client, db


def test_calibration_tab_empty_state(admin_client):
    client, _ = admin_client
    body = client.get("/admin?tab=calibration").text
    assert "Calibration" in body
    assert "No confidence-rated answers recorded yet" in body


def test_calibration_tab_reports_bands_and_high_conf_miss(admin_client):
    client, db = admin_client
    # One confident miss and one confident hit.
    state.record_turn_event(db, student_id=1, kind="review", goal_ids=["AST101-L04-g1"],
                            verdicts={"score": 1, "miscalibrated": True}, confidence=90)
    state.record_turn_event(db, student_id=1, kind="review", goal_ids=["AST101-L04-g1"],
                            verdicts={"score": 3}, confidence=90)
    db.commit()
    body = client.get("/admin?tab=calibration").text
    assert "high-confidence misses" in body
    assert "90%" in body  # the band row rendered


def test_calibration_tab_per_student_filter(admin_client):
    client, db = admin_client
    state.record_turn_event(db, student_id=1, kind="review", goal_ids=["AST101-L04-g1"],
                            verdicts={"score": 2}, confidence=70)
    db.commit()
    body = client.get("/admin?tab=calibration&student=s@example.edu").text
    assert "Student: s@example.edu" in body
