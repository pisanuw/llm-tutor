"""Repeated failed logins reach the operator (step 0.6; F28, F29, F31,
F36).

F28: failures were rows behind an admin tab. F29: a tap on a dead link was
recorded without the address. F31: an expired session and a whitelist
removal left no row. F36: the rate-limit banner named no time.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor import alerts
from tutor.auth import attempts, login_alerts
from tutor.db.models import LoginAttempt, MagicLink, Student, WhitelistEntry
from tutor.utctime import utcnow
from tests.helpers import login_cookie, make_session

CHROME = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile Safari/604.1"
SCANNER = "Mozilla/5.0 Proofpoint URLDefense link checker"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session()
    db.add(Student(id=1, email="ada@example.edu", name="Ada"))
    db.add(WhitelistEntry(email="ada@example.edu", added_by="test"))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: sent.append((key, subject, body)) or "queued")
    client = TestClient(app)
    try:
        yield client, db, sent
    finally:
        app.dependency_overrides.clear()
        db.close()
        config.get_config.cache_clear()


def _fail(db, outcome, email="ada@example.edu", ua=CHROME, minutes_ago=0, detail=None):
    row = LoginAttempt(email=email, method=attempts.MAGIC, outcome=outcome, user_agent=ua, detail=detail,
                       created_at=utcnow() - datetime.timedelta(minutes=minutes_ago))
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# The alert
# ---------------------------------------------------------------------------

def test_the_third_failure_in_an_hour_emails_once_with_the_advice(world):
    client, db, sent = world
    _fail(db, attempts.CODE_INVALID, minutes_ago=20, detail="wrong code")
    _fail(db, attempts.LINK_INVALID, minutes_ago=10, detail="link opened (expired)")
    assert login_alerts.note_failure(db, email="ada@example.edu", user_agent=CHROME) == "counted"
    _fail(db, attempts.CODE_INVALID, detail="wrong code")
    assert login_alerts.note_failure(db, email="ada@example.edu", user_agent=CHROME) == "queued"
    (key, subject, body), = sent
    assert key.startswith("login:") and subject == "Sign-in failing: Ada <ada@example.edu>"
    assert "failed to sign in 3 times" in body and "code_invalid" in body and "link_invalid" in body
    assert "What to tell them: Their code was wrong or exhausted" in body
    assert "/admin?tab=logins&student=ada@example.edu" in body and " PDT" in body or " PST" in body


def test_a_scanner_and_an_unknown_address_never_alert(world):
    client, db, sent = world
    for _ in range(4):
        _fail(db, attempts.LINK_INVALID, ua=SCANNER, detail="link opened (used); scanner")
    assert login_alerts.note_failure(db, email="ada@example.edu", user_agent=SCANNER) == "skipped"
    for _ in range(4):
        _fail(db, attempts.DENIED, email="stranger@gmail.com")
    assert login_alerts.note_failure(db, email="stranger@gmail.com", user_agent=CHROME) == "skipped"
    assert sent == []


def test_a_strangers_typing_never_counts_as_the_students_failure(world):
    """The public form records a rate-limited request, a malformed
    submission and a code with no live link under whatever address was
    typed; anyone can produce those for anyone, so they never alert and
    never mark the student locked out (the review of step 0.9)."""
    client, db, sent = world
    for detail, outcome in (("no live link", attempts.CODE_INVALID), ("malformed", attempts.CODE_INVALID),
                            ("code", attempts.RATE_LIMITED), (None, attempts.INVALID_EMAIL)):
        _fail(db, outcome, detail=detail)
    assert login_alerts.note_failure(db, email="ada@example.edu", user_agent=CHROME) == "counted"
    assert sent == []
    assert attempts.attributable(attempts.CODE_INVALID, "wrong code") is True
    assert attempts.attributable(attempts.LINK_INVALID, "link opened (used)") is True
    # Three failures of her own still reach the operator.
    for _ in range(3):
        _fail(db, attempts.CODE_INVALID, detail="wrong code")
    assert login_alerts.note_failure(db, email="ada@example.edu", user_agent=CHROME) == "queued"


def test_a_forged_cookie_expires_as_nobody(world):
    """record_expiry checked no signature, so a cookie forged with another
    student's id wrote an expired row under their address."""
    from jose import jwt

    client, db, sent = world
    forged = jwt.encode({"sub": "1", "jti": "x", "exp": 1}, "not-the-secret", algorithm="HS256")
    client.cookies.set("session_token", forged)
    r = client.get("/chat?course=AST101", headers={"accept": "text/html", "user-agent": CHROME}, follow_redirects=False)
    assert r.status_code == 303
    (row,) = db.query(LoginAttempt).all()
    assert (row.outcome, row.email, row.student_id) == ("expired", None, None)


def test_old_failures_fall_out_of_the_window(world):
    client, db, sent = world
    _fail(db, attempts.CODE_INVALID, minutes_ago=90)
    _fail(db, attempts.CODE_INVALID, minutes_ago=70)
    _fail(db, attempts.CODE_INVALID)
    assert login_alerts.note_failure(db, email="ada@example.edu", user_agent=CHROME) == "counted"
    assert sent == []


def test_recording_a_failure_counts_it(world, monkeypatch):
    client, db, sent = world
    for _ in range(2):
        _fail(db, attempts.CODE_INVALID)
    attempts.record_values(db, method=attempts.MAGIC, outcome=attempts.CODE_INVALID, email="ada@example.edu",
                           user_agent=CHROME, detail="wrong code")
    assert len(sent) == 1 and db.query(LoginAttempt).count() == 3


# ---------------------------------------------------------------------------
# The rows that were missing
# ---------------------------------------------------------------------------

def test_a_tap_on_a_dead_link_carries_the_address_and_tells_a_scanner_apart(world):
    client, db, sent = world
    db.add(MagicLink(email="ada@example.edu", student_id=1, token="tok-used", code="123456", used=True,
                     expires_at=utcnow() + datetime.timedelta(minutes=10)))
    db.commit()
    r = client.get("/auth/magic/tok-used", headers={"user-agent": SCANNER})
    assert r.status_code == 400
    r = client.post("/auth/magic/tok-used", headers={"user-agent": CHROME})
    assert r.status_code == 400
    r = client.get("/auth/magic/never-issued", headers={"user-agent": CHROME})
    assert r.status_code == 400
    rows = db.query(LoginAttempt).order_by(LoginAttempt.id).all()
    assert [(a.outcome, a.email, a.student_id, a.detail) for a in rows] == [
        ("link_invalid", "ada@example.edu", 1, "link opened (used); scanner"),
        ("link_invalid", "ada@example.edu", 1, "link confirmed (used)"),
        ("link_invalid", None, None, "link opened (unknown)"),
    ]


def test_an_expired_session_and_a_whitelist_removal_leave_rows(world):
    client, db, sent = world
    token = login_cookie(db, 1)
    from tutor.db.models import Session as SessionModel

    sess = db.query(SessionModel).one()
    sess.expires_at = utcnow() - datetime.timedelta(days=1)
    db.commit()
    client.cookies.set("session_token", token)
    r = client.get("/chat?course=AST101", headers={"accept": "text/html", "user-agent": CHROME}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=")
    (row,) = db.query(LoginAttempt).all()
    assert (row.method, row.outcome, row.email, row.student_id) == ("session", "expired", "ada@example.edu", 1)
    assert row.user_agent == CHROME and attempts.tone(row.outcome) == "progress"

    db.delete(row)
    sess.expires_at = utcnow() + datetime.timedelta(days=1)
    db.query(WhitelistEntry).delete()
    db.commit()
    r = client.get("/chat/history?course=AST101", headers={"user-agent": CHROME})
    assert r.status_code == 401 and r.json()["detail"] == "Access removed"
    (row,) = db.query(LoginAttempt).all()
    assert (row.method, row.outcome, row.email, row.student_id) == ("session", "access_removed", "ada@example.edu", 1)
    assert sent == []                                        # lockouts are rows, not failures


def test_a_visitor_with_no_cookie_leaves_no_row(world):
    client, db, _sent = world
    r = client.get("/map", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and db.query(LoginAttempt).count() == 0


def test_the_rate_limit_banner_names_the_minute(world, monkeypatch):
    from tutor.routers import auth as auth_mod

    client, db, _sent = world
    auth_mod._magic_link_log.clear()
    for _ in range(auth_mod._MAGIC_LINK_LIMIT):
        assert auth_mod._magic_link_rate_check("testclient")
    r = client.post("/auth/magic-link", data={"email": "ada@example.edu"})
    assert r.status_code == 429
    import re

    assert re.search(r"Try again after \d{1,2}:\d{2} [AP]M", r.text), r.text[:300]
    assert "the newest code in your inbox still works" in r.text
    auth_mod._magic_link_log.clear()
