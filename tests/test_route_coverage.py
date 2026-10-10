"""Plan step 6.2 (finding F11): every route is exercised by a test.

The review found 18 of 57 routes that no test referenced; by this step the
app has 63 routes and 11 were still unreferenced. They are tested here
through the real sign-in dependencies (a token admin session, a student
session cookie), and test_every_route_is_referenced_by_a_test fails when a
new route arrives without a test.
Done when: no route is unreferenced (the guard below).
"""
from __future__ import annotations

import ast
import datetime
import pathlib
import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor.db.models import (
    ExerciseAttempt, LearningGoal, Session as SessionModel, Student, StudentProgress, WhitelistEntry,
)
from tutor.utctime import utcnow
from tests.helpers import make_session

TESTS = pathlib.Path(__file__).resolve().parent
TOKEN = "tkn-route-test"


def _routes() -> list[tuple[str, list[str]]]:
    from starlette.routing import Mount

    from tutor.main import app

    out = []
    for r in app.routes:
        if isinstance(r, Mount) or not getattr(r, "methods", None):
            continue
        if r.path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            continue
        out.append((r.path, sorted(r.methods - {"HEAD"})))
    return out


def _referenced(path: str, corpus: str) -> bool:
    """A test names the path as a string: each {param} matches one segment,
    and the path ends at a quote, a query, a slash, or a fragment."""
    pat = re.sub(r"\\\{[^}]+\\\}", r"[^\"'?/ ]+", re.escape(path))
    return re.search(r"[\"'f]" + pat + r"(?:[\"'?/#]|$)", corpus) is not None


def _code_strings(source: str) -> str:
    """The string literals a module's code uses, one per line. Comments and
    docstrings are not code, so a route they mention is not covered by them
    (finding F26: the old probe counted /api/chat because a comment named it)."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)
    }
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.append(ast.unparse(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.append(repr(node.value))
    return "\n".join(out)


def test_every_route_is_referenced_by_a_test():
    # This file counts too: its tests below cover the routes nothing else
    # did. The matcher itself names no route.
    corpus = "\n".join(_code_strings(p.read_text()) for p in TESTS.rglob("*.py"))
    unreferenced = [f"{','.join(m)} {p}" for p, m in _routes() if not _referenced(p, corpus)]
    assert len(_routes()) >= 60
    assert unreferenced == []


def test_the_matcher_would_catch_one():
    assert not _referenced("/admin/api/secret", 'client.get("/admin/api/secrets")')
    assert _referenced("/admin/api/secret", 'client.get("/admin/api/secret")')
    assert _referenced("/auth/magic/{token}", 'client.get(f"/auth/magic/{tok}")')


def test_a_comment_or_docstring_is_not_coverage():
    source = (
        '"""Covers "/api/secret" in prose."""\n'
        "def test_x():\n"
        '    """Posts to "/api/secret"."""\n'
        '    client.get("/other")  # and "/api/secret" here\n'
    )
    assert not _referenced("/api/secret", _code_strings(source))
    assert _referenced("/api/secret", _code_strings('client.post("/api/secret", json={})'))
    assert _referenced("/auth/magic/{token}", _code_strings('client.get(f"/auth/magic/{tok}")'))


# ---------------------------------------------------------------------------
# The routes nothing else tested
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(WhitelistEntry(email="stu@example.edu", added_by="test"))
    s.add(LearningGoal(id="AST101-L03-g", title="Seasons", description="", course="AST101", lecture_num=3))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def app(db, monkeypatch):
    import tutor.main as main_mod
    from tutor.auth.deps import get_db

    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    main_mod.app.dependency_overrides[get_db] = lambda: db
    try:
        yield main_mod.app
    finally:
        main_mod.app.dependency_overrides.clear()
        config.get_config.cache_clear()


@pytest.fixture
def admin(app):
    client = TestClient(app, base_url="http://localhost")
    assert client.post("/admin/login", data={"token": TOKEN}, follow_redirects=False).status_code == 303
    return client


@pytest.fixture
def student(app, db):
    from tutor.auth.jwt_utils import create_token

    token, jti, expires = create_token(1)
    db.add(SessionModel(student_id=1, jwt_jti=jti, expires_at=expires, method="magic"))
    db.commit()
    client = TestClient(app, base_url="http://localhost")
    client.cookies.set("session_token", token)
    return client


def test_the_dashboard_answers_with_and_without_the_slash(admin):
    assert admin.get("/admin/").status_code == 200
    assert admin.get("/admin").status_code == 200


def test_admin_logout_ends_the_token_session(admin):
    resp = admin.post("/admin/logout", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert any(c.startswith("admin_session=") for c in resp.headers.get_list("set-cookie"))
    assert admin.get("/admin/api/exercises").status_code == 403


def test_the_exercise_attempts_api(admin, student, db):
    db.add(ExerciseAttempt(student_id=1, exercise_id="AST101-L03-code-01", submission="int f();",
                           score=3, passed=True))
    db.commit()
    rows = admin.get("/admin/api/exercises").json()
    assert [(r["exercise_id"], r["score"], r["passed"]) for r in rows] == [("AST101-L03-code-01", 3, True)]
    assert student.get("/admin/api/exercises").status_code == 403          # a student is not an admin


def test_the_whitelist_routes(admin, student, db):
    from tutor.auth.access import whitelist_enabled

    resp = admin.post("/admin/whitelist/add", data={"email": "New@example.edu"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin?tab=access"
    assert db.query(WhitelistEntry).filter_by(email="new@example.edu").count() == 1
    admin.post("/admin/whitelist/remove", data={"email": "new@example.edu"}, follow_redirects=False)
    assert db.query(WhitelistEntry).filter_by(email="new@example.edu").count() == 0
    admin.post("/admin/whitelist/toggle", data={"enabled": "on"}, follow_redirects=False)
    assert whitelist_enabled(db) is True
    admin.post("/admin/whitelist/toggle", data={}, follow_redirects=False)
    assert whitelist_enabled(db) is False
    assert student.post("/admin/whitelist/add", data={"email": "x@example.edu"}).status_code == 403
    assert db.query(WhitelistEntry).filter_by(email="x@example.edu").count() == 0


def test_the_review_nudge(student, db):
    assert student.get("/review/nudge?course=AST101").json() == {"due_count": 0, "minutes": 0, "message": ""}
    db.add(StudentProgress(student_id=1, goal_id="AST101-L03-g", status="mastered", mastery_score=0.9,
                           fsrs_stability=2.0, fsrs_due=utcnow() - datetime.timedelta(days=1)))
    db.commit()
    with patch("tutor.agent.mastery._has_exam_questions", return_value=True):
        body = student.get("/review/nudge?course=AST101").json()
    assert body["due_count"] == 1 and body["message"].startswith("1 item due for review")
