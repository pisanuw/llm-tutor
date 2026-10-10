"""First-login UX (Phase 1): the /welcome page,
the remembered current course, and the course-aware sidebar."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from tutor.agent.navigator import current_course
from tutor.db.models import (
    LearningGoal,
    PathEvent,
    Student,
    StudentPath,
    Track,
)
from tests.helpers import make_session


@pytest.fixture()
def app_client():
    """TestClient with DB + auth overridden. Both courses have their default
    lecture-order track and one goal each; the student has chosen nothing."""
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db

    # StaticPool: the routes under test commit, which would otherwise return
    # the connection and hand the next request a fresh, empty :memory: DB.
    session = make_session()

    s = Student(email="newbie@example.edu", name="Ada Lovelace")
    session.add(s)
    session.flush()
    session.add_all([
        LearningGoal(id="AST101-L01-celestial-sphere", title="Describe the celestial sphere model",
                     description="", course="AST101", lecture_num=1),
        LearningGoal(id="AST102-L01-parallax-distance", title="Compute a distance from parallax",
                     description="", course="AST102", lecture_num=1),
        Track(id="AST101-lecture-order", course="AST101", title="Lecture Order",
              description="", goal_ids_json='["AST101-L01-celestial-sphere"]', graph_version=1),
        Track(id="AST102-lecture-order", course="AST102", title="Lecture Order",
              description="", goal_ids_json='["AST102-L01-parallax-distance"]', graph_version=1),
    ])
    session.flush()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s
    client = TestClient(app, raise_server_exceptions=True)
    yield client, s, session
    app.dependency_overrides.clear()
    session.close()


# ---------------------------------------------------------------------------
# /welcome
# ---------------------------------------------------------------------------

def test_welcome_page_greets_and_offers_both_courses(app_client):
    client, _s, _db = app_client
    resp = client.get("/welcome")
    assert resp.status_code == 200
    assert "Welcome, Ada." in resp.text
    assert "Start with AST 101" in resp.text
    assert "Start with AST 102" in resp.text
    assert "How this works" in resp.text


def test_welcome_post_creates_default_path_and_goes_to_chat(app_client):
    client, student, session = app_client
    resp = client.post("/welcome", data={"course": "AST102"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"

    path = session.query(StudentPath).filter_by(student_id=student.id).one()
    assert path.course == "AST102"
    assert path.track_id == "AST102-lecture-order"
    ev = session.query(PathEvent).filter_by(student_id=student.id).one()
    assert (ev.modality, ev.source, ev.course) == ("track_set", "system", "AST102")


def test_welcome_post_rejects_unknown_course(app_client):
    client, student, session = app_client
    resp = client.post("/welcome", data={"course": "101"})
    assert resp.status_code == 400
    assert "Pick one of the courses to continue." in resp.text
    assert session.query(StudentPath).filter_by(student_id=student.id).count() == 0


def test_welcome_marks_current_course_on_a_return_visit(app_client):
    client, _s, _db = app_client
    client.post("/welcome", data={"course": "AST102"}, follow_redirects=False)
    resp = client.get("/welcome")
    assert "Continue with AST 102" in resp.text
    assert "Start with AST 101" in resp.text


# ---------------------------------------------------------------------------
# The remembered course
# ---------------------------------------------------------------------------

def test_current_course_is_none_before_any_choice(app_client):
    _c, student, session = app_client
    assert current_course(student.id, session) is None


def test_chat_opens_on_the_chosen_course(app_client):
    client, _s, _db = app_client
    client.post("/welcome", data={"course": "AST102"}, follow_redirects=False)
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert 'data-course="AST102"' in resp.text
    assert 'window.TUTOR_COURSE = "AST102";' in resp.text     # read by /static/chat.js


def test_chat_query_param_overrides_the_current_course(app_client):
    client, _s, _db = app_client
    client.post("/welcome", data={"course": "AST102"}, follow_redirects=False)
    resp = client.get("/chat?course=AST101")
    assert 'data-course="AST101"' in resp.text
    # An invalid value falls back to the current course, never 500s.
    resp = client.get("/chat?course=999")
    assert 'data-course="AST102"' in resp.text


def test_path_course_switch_becomes_current(app_client):
    client, student, session = app_client
    r = client.post("/path/course", json={"course": "AST102"})
    assert r.status_code == 200 and r.json()["created"] is True
    assert current_course(student.id, session) == "AST102"

    # Age the AST102 path so the next touch is unambiguously newer.
    row = session.query(StudentPath).filter_by(student_id=student.id, course="AST102").one()
    row.updated_at = row.updated_at - datetime.timedelta(minutes=5)
    session.flush()

    r = client.post("/path/course", json={"course": "AST101"})
    assert r.json()["created"] is True
    assert current_course(student.id, session) == "AST101"

    # Switching back touches the existing AST102 row (no duplicate, keeps track).
    row_ast101 = session.query(StudentPath).filter_by(student_id=student.id, course="AST101").one()
    row_ast101.updated_at = row_ast101.updated_at - datetime.timedelta(minutes=5)
    session.flush()
    r = client.post("/path/course", json={"course": "AST102"})
    assert r.json()["created"] is False
    assert current_course(student.id, session) == "AST102"
    assert session.query(StudentPath).filter_by(student_id=student.id).count() == 2


def test_path_course_rejects_unknown_course(app_client):
    client, _s, _db = app_client
    assert client.post("/path/course", json={"course": "101"}).status_code == 422


def test_track_picker_defaults_to_current_course(app_client):
    client, _s, _db = app_client
    client.post("/welcome", data={"course": "AST102"}, follow_redirects=False)
    resp = client.get("/track-picker")
    assert resp.status_code == 200
    assert 'let activeCourse = "AST102";' in resp.text


# ---------------------------------------------------------------------------
# Course-aware sidebar
# ---------------------------------------------------------------------------

def test_progress_whats_next_follows_the_course(app_client):
    client, _s, _db = app_client
    client.post("/welcome", data={"course": "AST102"}, follow_redirects=False)

    resp = client.get("/chat/progress?course=AST102")
    assert resp.status_code == 200
    assert "Compute a distance from parallax" in resp.text
    assert "Describe the celestial sphere model" not in resp.text
    assert "/track-picker?course=AST102" in resp.text

    resp = client.get("/chat/progress?course=AST101")
    assert "Describe the celestial sphere model" in resp.text
    assert "Compute a distance from parallax" not in resp.text


def test_progress_defaults_to_the_current_course(app_client):
    client, _s, _db = app_client
    client.post("/welcome", data={"course": "AST102"}, follow_redirects=False)
    resp = client.get("/chat/progress")
    assert "Compute a distance from parallax" in resp.text
