"""Sign-in problems fold into the digest, the Logins tab and the roster
(step 0.6; F28, F30, F35)."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor import login_digest
from tutor.auth import attempts
from tutor.db.models import ClassEnrollment, ClassInstructor, CourseClass, LoginAttempt, Student, WhitelistEntry
from tutor.utctime import utcnow
from tests.helpers import login_cookie, make_session

CHROME = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15"
SCANNER = "Mozilla/5.0 Proofpoint URLDefense"
NOW = utcnow()


def _row(db, outcome, email="ada@example.edu", method=attempts.MAGIC, minutes_ago=30, ua=CHROME, detail=None):
    db.add(LoginAttempt(email=email, method=method, outcome=outcome, user_agent=ua, detail=detail,
                        created_at=NOW - datetime.timedelta(minutes=minutes_ago)))
    db.commit()


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="ada@example.edu", name="Ada"))
    s.add(Student(id=2, email="bo@example.edu", name="Bo"))
    s.add(Student(id=3, email="prof@example.edu", name="Prof", is_instructor=True))
    s.commit()
    yield s
    s.close()


def test_the_summary_folds_the_trail(db):
    _row(db, attempts.LINK_SENT, minutes_ago=50)
    _row(db, attempts.CODE_INVALID, minutes_ago=45, detail="wrong code")
    _row(db, attempts.LINK_INVALID, minutes_ago=40, detail="link opened (used); scanner", ua=SCANNER)
    _row(db, attempts.CODE_INVALID, minutes_ago=35, detail="wrong code")          # Ada, never got in
    _row(db, attempts.LINK_SENT, email="bo@example.edu", minutes_ago=30)
    _row(db, attempts.CODE_INVALID, email="bo@example.edu", minutes_ago=25)
    _row(db, attempts.OK, email="bo@example.edu", minutes_ago=20)                        # Bo got in after
    _row(db, attempts.DENIED, email="stranger@gmail.com", minutes_ago=15)
    _row(db, attempts.LINK_INVALID, email=None, minutes_ago=14, detail="link opened (unknown)")
    _row(db, attempts.RATE_LIMITED, email="ada@example.edu", minutes_ago=13)
    _row(db, attempts.STARTED, email=None, method=attempts.GOOGLE, minutes_ago=12)
    _row(db, attempts.EXPIRED, email="bo@example.edu", method=attempts.SESSION, minutes_ago=10)
    _row(db, attempts.ACCESS_REMOVED, email="bo@example.edu", method=attempts.SESSION, minutes_ago=9)
    s = login_digest.summary(db, NOW - datetime.timedelta(hours=1), NOW + datetime.timedelta(minutes=1))
    assert (s.attempts, s.ok) == (13, 1)
    assert [(p.email, p.failures, p.got_in_after) for p in s.problems] == [("ada@example.edu", 3, False), ("bo@example.edu", 1, True)]
    ada = s.problems[0]
    assert ada.name == "Ada" and ada.outcomes == ["code_invalid", "code_invalid", "rate_limited"] and ada.scanner_taps == 1
    assert [p.email for p in s.unknown] == ["stranger@gmail.com"] and not s.unknown[0].known
    assert (s.anonymous_failures, s.scanner_taps, s.rate_limited) == (1, 1, 1)
    assert (s.unfinished_magic, s.unfinished_google) == (0, 1)      # Ada's link was settled by her failures
    assert (s.expired, s.access_removed) == (1, 1)
    assert s.empty is False

    md = login_digest.render_markdown(s)
    assert "## Sign-in problems" in md
    assert "- **Ada <ada@example.edu>**: 3 failures (code_invalid, code_invalid, rate_limited), last " in md
    assert "not signed in since" in md and "- Got in after failing: bo@example.edu" in md
    assert "- Not on the whitelist and tried: stranger@gmail.com (1)" in md
    assert "- Failures with no address" in md and "- Rate-limited requests: 1" in md
    assert "- Started and not completed: 1 Google sign-in" in md
    assert "- Signed-in students taken off the whitelist: 1" in md
    assert "- Mail scanner taps on links (harmless): 1" in md


def test_a_window_of_successes_is_quiet(db):
    _row(db, attempts.LINK_SENT, minutes_ago=5)
    _row(db, attempts.OK, minutes_ago=4)
    s = login_digest.summary(db, NOW - datetime.timedelta(hours=1), NOW + datetime.timedelta(minutes=1))
    assert s.empty and login_digest.render_markdown(s) == "" and s.ok == 1
    from scripts.daily_report import is_quiet_day

    assert is_quiet_day([], None, None, s) is False       # metrics None: send anyway
    assert login_digest.render_markdown(None) == ""


def test_the_logins_tab_lists_the_problems_in_local_time(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers.admin import issue_admin_session

    app.dependency_overrides[get_db] = lambda: db
    try:
        _row(db, attempts.CODE_INVALID, minutes_ago=45)
        _row(db, attempts.CODE_INVALID, minutes_ago=35)
        client = TestClient(app)
        client.cookies.set("admin_session", issue_admin_session())
        page = client.get("/admin?tab=logins").text
        assert "Sign-in problems, last 7 days" in page and 'data-problem="ada@example.edu"' in page
        assert "2 failures (code_invalid, code_invalid)" in page and "not signed in since" in page
        assert "When (local time)" in page and "When (UTC)" not in page
    finally:
        app.dependency_overrides.clear()
        config.get_config.cache_clear()


def test_the_roster_marks_a_student_who_could_not_sign_in(db, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app

    for s in db.query(Student).all():
        db.add(WhitelistEntry(email=s.email, added_by="test"))
    klass = CourseClass(name="AST 101 A", course="AST101", term="Autumn 2026", instructor_id=3)
    db.add(klass)
    db.flush()
    db.add(ClassInstructor(class_id=klass.id, student_id=3))
    db.add(ClassEnrollment(class_id=klass.id, student_id=1))
    db.add(ClassEnrollment(class_id=klass.id, student_id=2))
    db.commit()
    _row(db, attempts.CODE_INVALID, minutes_ago=60)
    _row(db, attempts.LINK_INVALID, minutes_ago=50)
    _row(db, attempts.CODE_INVALID, email="bo@example.edu", minutes_ago=40)
    _row(db, attempts.OK, email="bo@example.edu", minutes_ago=30)
    # Bo's used link tapped by the mail scanner, and a stranger typing his
    # address with no live link: neither is Bo's failure (the review of 0.9).
    _row(db, attempts.LINK_INVALID, email="bo@example.edu", minutes_ago=20, ua=SCANNER, detail="link opened (used); scanner")
    _row(db, attempts.CODE_INVALID, email="bo@example.edu", minutes_ago=10, detail="no live link")
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.cookies.set("session_token", login_cookie(db, 3))
        page = client.get(f"/instructor?class_id={klass.id}").text
        assert page.count("data-login-problem") == 1
        assert "could not sign in (2 failures), never signed in" in page
        ada = page[page.index("ada@example.edu"):page.index("bo@example.edu")]
        assert "data-login-problem" in ada
    finally:
        app.dependency_overrides.clear()
        config.get_config.cache_clear()
