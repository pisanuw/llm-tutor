"""Thumbs up/down on tutor replies.

Covers:
- POST /chat/feedback: records a rating for a recent reply, upserts on
  re-click, keeps the reason from the follow-up prompt, clears it on 👍,
  404s for text that is not a recent tutor reply, 422s for a bad rating
- the admin Conversations tab anchors bubbles by message id and shows the
  flag + reason inline; exports carry the feedback too
- tutor.feedback_report: window query, deep link, Markdown + HTML bodies
- scripts.feedback_report: prints "0 thumbs-down" and sends nothing on a
  quiet day; renders the report on a flagged day
"""
from __future__ import annotations

import datetime
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import tutor.config as config
from tutor.db.models import Base, Message, MessageFeedback, Student
from tutor.utctime import utcnow
from tests.helpers import make_session

REPLY = "Start with where the Sun is.\n\nThe lit half always faces it, so we see a changing share."


@pytest.fixture()
def student_client(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()

    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db

    session = make_session()
    s = Student(email="sam@example.edu", name="Sam")
    session.add(s)
    session.flush()
    t0 = datetime.datetime(2026, 9, 3, 15, 0, 0)
    session.add(Message(student_id=s.id, role="user", content="Why does the Moon have phases?",
                        course="AST101", lecture_num=4, created_at=t0))
    session.add(Message(student_id=s.id, role="assistant", content=REPLY,
                        course="AST101", lecture_num=4, created_at=t0 + datetime.timedelta(seconds=5)))
    session.commit()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s
    client = TestClient(app)
    yield client, s, session
    app.dependency_overrides.clear()
    session.close()
    config.get_config.cache_clear()


def _reply_id(db) -> int:
    return db.query(Message).filter_by(role="assistant").one().id


# ---------------------------------------------------------------------------
# POST /chat/feedback
# ---------------------------------------------------------------------------

def test_thumbs_up_records_rating(student_client):
    client, s, db = student_client
    # Whitespace differences must not break the match (markdown re-rendering).
    r = client.post("/chat/feedback", data={"text": " ".join(REPLY.split()), "rating": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["rating"] == 1 and body["has_comment"] is False
    fb = db.query(MessageFeedback).one()
    assert fb.message_id == _reply_id(db) and fb.student_id == s.id and fb.comment is None


def test_thumbs_down_then_reason_updates_same_row(student_client):
    client, _, db = student_client
    r = client.post("/chat/feedback", data={"text": REPLY, "rating": "-1"})
    assert r.status_code == 200 and r.json()["rating"] == -1
    r = client.post("/chat/feedback", data={"text": REPLY, "rating": "-1",
                                            "comment": "It never mentioned duplicates."})
    assert r.status_code == 200 and r.json()["has_comment"] is True
    rows = db.query(MessageFeedback).all()
    assert len(rows) == 1
    assert rows[0].rating == -1 and rows[0].comment == "It never mentioned duplicates."


def test_thumbs_up_after_down_clears_reason(student_client):
    client, _, db = student_client
    client.post("/chat/feedback", data={"text": REPLY, "rating": "-1", "comment": "wrong"})
    r = client.post("/chat/feedback", data={"text": REPLY, "rating": "1"})
    assert r.status_code == 200
    fb = db.query(MessageFeedback).one()
    assert fb.rating == 1 and fb.comment is None


def test_feedback_rejects_unknown_text_and_bad_rating(student_client):
    client, _, db = student_client
    r = client.post("/chat/feedback", data={"text": "Not something the tutor said", "rating": "-1"})
    assert r.status_code == 404
    r = client.post("/chat/feedback", data={"text": REPLY, "rating": "5"})
    assert r.status_code == 422
    r = client.post("/chat/feedback", data={"text": REPLY, "rating": "down"})
    assert r.status_code == 422
    assert db.query(MessageFeedback).count() == 0


def test_feedback_comment_is_truncated(student_client):
    client, _, db = student_client
    client.post("/chat/feedback", data={"text": REPLY, "rating": "-1", "comment": "x" * 5000})
    assert len(db.query(MessageFeedback).one().comment) == 2000


def test_chat_page_ships_feedback_controls(student_client):
    """The controls are built client-side; make sure the JS and the POST target ship."""
    client, s, db = student_client
    from tutor.db.models import StudentPath, Track
    db.add(Track(id="AST101-lecture-order", course="AST101", title="Lecture order",
                 description="", goal_ids_json="[]", graph_version=1))
    db.add(StudentPath(student_id=s.id, course="AST101", track_id="AST101-lecture-order"))
    db.commit()
    r = client.get("/chat?course=AST101")
    assert r.status_code == 200
    # The chat's script is a static file since plan step 5.5; the page loads it.
    src = re.search(r'<script src="(/static/chat\.js\?v=[0-9a-f]+)"></script>', r.text).group(1)
    js = client.get(src)
    assert js.status_code == 200
    assert "buildFeedbackControls" in js.text
    assert "/chat/feedback" in js.text
    assert "What was wrong with this response?" in js.text


# ---------------------------------------------------------------------------
# Admin surfaces
# ---------------------------------------------------------------------------

def _flag(client, comment="Skipped the full Moon."):
    client.post("/chat/feedback", data={"text": REPLY, "rating": "-1", "comment": comment})


def test_admin_transcript_anchors_and_flags(student_client):
    client, _, db = student_client
    _flag(client)
    mid = _reply_id(db)
    from tutor.routers.admin import issue_admin_session
    client.cookies.set("admin_session", issue_admin_session())
    r = client.get("/admin?tab=conversations&student=sam@example.edu")
    assert r.status_code == 200
    assert f'id="msg-{mid}"' in r.text
    assert "👎 Student flagged this" in r.text
    assert "Skipped the full Moon." in r.text
    assert "ring-red-300" in r.text


def test_export_carries_feedback(student_client):
    client, _, db = student_client
    _flag(client)
    from tutor.routers.admin import issue_admin_session
    client.cookies.set("admin_session", issue_admin_session())
    r = client.get("/admin/conversations/export?student=sam@example.edu&format=json")
    msgs = r.json()["messages"]
    assert msgs[0]["feedback"] is None
    assert msgs[1]["feedback"] == {"rating": -1, "comment": "Skipped the full Moon."}
    r = client.get("/admin/conversations/export?student=sam@example.edu")
    assert "_[student feedback: 👎 Skipped the full Moon.]_" in r.text


# ---------------------------------------------------------------------------
# Report rendering and the emailer
# ---------------------------------------------------------------------------

def test_report_query_and_render(student_client):
    from tutor.feedback_report import (
        conversation_link, render_report_html, render_report_markdown, thumbs_down_in_window,
    )
    client, _, db = student_client
    _flag(client)
    mid = _reply_id(db)
    now = utcnow()
    flags = thumbs_down_in_window(db, now - datetime.timedelta(hours=1), now + datetime.timedelta(hours=1))
    assert len(flags) == 1
    f = flags[0]
    assert f.message_id == mid and f.student_email == "sam@example.edu"
    assert f.comment == "Skipped the full Moon."
    assert f.question_excerpt == "Why does the Moon have phases?"
    assert f.reply_excerpt.startswith("Start with where the Sun is")

    link = conversation_link("https://tutor.example.edu/", flags[0].feedback_id)
    assert link == f"https://tutor.example.edu/feedback/{flags[0].feedback_id}"

    md = render_report_markdown(flags, "2026-09-03", "https://tutor.example.edu")
    assert md.startswith("# Astra thumbs-down report: 2026-09-03")
    assert "1 tutor reply flagged by 1 student(s)" in md
    assert link in md and "What was wrong: Skipped the full Moon." in md
    html = render_report_html(flags, "2026-09-03", "https://tutor.example.edu")
    # href is HTML-escaped (& -> &amp;), as a mail client expects.
    assert f"href='{link.replace('&', '&amp;')}'" in html
    assert "Skipped the full Moon." in html

    # Outside the window, or with no flags, there is nothing to report.
    assert thumbs_down_in_window(db, now - datetime.timedelta(days=3), now - datetime.timedelta(days=2)) == []
    assert render_report_markdown([], "2026-09-03", "x") == ""
    assert render_report_html([], "2026-09-03", "x") == ""


def test_thumbs_up_is_not_reported(student_client):
    from tutor.feedback_report import thumbs_down_in_window
    client, _, db = student_client
    client.post("/chat/feedback", data={"text": REPLY, "rating": "1"})
    now = utcnow()
    assert thumbs_down_in_window(db, now - datetime.timedelta(hours=1), now + datetime.timedelta(hours=1)) == []


def test_emailer_cli_quiet_day_and_flagged_day(tmp_path, monkeypatch, capsys):
    import sqlalchemy
    from scripts.feedback_report import main

    # get_engine() treats DATABASE_URL as Postgres; point the SQLite fallback at
    # a temp file instead (TUTOR_DB is resolved against the repo root, so pass
    # an absolute path).
    db_path = tmp_path / "fb.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TUTOR_DB", str(db_path))
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    config.get_config.cache_clear()
    engine = sqlalchemy.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    s = Student(email="sam@example.edu", name="Sam")
    db.add(s)
    db.flush()
    m = Message(student_id=s.id, role="assistant", content=REPLY, course="AST102", lecture_num=2)
    db.add(m)
    db.flush()

    # Quiet day: exit 0, one status line, no report body.
    assert main(["--hours", "24", "--no-email"]) == 0
    out = capsys.readouterr().out
    assert "0 thumbs-down" in out and "thumbs-down report" not in out

    mid = m.id
    db.add(MessageFeedback(message_id=mid, student_id=s.id, rating=-1, comment="Too long."))
    db.commit()
    db.close()
    engine.dispose()

    assert main(["--hours", "24", "--no-email", "--base-url", "https://tutor.example.edu"]) == 0
    out = capsys.readouterr().out
    assert "1 thumbs-down" in out
    assert "# Astra thumbs-down report" in out
    assert "https://tutor.example.edu/feedback/" in out and f"#msg-{mid}" not in out   # the one link (step 0.4)
    assert "What was wrong: Too long." in out
    config.get_config.cache_clear()
