"""Plan step 3.1 (findings D21, D7, D3, D29): one site header.

D21: nine hand-written headers with different link sets and three
current-page styles. D7: emoji-only links on phones with no accessible name.
D3: the review count covered both courses but the link opened AST101. D29: chat,
map, help, and admin had no h1.
Done when: every page's header comes from the partial; a AST102 student's review
link lands on AST102 items.
"""
from __future__ import annotations

import datetime
import pathlib
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from tutor.db.models import (
    LearningGoal, Session as SessionModel, Student, StudentPath, StudentProgress, Track,
)
from tutor.utctime import utcnow
from tests.helpers import make_session

_TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "tutor" / "web" / "templates"


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="prof@example.edu", name="Prof", is_instructor=True))
    for course in ("AST101", "AST102"):
        s.add(Track(id=f"{course}-lecture-order", course=course, title="Lecture order",
                    goal_ids_json="[]"))
    s.add(LearningGoal(id="A1", title="The celestial sphere", description="", course="AST101", lecture_num=1))
    s.add(LearningGoal(id="B1", title="Parallax", description="", course="AST102", lecture_num=1))
    s.add(LearningGoal(id="B2", title="Stellar lives", description="", course="AST102", lecture_num=2))
    s.commit()
    # The student works in AST102 now (the most recent path).
    s.add(StudentPath(student_id=1, course="AST101", track_id="AST101-lecture-order",
                      updated_at=utcnow() - datetime.timedelta(days=3)))
    s.add(StudentPath(student_id=1, course="AST102", track_id="AST102-lecture-order",
                      updated_at=utcnow()))
    due = utcnow() - datetime.timedelta(hours=1)
    for goal in ("A1", "B1", "B2"):
        s.add(StudentProgress(student_id=1, goal_id=goal, status="mastered", fsrs_due=due))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db, monkeypatch):
    import tutor.config as config
    import tutor.main as main_mod
    from tutor.auth.deps import get_db
    from tutor.auth.jwt_utils import create_token

    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    main_mod.app.dependency_overrides[get_db] = lambda: db
    token, jti, expires = create_token(1)
    db.add(SessionModel(student_id=1, jwt_jti=jti, expires_at=expires, method="magic"))
    db.commit()
    c = TestClient(main_mod.app)
    c.cookies.set("session_token", token)
    try:
        yield c
    finally:
        main_mod.app.dependency_overrides.clear()
        config.get_config.cache_clear()


@pytest.fixture
def deck():
    import tutor.main as main_mod

    root = main_mod._slides_dir / "AST102" / "lecture-99"
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<html>deck</html>")
    try:
        yield "/lectures/AST102/99"
    finally:
        shutil.rmtree(root, ignore_errors=True)


_PAGES = [
    ("/chat?course=AST102", "chat"),
    ("/map", "map"),
    ("/lectures", "lectures"),
    ("/review?course=AST102", "review"),
    ("/help", "help"),
    ("/welcome", None),
    ("/track-picker?course=AST102", None),
    ("/instructor", "instructor"),
    ("/instructor/report", "instructor"),
    ("/admin", "admin"),
]

_CURRENT_HREF = {"chat": "/chat", "map": "/map", "lectures": "/lectures", "help": "/help",
                 "review": "/review?course=AST102", "instructor": "/instructor", "admin": "/admin"}


def _current_links(html: str) -> set[str]:
    """hrefs of the site header's links marked as the current page."""
    header = html[html.index("<header"):html.index("</header>")]
    return set(re.findall(r'<a href="([^"]+)"[^>]*aria-current="page"', header))


@pytest.mark.parametrize("path,current", _PAGES, ids=[p for p, _ in _PAGES])
def test_every_page_renders_the_shared_header(client, path, current):
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    html = resp.text
    assert html.count("<header") == 1                       # one header, the partial's
    assert "data-nav-menu" in html and html.count('aria-label="Main"') == 2
    for href in ("/chat", "/map", "/lectures", "/help", "/instructor", "/admin"):
        assert f'href="{href}"' in html, (path, href)
    assert html.count('<form method="post" action="/auth/logout"') == 2   # a POST (step 4.5)
    assert _current_links(html) == ({_CURRENT_HREF[current]} if current else set())
    # Every link has a text label; on narrow screens it sits in the menu sheet.
    assert '<span aria-hidden="true">🗺</span><span>Map</span>' in html


def test_the_lecture_viewer_too(client, deck):
    html = client.get(deck).text
    assert html.count("<header") == 1 and "data-nav-menu" in html
    assert _current_links(html) == {"/lectures"}


def test_pages_without_a_visible_heading_get_a_hidden_one(client):
    for path, heading in (("/chat?course=AST102", "Chat with the tutor"), ("/map", "Learning map"),
                          ("/help", "Help"), ("/admin", "Admin")):
        assert f'<h1 class="sr-only">{heading}</h1>' in client.get(path).text, path


def test_a_ast102_students_review_link_lands_on_ast102_items(client, monkeypatch):
    """The header's Review link carries the student's course, and the
    progress panel counts that course's due items (the review page's own
    query) and links there."""
    from tutor.agent import mastery

    monkeypatch.setattr(mastery, "_has_exam_questions", lambda goal_id: True)
    for path in ("/map", "/lectures", "/instructor"):
        assert 'href="/review?course=AST102"' in client.get(path).text, path
    panel = client.get("/chat/progress?course=AST102").text
    assert "2 due for review" in panel and 'href="/review?course=AST102"' in panel
    assert client.get("/chat/progress?course=AST101").text.count("1 due for review") == 1
    session = client.get("/review/session?course=AST102").json()
    assert session["due_count"] == 2


def test_no_template_hand_writes_a_header():
    for tpl in _TEMPLATES.glob("*.html"):
        # help.html and error.html render the partial when signed in and a
        # two-link header for a visitor, as the login page does.
        if tpl.name in ("_nav.html", "help.html", "error.html", "login.html", "base.html"):
            continue
        assert "<header" not in tpl.read_text(), f"{tpl.name}: use site_header from _nav.html"
    used = [t.name for t in _TEMPLATES.glob("*.html")
            if t.name != "_nav.html" and "site_header(" in t.read_text()]
    assert sorted(used) == sorted([
        "admin.html", "chat.html", "error.html", "help.html", "instructor.html", "instructor_report.html",
        "instructor_transcript.html", "lecture_view.html", "lectures.html", "map.html", "review.html",
        "track_picker.html", "welcome.html",
    ])
