"""Admin observability surfaces: Logins, Activity, Conversations tabs + the
login columns on the Students tab.
"""
from __future__ import annotations

import datetime

import pytest

from tutor.db.models import Message, Session as SessionModel, Student, TurnEvent
from tutor.utctime import utcnow
from tests.helpers import make_session


@pytest.fixture
def db():
    """No students: each test adds the ones it needs."""
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def admin_client(admin_client, db):
    """The shared admin client (tests/conftest.py) and its database."""
    return admin_client, db


def _now():
    return utcnow()


def test_students_tab_shows_login_columns(admin_client):
    client, db = admin_client
    db.add(Student(email="s@example.edu", name="S", login_count=4, last_login=_now()))
    db.commit()
    body = client.get("/admin?tab=students").text
    assert "Logins" in body and "Last Login" in body
    assert "s@example.edu" in body and "4" in body


def test_logins_tab_lists_summary_and_feed(admin_client):
    client, db = admin_client
    s = Student(email="lg@example.edu", name="LG", login_count=2, last_login=_now())
    db.add(s)
    db.flush()
    db.add(SessionModel(student_id=s.id, jwt_jti="jti-1", method="google",
                        expires_at=_now() + datetime.timedelta(days=1)))
    db.add(SessionModel(student_id=s.id, jwt_jti="jti-2", method="magic",
                        expires_at=_now() + datetime.timedelta(days=1)))
    db.commit()

    body = client.get("/admin?tab=logins").text
    assert "Per-student logins" in body and "Recent logins" in body
    assert "lg@example.edu" in body
    assert "google" in body and "magic" in body


def test_activity_tab_buckets_by_day(admin_client):
    client, db = admin_client
    s = Student(email="a@example.edu", name="A")
    db.add(s)
    db.flush()
    db.add(Message(student_id=s.id, role="user", content="hi", course="AST101"))
    db.add(TurnEvent(student_id=s.id, kind="chat", cost_usd=0.0123))
    db.add(SessionModel(student_id=s.id, jwt_jti="j", method="google",
                        expires_at=_now() + datetime.timedelta(days=1)))
    db.commit()

    body = client.get("/admin?tab=activity").text
    assert "Daily activity" in body
    # today's bucket (UTC, matching the route's utcnow-based bucketing):
    # 1 active student, 1 message, 1 chat turn, the cost.
    assert utcnow().date().isoformat() in body
    assert "0.0123" in body


def test_conversations_tab_requires_student_then_shows_transcript(admin_client):
    client, db = admin_client
    s = Student(email="cv@example.edu", name="CV")
    db.add(s)
    db.flush()
    db.add(Message(student_id=s.id, role="user", content="why does my code print nothing", course="AST101"))
    db.add(Message(student_id=s.id, role="assistant", content="let us trace the loop", course="AST101"))
    db.commit()

    # No student selected -> the list of recent conversations, no transcript.
    empty = client.get("/admin?tab=conversations").text
    assert "Recent conversations" in empty and "let us trace the loop" not in empty

    body = client.get("/admin?tab=conversations&student=cv@example.edu").text
    assert "why does my code print nothing" in body
    assert "let us trace the loop" in body
    assert "cv@example.edu" in body


def test_conversations_unknown_student_is_clean(admin_client):
    client, db = admin_client
    r = client.get("/admin?tab=conversations&student=ghost@example.edu")
    assert r.status_code == 200  # no crash, just no transcript
