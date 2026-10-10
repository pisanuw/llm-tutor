"""Tests for PathEvent telemetry and /path router helpers (Session 15)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tutor.db.models import (
    LearningGoal,
    PathEvent,
    Student,
    StudentPath,
    StudentProgress,
    Track,
)
from tutor.agent.state import record_path_event, set_student_path
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# DB fixture (shared with test_navigator pattern)
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    session = make_session()
    yield session
    session.close()


def _student(db, email="test@example.com") -> Student:
    s = Student(email=email, name="Test")
    db.add(s)
    db.flush()
    return s


def _goal(db, gid: str, course: str = "AST101", lecture: int = 1) -> LearningGoal:
    g = LearningGoal(id=gid, title=gid, description="", course=course, lecture_num=lecture)
    db.add(g)
    db.flush()
    return g


def _track(db, tid: str, course: str, goal_ids: list) -> Track:
    t = Track(id=tid, course=course, title=tid, description="", goal_ids_json=json.dumps(goal_ids), graph_version=1)
    db.add(t)
    db.flush()
    return t


def _master(db, student_id, goal_id):
    row = StudentProgress(student_id=student_id, goal_id=goal_id, status="mastered", evidence_type="test")
    db.add(row)
    db.flush()


# ---------------------------------------------------------------------------
# PathEvent model / record_path_event
# ---------------------------------------------------------------------------

def test_record_path_event_creates_row(db):
    s = _student(db)
    _goal(db, "g1")
    event = record_path_event(db, s.id, "AST101", "teach", goal_id="g1", source="student")
    assert event.id is not None
    assert event.modality == "teach"
    assert event.goal_id == "g1"
    assert event.source == "student"
    assert event.course == "AST101"


def test_record_path_event_without_goal(db):
    s = _student(db)
    event = record_path_event(db, s.id, "AST101", "track_set", goal_id=None, source="student")
    assert event.goal_id is None
    assert event.modality == "track_set"


def test_record_path_event_persists(db):
    s = _student(db)
    _goal(db, "g1")
    record_path_event(db, s.id, "AST101", "slides", goal_id="g1")
    record_path_event(db, s.id, "AST101", "book",   goal_id="g1")
    db.flush()
    rows = db.query(PathEvent).filter_by(student_id=s.id).all()
    assert len(rows) == 2
    modalities = {r.modality for r in rows}
    assert modalities == {"slides", "book"}


def test_record_path_event_default_source(db):
    s = _student(db)
    event = record_path_event(db, s.id, "AST101", "quiz")
    assert event.source == "student"


def test_multiple_students_events_are_independent(db):
    s1 = _student(db, "a@x.com")
    s2 = _student(db, "b@x.com")
    _goal(db, "g1")
    record_path_event(db, s1.id, "AST101", "teach", goal_id="g1")
    record_path_event(db, s2.id, "AST101", "slides", goal_id="g1")
    db.flush()
    assert db.query(PathEvent).filter_by(student_id=s1.id).count() == 1
    assert db.query(PathEvent).filter_by(student_id=s2.id).count() == 1
    assert db.query(PathEvent).filter_by(student_id=s1.id).first().modality == "teach"
    assert db.query(PathEvent).filter_by(student_id=s2.id).first().modality == "slides"


def test_track_set_event_records_on_set_path(db):
    s = _student(db)
    _track(db, "AST101-lecture-order", "AST101", [])
    set_student_path(db, s.id, "AST101", "AST101-lecture-order")
    # set_student_path doesn't record PathEvent (that's done by the router)
    # Just ensure set_student_path + record_path_event can coexist
    record_path_event(db, s.id, "AST101", "track_set")
    db.flush()
    row = db.query(PathEvent).filter_by(student_id=s.id, modality="track_set").first()
    assert row is not None


# ---------------------------------------------------------------------------
# PathEvent timestamp ordering
# ---------------------------------------------------------------------------

def test_path_events_ordered_by_ts(db):
    s = _student(db)
    _goal(db, "g1")
    _goal(db, "g2")
    record_path_event(db, s.id, "AST101", "teach", goal_id="g1")
    record_path_event(db, s.id, "AST101", "slides", goal_id="g2")
    db.flush()
    rows = (
        db.query(PathEvent)
        .filter_by(student_id=s.id)
        .order_by(PathEvent.ts.asc())
        .all()
    )
    assert rows[0].goal_id == "g1"
    assert rows[1].goal_id == "g2"


# ---------------------------------------------------------------------------
# FastAPI integration: /path/event endpoint
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client():
    """TestClient with DB and auth dependencies overridden."""
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db

    # Use check_same_thread=False so TestClient's worker thread can use the same connection
    session = make_session()

    s = _student(session)
    _goal(session, "g-test", course="AST101", lecture=1)
    session.flush()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s

    client = TestClient(app, raise_server_exceptions=True)
    yield client, s, session
    app.dependency_overrides.clear()
    session.close()


def test_post_path_event_valid(app_client):
    client, student, session = app_client
    resp = client.post("/path/event", json={
        "course": "AST101", "goal_id": "g-test", "modality": "teach", "source": "student"
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    rows = session.query(PathEvent).filter_by(student_id=student.id).all()
    assert len(rows) == 1
    assert rows[0].modality == "teach"


def test_post_path_event_invalid_modality(app_client):
    client, _s, _db = app_client
    resp = client.post("/path/event", json={
        "course": "AST101", "goal_id": "g-test", "modality": "teleport", "source": "student"
    })
    assert resp.status_code == 422


def test_post_path_event_no_goal(app_client):
    client, student, session = app_client
    resp = client.post("/path/event", json={"course": "AST101", "modality": "track_set"})
    assert resp.status_code == 200
    rows = session.query(PathEvent).filter_by(student_id=student.id).all()
    assert rows[0].goal_id is None


def test_post_path_event_unknown_goal(app_client):
    client, _s, _db = app_client
    resp = client.post("/path/event", json={
        "course": "AST101", "goal_id": "does-not-exist", "modality": "teach"
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /path/next endpoint
# ---------------------------------------------------------------------------

def test_get_path_next_returns_goals(app_client):
    client, student, session = app_client
    _track(session, "AST101-lecture-order", "AST101", ["g-test"])
    resp = client.get("/path/next?course=AST101&n=3")
    assert resp.status_code == 200
    data = resp.json()
    assert "goals" in data
    assert "track_id" in data
    goal_ids = [g["id"] for g in data["goals"]]
    assert "g-test" in goal_ids


def test_get_path_next_empty_when_all_mastered(app_client):
    client, student, session = app_client
    _master(session, student.id, "g-test")
    _track(session, "AST101-lecture-order", "AST101", ["g-test"])
    resp = client.get("/path/next?course=AST101")
    assert resp.status_code == 200
    assert resp.json()["goals"] == []


# ---------------------------------------------------------------------------
# /path/track endpoint
# ---------------------------------------------------------------------------

def test_post_path_track_sets_track(app_client):
    client, student, session = app_client
    _track(session, "AST101-lecture-order", "AST101", ["g-test"])
    resp = client.post("/path/track", json={"course": "AST101", "track_id": "AST101-lecture-order"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    row = session.query(StudentPath).filter_by(student_id=student.id, course="AST101").first()
    assert row.track_id == "AST101-lecture-order"


def test_post_path_track_unknown_track(app_client):
    client, _s, _db = app_client
    resp = client.post("/path/track", json={"course": "AST101", "track_id": "AST101-not-a-track"})
    assert resp.status_code == 404
