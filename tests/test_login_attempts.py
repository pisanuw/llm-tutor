"""The sign-in trail (tutor.auth.attempts) and the admin Logins tab that reads it.

2026-10-02: a student could not sign in by Google or by magic link and the
site had no record of it. Every step of a sign-in now leaves a row with the
host and the browser, the admin can read and filter them, the recorder never
breaks a login, and the rows leave with the student's data.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor.agent import state
from tutor.auth import attempts
from tutor.db.models import LoginAttempt, Student
from tutor.utctime import utcnow
from tests.helpers import make_session

SCANNER = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) URL-Defense"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) Safari/605.1.15"


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers import status
    from tutor.routers.admin import issue_admin_session

    status._reset_for_tests()
    db = make_session()
    ada = Student(id=2, email="ada@example.edu", name="Ada")
    db.add_all([Student(id=1, email="prof@example.edu", name="Prof"), ada])
    db.flush()
    t0 = utcnow() - datetime.timedelta(minutes=10)
    rows = [
        ("ada@example.edu", "magic", "link_sent", "tutor.example.edu", PHONE, None, None),
        ("ada@example.edu", "magic", "link_opened", "tutor.example.edu", SCANNER, None, None),
        ("ada@example.edu", "magic", "code_invalid", "tutor.example.edu", PHONE, None, "wrong code"),
        ("ada@example.edu", "magic", "ok", "tutor.example.edu", PHONE, 2, "/chat"),
        (None, "google", "started", "tutor-app.onrender.com", PHONE, None, None),
        ("bo@example.edu", "google", "denied", "tutor.example.edu", PHONE, None, None),
    ]
    for i, (email, method, outcome, host, ua, sid, detail) in enumerate(rows):
        db.add(LoginAttempt(email=email, method=method, outcome=outcome, host=host, user_agent=ua,
                            student_id=sid, detail=detail,
                            created_at=t0 + datetime.timedelta(seconds=i)))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set("admin_session", issue_admin_session())
    try:
        yield client, db
    finally:
        app.dependency_overrides.clear()
        db.close()
        config.get_config.cache_clear()


def test_the_logins_tab_shows_every_step_with_its_browser(admin):
    client, _ = admin
    page = client.get("/admin?tab=logins")
    assert page.status_code == 200
    body = page.text
    assert "Login attempts (6)" in body
    for outcome in ("link_sent", "link_opened", "code_invalid", "ok", "started", "denied"):
        assert outcome in body
    assert SCANNER[:57] in body and 'title="' + SCANNER in body     # the scanner is visible
    assert "tutor-app.onrender.com" in body                 # and the host
    assert "wrong code" in body
    # The 7-day counts above the trail.
    assert "code_invalid <span" in body.replace("\n", " ")


def test_the_trail_filters_by_address_fragment(admin):
    client, _ = admin
    page = client.get("/admin?tab=logins&student=BO")
    assert page.status_code == 200
    assert "Login attempts (1)" in page.text and "denied" in page.text
    assert "code_invalid</span>" not in page.text
    assert 'value="bo"' in page.text                                    # the filter box keeps it


def test_record_keeps_a_login_alive_when_the_insert_fails(caplog):
    db = MagicMock()
    db.commit.side_effect = RuntimeError("no such table: login_attempts")
    attempts.record(db, None, method="magic", outcome="ok", email="x@example.edu", student_id=1)
    db.rollback.assert_called_once()
    assert "login attempt not recorded" in caplog.text


def test_record_clips_and_lowercases_without_a_request():
    db = make_session()
    attempts.record(db, None, method="google", outcome="denied", email="  Stu@Example.EDU ",
                    detail="x" * 400)
    row = db.query(LoginAttempt).one()
    assert (row.email, row.host, row.user_agent) == ("stu@example.edu", None, None)
    assert len(row.detail or "") == 255
    db.close()


def test_tone_groups_outcomes_for_the_page():
    assert attempts.tone("ok") == "ok"
    assert {attempts.tone(o) for o in attempts.IN_PROGRESS} == {"progress"}
    assert {attempts.tone(o) for o in attempts.FAILED} == {"fail"}
    assert attempts.tone("something-new") == "fail"                     # unknown reads as a failure


def test_a_students_attempts_leave_with_their_data_by_id_and_by_address():
    db = make_session()
    db.add_all([Student(id=1, email="gone@example.edu", name="Gone"), Student(id=2, email="stays@example.edu", name="Stays")])
    db.flush()
    db.add_all([
        LoginAttempt(email="gone@example.edu", method="magic", outcome="link_sent"),   # before the row existed
        LoginAttempt(email="gone@example.edu", student_id=1, method="magic", outcome="ok"),
        LoginAttempt(email="stays@example.edu", student_id=2, method="google", outcome="ok"),
    ])
    db.commit()
    assert "login_attempts" in state.erasure_scope(1)
    assert state.ANONYMIZE_POLICY["login_attempts"] == "delete"
    counts = state.delete_student_data(db, 1)
    db.commit()
    assert counts["login_attempts"] == 2
    assert [a.email for a in db.query(LoginAttempt)] == ["stays@example.edu"]
    db.close()
