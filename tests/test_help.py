"""First-login UX, Phase 3: the public /help page, the chat help modal, and
the orientation links on the other pages."""

from __future__ import annotations

import pytest

from tutor.db.models import Student, StudentPath, Track
from tests.helpers import make_session


@pytest.fixture()
def db():
    session = make_session()
    session.add(Student(id=1, email="stu@example.edu", name="Stu Dent"))
    session.add(Track(id="AST101-lecture-order", course="AST101", title="Lecture Order",
                      description="", goal_ids_json="[]", graph_version=1))
    session.add(StudentPath(student_id=1, course="AST101", track_id="AST101-lecture-order"))
    session.commit()
    yield session
    session.close()


def test_help_page_is_public(app_client):
    r = app_client.get("/help")
    assert r.status_code == 200
    assert "How Astra works" in r.text
    assert "What it will not do" in r.text
    assert "Log in" in r.text          # no session: offer login, not chat
    assert 'aria-label="Main"' not in r.text


def test_help_page_offers_chat_when_logged_in(app_client, db, monkeypatch):
    """Signed in, the help page shows the shared site header (plan step 3.1)."""
    from tutor.db.models import Student
    from tutor.routers import onboarding

    stu = db.get(Student, 1)
    monkeypatch.setattr(onboarding, "_session_student", lambda request, db: stu)
    r = app_client.get("/help")
    assert 'aria-label="Main"' in r.text and 'href="/chat"' in r.text
    assert 'aria-current="page"' in r.text and "Log out" in r.text
    assert "Log in" not in r.text


def test_help_page_ignores_garbage_cookie(app_client):
    app_client.cookies.set("session_token", "not-a-jwt")
    r = app_client.get("/help")
    assert r.status_code == 200
    assert "Log in" in r.text


def test_chat_has_the_help_modal_and_button(student_client):
    r = student_client.get("/chat")
    assert r.status_code == 200
    assert 'id="help-modal"' in r.text
    assert "How Astra works" in r.text
    assert "openHelp()" in r.text


def test_login_page_links_to_help(app_client):
    r = app_client.get("/auth/login")
    assert r.status_code == 200
    assert 'href="/help"' in r.text
    # The consent notice itself is pinned word for word in test_consent_notice.py.


def test_other_pages_link_to_help(student_client):
    assert 'href="/help"' in student_client.get("/welcome").text
    assert 'href="/help"' in student_client.get("/lectures").text
    assert 'href="/help"' in student_client.get("/map").text
    picker = student_client.get("/track-picker").text
    assert 'href="/help"' in picker
    assert "Order of suggestions" in picker
    assert "Save this order and go to the chat" in picker
