"""Plan step 5.4 (findings C23, C24): web-layer consolidation.

C23: COURSES in five places; Jinja2Templates built in nine routers from a
cwd-relative path; three re-implementations of the session check; the
lowercased-email lookup written out thirteen times; admin_dashboard a
336-line run of ``if tab ==`` blocks; the progress panel 169 lines of
f-string HTML escaped by hand; no router under mypy. C24: JSON embedded in
<script> was escaped for "</" only.
Done when: mypy gates the routers; admin_dashboard under 60 lines.
"""
from __future__ import annotations

import ast
import datetime
import importlib
import json
import pathlib
import re
import tomllib
from unittest.mock import patch

import pytest

from tests.browser_support import app_client, student_db
from tutor.db.models import LearningGoal, Session as SessionModel, WhitelistEntry
from tutor.utctime import utcnow

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "tutor"


def _sources():
    return [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]


# ---------------------------------------------------------------------------
# One of each
# ---------------------------------------------------------------------------

def test_every_page_renders_from_one_anchored_template_directory():
    from tutor import templating

    assert templating.TEMPLATES_DIR.is_absolute() and (templating.TEMPLATES_DIR / "base.html").exists()
    for path in (APP / "routers").glob("*.py"):
        module = importlib.import_module(f"tutor.routers.{path.stem}")
        if hasattr(module, "templates"):
            assert module.templates is templating.templates, path.name
    built = [p.name for p in _sources() if "Jinja2Templates(" in p.read_text()]
    assert built == ["templating.py"]


def _code_strings(source: str) -> list[str]:
    """The string literals a module's code uses: docstrings (and comments,
    which the AST drops) may name a course as an example."""
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_the_course_list_is_written_once():
    """courses.yaml is the one place the courses are named (tutor/courses.py
    reads it): no app module spells a course id in its code."""
    from tutor.courses import COURSES

    assert COURSES
    spelled = {p.name: hits for p in _sources()
               if (hits := sorted({s for s in _code_strings(p.read_text()) if s in COURSES}))}
    assert spelled == {}
    # The check itself: a hardcoded course tuple is caught.
    assert set(_code_strings(f"COURSES = {tuple(COURSES)!r}\n")) == set(COURSES)


def test_the_email_lookup_is_written_once():
    users = [p.name for p in _sources() if "func.lower(Student.email)" in p.read_text()]
    assert users == ["access.py"]
    assert (APP / "auth" / "access.py").read_text().count("func.lower(Student.email)") == 1


def test_find_student_ignores_case_and_spaces():
    from tutor.auth.access import find_student

    db = student_db(email="Kim.Lee@Example.EDU")
    assert find_student(db, "  kim.lee@example.Edu ").id == 1
    assert find_student(db, "") is None and find_student(db, None) is None
    assert find_student(db, "nobody@example.edu") is None


# ---------------------------------------------------------------------------
# One session check
# ---------------------------------------------------------------------------

@pytest.fixture
def sessions():
    from tutor.auth.jwt_utils import create_token

    db = student_db()
    token, jti, expires = create_token(1)
    row = SessionModel(student_id=1, jwt_jti=jti, expires_at=expires, method="magic")
    db.add(row)
    db.commit()
    return db, token, row


def test_session_student_accepts_only_a_live_session(sessions):
    from tutor.auth.deps import session_student

    db, token, row = sessions
    assert session_student(db, token).id == 1
    for bad in (None, "", "not-a-jwt"):
        assert session_student(db, bad) is None
    row.revoked = True
    db.commit()
    assert session_student(db, token) is None
    row.revoked = False
    row.expires_at = utcnow() - datetime.timedelta(minutes=1)
    db.commit()
    assert session_student(db, token) is None


def test_session_student_drops_a_student_whose_access_was_removed(sessions):
    from tutor.auth.deps import session_student

    db, token, _row = sessions
    db.query(WhitelistEntry).delete()
    db.commit()
    assert session_student(db, token) is None


def test_the_help_page_and_the_root_read_a_real_session(sessions):
    from fastapi.testclient import TestClient

    import tutor.main as main_mod
    from tutor.auth.deps import get_db

    db, token, row = sessions
    main_mod.app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(main_mod.app)
        client.cookies.set("session_token", token)
        assert 'aria-label="Main"' in client.get("/help").text        # signed in: the site header
        with patch("tutor.db.models.get_session_factory", return_value=lambda: db), \
             patch.object(db, "close", lambda: None):
            assert client.get("/", follow_redirects=False).headers["location"] == "/chat"
            row.revoked = True
            db.commit()
            assert client.get("/", follow_redirects=False).headers["location"] == "/auth/login"
        assert "Log in" in client.get("/help").text                     # revoked: signed out
    finally:
        main_mod.app.dependency_overrides.clear()


def test_no_router_decodes_the_session_cookie_itself():
    decoders = sorted(p.relative_to(APP).as_posix() for p in _sources()
                      if "decode_token(" in p.read_text())
    # deps: the two session checks; auth: logout revokes the session it names.
    assert decoders == ["auth/deps.py", "auth/jwt_utils.py", "routers/auth.py"]


# ---------------------------------------------------------------------------
# The admin dashboard, per tab
# ---------------------------------------------------------------------------

def test_admin_dashboard_stays_small_and_every_tab_has_a_builder():
    from tutor.routers import admin

    tree = ast.parse((APP / "routers" / "admin.py").read_text())
    size = next(n.end_lineno - n.lineno + 1 for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "admin_dashboard")
    assert size < 60, size
    tabs = {key for _group, items in admin.TAB_GROUPS for key, _label in items}
    assert tabs == set(admin._TAB_BUILDERS)
    for build in admin._TAB_BUILDERS.values():
        assert set(build.__annotations__) >= {"db", "student", "class_id"}


def test_an_unknown_tab_renders_empty():
    from fastapi.testclient import TestClient

    import tutor.main as main_mod
    from tutor.auth.deps import get_db
    from tutor.routers.admin import issue_admin_session

    db = student_db()
    main_mod.app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(main_mod.app)
        client.cookies.set("admin_session", issue_admin_session())
        assert client.get("/admin?tab=nonsense").status_code == 200
    finally:
        main_mod.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# The progress panel is a template, escaped by Jinja
# ---------------------------------------------------------------------------

def test_the_progress_panel_escapes_titles_and_keeps_ids_out_of_scripts(tmp_path, monkeypatch):
    import tutor.config as config
    from tutor import decks
    from tutor.db.models import Track

    # The slides link is offered only for a deck on disk (review step 0.3).
    deck = tmp_path / "AST101" / "lecture-01"
    deck.mkdir(parents=True)
    (deck / "index.html").write_text("<html></html>")
    cfg = dict(config.get_config())
    cfg["output_dir"] = tmp_path
    monkeypatch.setattr(decks, "get_config", lambda: cfg)
    decks._reset_for_tests()
    db = student_db()
    title = 'Stars <img src=x onerror=alert(1)> & "friends"'
    db.add(LearningGoal(id="AST101-L01-o'neil", title=title, description="", course="AST101", lecture_num=1,
                        slide_anchor=json.dumps({"lecture": 1})))
    db.get(Track, "AST101-lecture-order").goal_ids_json = json.dumps(["AST101-L01-o'neil"])
    db.commit()
    try:
        with app_client(db) as client:
            panel = client.get("/chat/progress?course=AST101").text
    finally:
        decks._reset_for_tests()             # the tmp deck list must not outlive the test
    assert "<img src=x" not in panel and "&lt;img src=x onerror=alert(1)&gt; &amp; &#34;friends&#34;" in panel
    assert 'data-goal="AST101-L01-o&#39;neil"' in panel
    assert "recordPathEvent(this.dataset.goal, this.dataset.course, 'teach')" in panel
    assert "o'neil" not in panel and "o\\'neil" not in panel          # never inside a JS string
    assert 'href="/slides/AST101/lecture-01/index.html"' in panel


# ---------------------------------------------------------------------------
# C24: JSON inside <script>
# ---------------------------------------------------------------------------

EVIL = "</script><script>alert(1)</script>"


def test_lecture_titles_cannot_close_the_chat_pages_script():
    db = student_db()
    with patch("tutor.agent.lecture_plan.all_lecture_titles",
               return_value={"AST101": {1: EVIL}, "AST102": {}}), app_client(db) as client:
        page = client.get("/chat?course=AST101").text
    line = next(ln for ln in page.splitlines() if "window.LECTURE_TITLES" in ln)
    data = line.split("=", 1)[1].rsplit(";</script>", 1)[0]
    # No raw "<" at all: the old "</" escape still let "<script>" and "<!--"
    # through, which can change how the browser parses the rest.
    assert EVIL not in page and "<" not in data and "\\u003c/script\\u003e" in data
    assert json.loads(data) == {"AST101": {"1": EVIL}, "AST102": {}}


def test_goal_titles_cannot_close_the_maps_script():
    db = student_db()
    db.add(LearningGoal(id="AST101-L01-x", title=EVIL, description="", course="AST101", lecture_num=1))
    db.commit()
    with app_client(db) as client:
        page = client.get("/map?course=AST101").text
    assert EVIL not in page
    data = re.search(r'<script type="application/json" id="graph-data">(.*?)</script>', page, re.S).group(1)
    assert "<" not in data
    assert json.loads(data)["nodes"][0]["title"] == EVIL


# ---------------------------------------------------------------------------
# mypy gates the routers
# ---------------------------------------------------------------------------

def test_mypy_gates_the_routers_and_the_web_helpers():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mypy"]
    for path in ("tutor/routers", "tutor/classes.py", "tutor/class_metrics.py", "tutor/health.py",
                 "tutor/courses.py", "tutor/templating.py"):
        assert path in config["files"], path
