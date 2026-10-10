"""Structured activity logging + login tracking.

The activity() event stream is the tailable mirror of the DB record used for
later analysis of student activity, conversations, and supervisor behavior.
Login tracking bumps Student.last_login/login_count and stamps the auth method
on the sessions row (the per-login audit trail).
"""
from __future__ import annotations

import json
import logging

import pytest

import tutor.config as config
from tutor import logging_config
from tutor.db.models import Session as SessionModel, Student
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# activity() structured event stream
# ---------------------------------------------------------------------------

class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def capture_activity():
    log = logging.getLogger("tutor.activity")
    log.setLevel(logging.INFO)
    h = _Collect()
    log.addHandler(h)
    yield h
    log.removeHandler(h)


def test_activity_emits_one_json_line_with_metadata(capture_activity):
    logging_config.activity("login", student_id=5, email="a@example.edu", method="google")
    assert len(capture_activity.lines) == 1
    rec = json.loads(capture_activity.lines[0])
    assert rec["event"] == "login"
    assert rec["student_id"] == 5
    # Emails never reach the log stream (it cannot be erased or anonymized):
    # activity() drops the field even when a call site passes it.
    assert "email" not in rec
    assert "a@example.edu" not in capture_activity.lines[0]
    assert rec["method"] == "google"
    assert rec["ts"].endswith("Z")


def test_none_fields_are_omitted(capture_activity):
    logging_config.activity("chat_turn", student_id=1, move=None, intent="QUESTION")
    rec = json.loads(capture_activity.lines[0])
    assert "move" not in rec
    assert rec["intent"] == "QUESTION"


def test_content_included_when_enabled(monkeypatch, capture_activity):
    monkeypatch.setenv("LOG_CONVERSATION_CONTENT", "true")
    logging_config.activity("chat_turn", student_id=1,
                            content={"user_message": "why is the eclipse red?", "reply": "let's trace it"})
    rec = json.loads(capture_activity.lines[0])
    assert rec["content"]["user_message"] == "why is the eclipse red?"
    assert rec["content"]["reply"] == "let's trace it"


def test_content_excluded_when_disabled(monkeypatch, capture_activity):
    monkeypatch.setenv("LOG_CONVERSATION_CONTENT", "false")
    logging_config.activity("chat_turn", student_id=1,
                            content={"user_message": "secret", "reply": "x"})
    rec = json.loads(capture_activity.lines[0])
    assert "content" not in rec


def test_long_content_is_clipped(monkeypatch, capture_activity):
    monkeypatch.setenv("LOG_CONVERSATION_CONTENT", "true")
    big = "x" * 20000
    logging_config.activity("chat_turn", student_id=1, content={"reply": big})
    rec = json.loads(capture_activity.lines[0])
    assert len(rec["content"]["reply"]) < 20000
    assert "chars]" in rec["content"]["reply"]


def test_activity_never_raises_on_unserialisable(capture_activity):
    # default=str handles odd objects; the whole call is also try/excepted.
    logging_config.activity("x", weird=object())
    assert len(capture_activity.lines) == 1  # emitted, did not raise


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------

def test_configure_logging_sets_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert logging_config.configure_logging() == "DEBUG"
    assert logging.getLogger("tutor").level == logging.DEBUG


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    logging_config.configure_logging()
    logging_config.configure_logging()
    app = logging.getLogger("tutor")
    assert sum(1 for h in app.handlers if getattr(h, "_tutor", False)) == 1
    act = logging.getLogger("tutor.activity")
    assert sum(1 for h in act.handlers if getattr(h, "_tutor_activity", False)) == 1


def test_content_enabled_default_false(monkeypatch):
    # Conversation text stays out of the host's logs unless an operator opts in
    # (plan step 0.8, decision 7).
    monkeypatch.delenv("LOG_CONVERSATION_CONTENT", raising=False)
    assert logging_config.content_enabled() is False
    monkeypatch.setenv("LOG_CONVERSATION_CONTENT", "true")
    assert logging_config.content_enabled() is True
    monkeypatch.setenv("LOG_CONVERSATION_CONTENT", "off")
    assert logging_config.content_enabled() is False


# ---------------------------------------------------------------------------
# Login tracking (auth helpers)
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ADMIN_TOKEN", "x")
    config.get_config.cache_clear()
    s = make_session()
    yield s
    s.close()
    config.get_config.cache_clear()


def test_set_session_cookie_bumps_login_and_stamps_method(db):
    from tutor.routers.auth import _set_session_cookie

    stu = Student(email="a@example.edu", name="A")
    db.add(stu)
    db.commit()
    db.refresh(stu)
    assert stu.login_count == 0 and stu.last_login is None

    _set_session_cookie(stu, db, method="google")
    db.refresh(stu)
    assert stu.login_count == 1
    assert stu.last_login is not None
    rows = db.query(SessionModel).filter_by(student_id=stu.id).all()
    assert len(rows) == 1 and rows[0].method == "google"

    _set_session_cookie(stu, db, method="magic")
    db.refresh(stu)
    assert stu.login_count == 2
    assert sorted(s.method for s in db.query(SessionModel).all()) == ["google", "magic"]


def test_login_or_create_threads_method_and_creates_student(db):
    from tutor.routers.auth import _login_or_create

    _login_or_create(db, email="new@example.edu", name="New", method="google")
    stu = db.query(Student).filter_by(email="new@example.edu").first()
    assert stu is not None and stu.login_count == 1
    sess = db.query(SessionModel).filter_by(student_id=stu.id).first()
    assert sess.method == "google"
