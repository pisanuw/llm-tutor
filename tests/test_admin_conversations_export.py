"""Admin panel: Export Conversation.

Covers:
- the Conversations tab shows export links only once a student is selected
- GET /admin/conversations/export requires admin auth
- unknown student -> 404
- Markdown export: every message, oldest first, role labels, tool notes,
  attachment filename
- JSON export: full metadata, parsed tool notes
- ``course`` narrows the export to one course
"""
from __future__ import annotations

import datetime
import json

import pytest

from tutor.db.models import Message, Student
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


def _seed(db) -> Student:
    s = Student(email="Sam.Student@example.edu", name="Sam")
    db.add(s)
    db.flush()
    t0 = datetime.datetime(2026, 9, 1, 14, 0, 0)
    rows = [
        Message(student_id=s.id, role="user", content="Why does the Moon have phases?",
                course="AST101", lecture_num=2, created_at=t0),
        Message(student_id=s.id, role="assistant", content="Half of the Moon is always lit by the Sun.",
                course="AST101", lecture_num=2, slide_num=7,
                tool_summary=json.dumps([{"tool": "find_topic", "query": "moon phases",
                                          "top_source": "lecture-02", "slide": 7}]),
                created_at=t0 + datetime.timedelta(seconds=30)),
        Message(student_id=s.id, role="user", content="What is parallax?",
                course="AST102", lecture_num=1, created_at=t0 + datetime.timedelta(days=1)),
        Message(student_id=s.id, role="assistant", content="A near star's shift against far ones.",
                course="AST102", lecture_num=1, created_at=t0 + datetime.timedelta(days=1, seconds=20)),
    ]
    # Insert out of order to prove the export sorts by created_at.
    for m in reversed(rows):
        db.add(m)
    db.commit()
    return s


def test_export_links_appear_only_with_selected_student(admin_client):
    client, db = admin_client
    _seed(db)
    r = client.get("/admin?tab=conversations")
    assert r.status_code == 200
    assert "/admin/conversations/export" not in r.text

    r = client.get("/admin?tab=conversations&student=sam.student@example.edu")
    assert r.status_code == 200
    assert "/admin/conversations/export?student=Sam.Student%40example.edu&amp;format=md" in r.text
    assert "format=json" in r.text


def test_export_requires_admin(admin_client):
    client, db = admin_client
    _seed(db)
    client.cookies.clear()
    r = client.get("/admin/conversations/export?student=sam.student@example.edu")
    assert r.status_code == 403


def test_export_unknown_student_404(admin_client):
    client, _ = admin_client
    r = client.get("/admin/conversations/export?student=nobody@example.edu")
    assert r.status_code == 404


def test_export_markdown_full_transcript(admin_client):
    client, db = admin_client
    _seed(db)
    r = client.get("/admin/conversations/export?student=SAM.STUDENT@example.edu")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="tutor-conversation-Sam.Student-' in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.md"')

    body = r.text
    assert body.startswith("# Astra conversation: Sam.Student@example.edu")
    assert "4 messages" in body and "courses AST101, AST102" in body
    # Oldest first, role labels, lecture/slide metadata, tool notes.
    i_phases = body.index("Why does the Moon have phases?")
    i_parallax = body.index("What is parallax?")
    assert i_phases < i_parallax
    assert "**Student · AST101 · L02 · 2026-09-01 14:00 UTC**" in body
    assert "**Tutor · AST101 · L02 slide 7 · 2026-09-01 14:00 UTC**" in body
    assert "_[tools used: find_topic 'moon phases' -> lecture-02 (showed slide 7)]_" in body


def test_export_json_full_metadata(admin_client):
    client, db = admin_client
    _seed(db)
    r = client.get("/admin/conversations/export?student=sam.student@example.edu&format=json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"].endswith('.json"')

    data = r.json()
    assert data["student"] == "Sam.Student@example.edu"
    assert data["name"] == "Sam"
    assert data["course"] is None
    assert data["message_count"] == 4
    assert [m["role"] for m in data["messages"]] == ["user", "assistant", "user", "assistant"]
    first, second = data["messages"][0], data["messages"][1]
    assert first["content"] == "Why does the Moon have phases?"
    assert first["created_at"] == "2026-09-01T14:00:00Z"
    assert first["tools"] is None
    assert second["slide_num"] == 7
    assert second["tools"] == [{"tool": "find_topic", "query": "moon phases",
                                "top_source": "lecture-02", "slide": 7}]


def test_export_course_filter(admin_client):
    client, db = admin_client
    _seed(db)
    r = client.get("/admin/conversations/export?student=sam.student@example.edu&format=json&course=AST102")
    assert r.status_code == 200
    data = r.json()
    assert data["course"] == "AST102"
    assert data["message_count"] == 2
    assert all(m["course"] == "AST102" for m in data["messages"])

    r = client.get("/admin/conversations/export?student=sam.student@example.edu&course=999")
    assert r.status_code == 422  # not a course in courses.yaml

    r = client.get("/admin/conversations/export?student=sam.student@example.edu&format=pdf")
    assert r.status_code == 422
