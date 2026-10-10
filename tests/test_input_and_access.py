"""Plan step 2.9 (findings C15, B24, C5, C21, C14, C8): input validation and access.

C15/B24: ``course`` reached varchar(8) columns unchecked, so on Postgres an
over-length value was a 500 that emailed the operator. C5: removing a student
from the whitelist left their session working for days. C21: asking for a
magic link created a students row, links were never pruned, and one link could
log in twice. C14: an empty ADMIN_EMAILS failed silently. C8: the decks,
narration scripts, and narration audio were public URLs.
Done when: a 9-char course is a 422 everywhere; a removed student's cookie is 401.
"""
from __future__ import annotations

import datetime
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tutor.db.models import (
    Base, LearningGoal, MagicLink, Session as SessionModel, Student, Track, WhitelistEntry,
)
from tests.helpers import login_cookie, make_session

BAD = "123456789"          # nine characters: longer than varchar(8)


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Seasons", description="", course="AST101", lecture_num=3))
    s.add(Track(id="AST101-lecture-order", course="AST101", title="Lecture order", goal_ids_json="[]"))
    s.add(WhitelistEntry(email="stu@example.edu", added_by="test"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def app_with(db, monkeypatch):
    import tutor.config as config
    import tutor.main as main_mod
    from tutor.auth.deps import get_db

    gate_cache = getattr(main_mod, "_gate_cache", {})
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    gate_cache.clear()
    main_mod.app.dependency_overrides[get_db] = lambda: db
    try:
        yield main_mod.app
    finally:
        main_mod.app.dependency_overrides.clear()
        gate_cache.clear()
        config.get_config.cache_clear()


_login = login_cookie   # tests/helpers.py


# ---------------------------------------------------------------------------
# A 9-char course is a 422 everywhere
# ---------------------------------------------------------------------------

_ENDPOINTS = [
    ("post", "/chat/message", {"data": {"message": "hi", "course": BAD}}),
    ("post", "/chat/teachme", {"data": {"goal": "G1", "course": BAD}}),
    ("get", f"/chat/history?course={BAD}", {}),
    ("get", f"/chat/starters?course={BAD}", {}),
    ("get", f"/chat/progress?course={BAD}", {}),
    ("post", "/path/event", {"json": {"course": BAD, "modality": "teach"}}),
    ("post", "/path/track", {"json": {"course": BAD, "track_id": "AST101-lecture-order"}}),
    ("post", "/path/course", {"json": {"course": BAD}}),
    ("get", f"/path/next?course={BAD}", {}),
    ("get", f"/path/current?course={BAD}", {}),
    # /review opens on the student's course instead (bulletproofing R15).
    ("get", f"/review/session?course={BAD}", {}),
    ("get", f"/review/nudge?course={BAD}", {}),
]


@pytest.mark.parametrize("method,path,kwargs", _ENDPOINTS, ids=[p for _, p, _ in _ENDPOINTS])
def test_a_nine_char_course_is_a_422(app_with, db, method, path, kwargs):
    client = TestClient(app_with)
    client.cookies.set("session_token", _login(db))
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 422, resp.text
    assert "Unknown course" in resp.text


def test_the_json_api_rejects_it_too(app_with, db):
    db.add(Student(id=2, email="prof@example.edu", name="Prof"))
    db.commit()
    client = TestClient(app_with)
    client.cookies.set("session_token", _login(db, 2))
    resp = client.post("/api/chat", json={"message": "hi", "course": BAD})
    assert resp.status_code == 422 and "Unknown course" in resp.text


def test_valid_courses_still_work(app_with, db):
    client = TestClient(app_with)
    client.cookies.set("session_token", _login(db))
    assert client.get("/chat/history?course=AST102").status_code == 200
    assert client.post("/path/event", json={"course": "AST101", "modality": "explore"}).status_code == 200
    assert client.post("/path/event", json={"course": "AST101", "modality": "bogus"}).status_code == 422


def test_one_course_list():
    from tutor import classes, courses
    from tutor.routers import chat, lectures, onboarding, path

    assert classes.COURSES == lectures.COURSES == path.COURSES == chat.COURSES == courses.COURSES
    assert tuple(onboarding.COURSES) == courses.COURSES


# ---------------------------------------------------------------------------
# A removed student's cookie is 401
# ---------------------------------------------------------------------------

def test_a_removed_students_cookie_is_401(app_with, db):
    from tutor.auth.access import remove_from_whitelist

    client = TestClient(app_with)
    token = _login(db)
    client.cookies.set("session_token", token)
    assert client.get("/chat/history?course=AST101").status_code == 200
    remove_from_whitelist(db, "stu@example.edu")
    resp = client.get("/chat/history?course=AST101")
    assert resp.status_code == 401 and resp.json()["detail"] == "Access removed"
    assert db.query(SessionModel).filter_by(student_id=1).one().revoked is True
    add_back = WhitelistEntry(email="stu@example.edu", added_by="test")
    db.add(add_back)
    db.commit()
    assert client.get("/chat/history?course=AST101").status_code == 401      # revoked for good


def test_turning_the_whitelist_on_locks_out_the_unlisted(app_with, db):
    from tutor.auth.access import set_whitelist_enabled

    db.add(Student(id=3, email="walkin@example.edu", name="Walk"))
    db.commit()
    set_whitelist_enabled(db, False)
    client = TestClient(app_with)
    client.cookies.set("session_token", _login(db, 3))
    assert client.get("/chat/history?course=AST101").status_code == 200
    set_whitelist_enabled(db, True)
    assert client.get("/chat/history?course=AST101").status_code == 401


def test_an_admin_is_never_locked_out(app_with, db):
    db.add(Student(id=2, email="prof@example.edu", name="Prof"))
    db.commit()
    client = TestClient(app_with)
    client.cookies.set("session_token", _login(db, 2))
    assert client.get("/chat/history?course=AST101").status_code == 200


# ---------------------------------------------------------------------------
# Magic links
# ---------------------------------------------------------------------------

def _request_link(client, email) -> str:
    import re

    from tutor.routers import auth

    auth._magic_link_log.clear()
    page = client.post("/auth/magic-link", data={"email": email}).text
    return re.search(r'href="https?://[^/"]+(/auth/magic/[^"?]+)', page).group(1)


def test_asking_for_a_link_creates_no_student(app_with, db):
    db.add(WhitelistEntry(email="@example.edu", added_by="test"))
    db.commit()
    client = TestClient(app_with)
    path = _request_link(client, "someone@example.edu")
    assert db.query(Student).filter_by(email="someone@example.edu").first() is None
    link = db.query(MagicLink).filter_by(email="someone@example.edu").one()
    assert link.student_id is None

    assert client.get(path).status_code == 200                           # the confirm page
    assert db.query(Student).filter_by(email="someone@example.edu").first() is None   # spends nothing
    resp = client.post(path, follow_redirects=False)                      # Continue
    assert resp.status_code == 303 and "session_token" in resp.headers["set-cookie"]
    student = db.query(Student).filter_by(email="someone@example.edu").one()
    db.refresh(link)
    assert link.student_id == student.id and link.used is True
    assert client.post(path, follow_redirects=False).status_code == 400   # one use only


def test_a_link_for_an_address_removed_meanwhile_is_denied(app_with, db):
    from tutor.auth.access import remove_from_whitelist

    db.add(WhitelistEntry(email="later@example.edu", added_by="test"))
    db.commit()
    client = TestClient(app_with)
    path = _request_link(client, "later@example.edu")
    remove_from_whitelist(db, "later@example.edu")
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 403 and "is not authorized" in resp.text
    assert db.query(Student).filter_by(email="later@example.edu").first() is None


def test_a_link_is_consumed_by_one_conditional_update(app_with, db):
    """The claim is a single UPDATE ... WHERE used = false: a second claim of
    the same token matches no row, whatever the timing."""
    from tutor.utctime import utcnow

    db.add(MagicLink(email="stu@example.edu", token="tok", used=False,
                     expires_at=utcnow() + datetime.timedelta(minutes=5)))
    db.commit()
    first = db.query(MagicLink).filter(MagicLink.token == "tok", MagicLink.used == False)  # noqa: E712
    assert first.update({MagicLink.used: True}, synchronize_session=False) == 1
    again = db.query(MagicLink).filter(MagicLink.token == "tok", MagicLink.used == False)  # noqa: E712
    assert again.update({MagicLink.used: True}, synchronize_session=False) == 0
    db.rollback()


def test_requesting_a_link_prunes_that_addresses_dead_links(app_with, db):
    from tutor.utctime import utcnow

    past = utcnow() - datetime.timedelta(hours=1)
    db.add_all([MagicLink(email="stu@example.edu", token="old-1", used=True, expires_at=past),
                MagicLink(email="stu@example.edu", token="old-2", used=False, expires_at=past),
                MagicLink(email="other@example.edu", token="keep", used=True, expires_at=past)])
    db.commit()
    _request_link(TestClient(app_with), "stu@example.edu")
    tokens = {m.token for m in db.query(MagicLink).all()}
    assert "old-1" not in tokens and "old-2" not in tokens and "keep" in tokens
    assert len(tokens) == 2


def test_erasure_takes_links_that_carry_only_the_email(db):
    from tutor.agent import state
    from tutor.utctime import utcnow

    db.add(MagicLink(email="stu@example.edu", token="pending", expires_at=utcnow()))
    db.commit()
    state.delete_student_data(db, 1)
    db.commit()
    assert db.query(MagicLink).count() == 0


def test_login_still_works_before_the_migration_runs(tmp_path, monkeypatch):
    """magic_links.student_id NOT NULL (a database the migration has not
    reached): the link falls back to creating the student row first."""
    import tutor.config as config
    import tutor.main as main_mod
    from sqlalchemy import text

    from tutor.auth.deps import get_db

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE magic_links"))
        conn.execute(text(
            "CREATE TABLE magic_links (id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL, "
            "email VARCHAR(255), token VARCHAR(128) NOT NULL UNIQUE, code VARCHAR(8), "
            "code_attempts INTEGER NOT NULL DEFAULT 0, expires_at DATETIME NOT NULL, "
            "used BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME)"))
    session = sessionmaker(bind=engine)()
    session.add(WhitelistEntry(email="new@example.edu", added_by="test"))
    session.commit()
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    main_mod.app.dependency_overrides[get_db] = lambda: session
    try:
        client = TestClient(main_mod.app)
        path = _request_link(client, "new@example.edu")
        assert client.post(path, follow_redirects=False).status_code == 303
    finally:
        main_mod.app.dependency_overrides.clear()
        config.get_config.cache_clear()
        session.close()


# ---------------------------------------------------------------------------
# Decks and narration need a session; the book stays public
# ---------------------------------------------------------------------------

@pytest.fixture
def deck():
    import tutor.main as main_mod

    root = main_mod._slides_dir / "_test-gate"
    (root / "lecture-01").mkdir(parents=True, exist_ok=True)
    (root / "lecture-01" / "index.html").write_text("<html>deck</html>")
    (root / "lecture-01" / "script.md").write_text("narration notes")
    try:
        yield "/slides/_test-gate/lecture-01"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_decks_need_a_session(app_with, db, deck):
    client = TestClient(app_with)
    assert client.get(f"{deck}/index.html").status_code == 401
    page = client.get(f"{deck}/index.html", headers={"accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/auth/login?next=")
    client.cookies.set("session_token", _login(db))
    ok = client.get(f"{deck}/index.html")
    # The mount adds the activity tag to every deck it serves (tutor/deck_tracking.py).
    assert ok.status_code == 200 and ok.text.startswith("<html>deck</html>") and "deck-track.js" in ok.text
    assert ok.headers["cache-control"] == "no-cache"
    assert client.get(f"{deck}/script.md").status_code == 404        # never served


def test_a_removed_students_cookie_opens_no_deck(app_with, db, deck):
    from tutor.auth.access import remove_from_whitelist

    client = TestClient(app_with)
    client.cookies.set("session_token", _login(db))
    remove_from_whitelist(db, "stu@example.edu")
    assert client.get(f"{deck}/index.html").status_code == 401


def test_the_explorables_stay_public(app_with):
    resp = TestClient(app_with).get("/explorables/no-such-file.html")
    assert resp.status_code == 404                                      # not 401


# ---------------------------------------------------------------------------
# An empty ADMIN_EMAILS is reported at boot
# ---------------------------------------------------------------------------

def test_boot_warns_when_no_admin_is_configured(monkeypatch, tmp_path):
    import time

    import tutor.config as config
    from tutor import alerts, startup
    from tutor.main import app

    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("BOOT_WARNINGS", raising=False)
    monkeypatch.setenv("TUTOR_DB", str(tmp_path / "boot.db"))
    config.get_config.cache_clear()
    startup._reset_for_tests()
    sent: list = []
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: sent.append(body))
    try:
        with TestClient(app):
            deadline = time.monotonic() + 3
            while not sent and time.monotonic() < deadline:
                time.sleep(0.02)
    finally:
        config.get_config.cache_clear()
        startup._reset_for_tests()
    assert any("ADMIN_EMAILS is empty" in body for body in sent)


def test_the_courses_module():
    from fastapi import HTTPException

    from tutor.courses import optional_course, require_course

    assert require_course(" AST101 ") == "AST101"
    assert optional_course(None) is None and optional_course("") is None
    for bad in (BAD, "", None, "34", "AST101; DROP"):
        with pytest.raises(HTTPException) as err:
            require_course(bad)
        assert err.value.status_code == 422
