"""Where /admin sends someone who is not an admin (2026-10-02).

An instructor followed a link to the admin Access tab, signed in with Google,
then with a magic link, and both times landed back on a 403 page whose one
link was "Sign in". They reported that neither login worked. The page is
gone: a signed-in instructor goes to their dashboard, a signed-in student to
the chat, and a visitor to the login page with a way back to /admin.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor.db.models import Student, WhitelistEntry
from tests.helpers import login_cookie, make_session


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-denial-test")
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session()
    db.add_all([
        Student(id=1, email="prof@example.edu", name="Prof"),
        Student(id=2, email="rivera@example.edu", name="Dr. Rivera", is_instructor=True),
        Student(id=3, email="stu@example.edu", name="Stu"),
        WhitelistEntry(email="@example.edu", added_by="test"),   # a session only counts while allowed
    ])
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, base_url="http://localhost")
    try:
        yield client, db
    finally:
        app.dependency_overrides.clear()
        db.close()
        config.get_config.cache_clear()


def _bounce(client, path="/admin?tab=access"):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    return resp.headers["location"]


def test_a_visitor_is_sent_to_sign_in_with_a_way_back(setup):
    client, _ = setup
    # The tab rides along (review step 0.4, F17: a flag's link used to land on Today).
    assert _bounce(client) == "/auth/login?next=/admin%3Ftab%3Daccess"
    assert _bounce(client, "/admin") == "/auth/login?next=/admin"


def test_a_signed_in_instructor_lands_on_their_dashboard(setup):
    client, db = setup
    client.cookies.set("session_token", login_cookie(db, 2, method="google"))
    assert _bounce(client) == "/instructor"
    assert "Access denied" not in client.get("/admin").text       # the loop is gone


def test_a_signed_in_student_lands_on_the_chat(setup):
    client, db = setup
    client.cookies.set("session_token", login_cookie(db, 3))
    assert _bounce(client) == "/chat"


def test_an_admin_still_gets_the_dashboard(setup):
    client, db = setup
    client.cookies.set("session_token", login_cookie(db, 1, method="google"))
    assert client.get("/admin", follow_redirects=False).status_code == 200
