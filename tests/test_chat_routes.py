"""Tests for the chat router additions: the first-login track gate,
the /chat/history reload endpoint, and the /path/current track chip feed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tutor.db.models import (
    Message,
    Student,
    StudentPath,
    Track,
)
from tests.helpers import make_session


@pytest.fixture()
def app_client():
    """TestClient with DB + auth overridden. The student starts with NO track
    so each test can choose whether to satisfy the first-login gate."""
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db

    session = make_session()

    s = Student(email="test@example.com", name="Test")
    session.add(s)
    session.flush()
    session.add(
        Track(
            id="AST101-lecture-order",
            course="AST101",
            title="Lecture order",
            description="Follow the lectures in order",
            goal_ids_json="[]",
            graph_version=1,
        )
    )
    session.flush()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s

    client = TestClient(app, raise_server_exceptions=True)
    yield client, s, session
    app.dependency_overrides.clear()
    session.close()


def _set_track(session, student_id, course="AST101", track_id="AST101-lecture-order"):
    session.add(StudentPath(student_id=student_id, course=course, track_id=track_id))
    session.flush()


# ---------------------------------------------------------------------------
# First-login track gate
# ---------------------------------------------------------------------------

def test_chat_redirects_to_welcome_when_no_track(app_client):
    """A student who has never chosen a course goes to /welcome (which creates
    the default-track path), not straight to the track picker."""
    client, _s, _db = app_client
    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/welcome"


def test_chat_renders_once_a_track_is_chosen(app_client):
    client, student, session = app_client
    _set_track(session, student.id)
    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert "Astra" in resp.text


# ---------------------------------------------------------------------------
# /chat/history (reload persistence)
# ---------------------------------------------------------------------------

def test_history_returns_course_messages_in_order(app_client):
    client, student, session = app_client
    session.add_all([
        Message(student_id=student.id, role="user", content="first", course="AST101"),
        Message(student_id=student.id, role="assistant", content="reply", course="AST101"),
        Message(student_id=student.id, role="user", content="other course", course="AST102"),
    ])
    session.flush()

    resp = client.get("/chat/history?course=AST101")
    assert resp.status_code == 200
    data = resp.json()
    assert [m["role"] for m in data] == ["user", "assistant"]
    assert [m["content"] for m in data] == ["first", "reply"]
    # AST102 message must not leak into the AST101 history
    assert all("other course" != m["content"] for m in data)


def test_history_empty_for_new_student(app_client):
    client, _s, _db = app_client
    resp = client.get("/chat/history?course=AST101")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# /path/current (header track chip)
# ---------------------------------------------------------------------------

def test_path_current_returns_chosen_track_title(app_client):
    client, student, session = app_client
    _set_track(session, student.id)
    resp = client.get("/path/current?course=AST101")
    assert resp.status_code == 200
    body = resp.json()
    assert body["track_id"] == "AST101-lecture-order"
    assert body["title"] == "Lecture order"


def test_path_current_falls_back_to_default_track(app_client):
    client, _s, _db = app_client  # no StudentPath set
    resp = client.get("/path/current?course=AST101")
    assert resp.status_code == 200
    assert resp.json()["track_id"] == "AST101-lecture-order"
