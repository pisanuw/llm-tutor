"""Plan step 6.2 (finding F11): the sign-in flow through the real dependency.

Every route test overrode ``get_current_student``, so no 401 path ran and the
magic-link, logout, and Google routes had no test. Here only the database is
overridden: a link is asked for, followed, and its cookie used; the cookie is
then tampered with, expired, and revoked; the link is reused; and the Google
flow runs against a stand-in for Google's OAuth client.
"""
from __future__ import annotations

import datetime
import re
import types
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from jose import jwt

import tutor.config as config
from tutor.db.models import LoginAttempt, MagicLink, Session as SessionModel, Student, WhitelistEntry
from tutor.utctime import utcnow
from tests.helpers import make_session

PAGE, JSON = "/chat?course=AST101", "/chat/history?course=AST101"


@pytest.fixture
def db():
    s = make_session()
    s.add(WhitelistEntry(email="stu@example.edu", added_by="test"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db, monkeypatch):
    import tutor.main as main_mod
    from tutor.auth.deps import get_db
    from tutor.routers import auth as auth_router

    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)            # dev mode: the link is on the page
    config.get_config.cache_clear()
    auth_router._magic_link_log.clear()                                # the per-address rate limit
    main_mod.app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_mod.app, base_url="http://localhost")
    finally:
        main_mod.app.dependency_overrides.clear()
        config.get_config.cache_clear()


def _link(client, email="stu@example.edu") -> str:
    page = client.post("/auth/magic-link", data={"email": email})
    assert page.status_code == 200, page.text
    found = re.search(r"http://localhost(/auth/magic/[\w-]+)", page.text)
    assert found, "the dev-mode page shows the link"
    return found.group(1)


def _code(client, email="stu@example.edu") -> str:
    page = client.post("/auth/magic-link", data={"email": email})
    found = re.search(r"the code is (\d{6})", page.text)
    assert found, "the dev-mode page shows the code"
    return found.group(1)


def _sign_in(client) -> str:
    link = _link(client)
    page = client.get(link)                                              # the confirm page
    assert page.status_code == 200 and "Continue" in page.text
    assert "session_token" not in client.cookies                         # opening spends nothing
    resp = client.post(link, follow_redirects=False)                     # the Continue button
    assert resp.status_code == 303 and resp.headers["location"] == "/chat"
    return client.cookies["session_token"]


def _trail(db) -> list[str]:
    return [a.outcome for a in db.query(LoginAttempt).order_by(LoginAttempt.id)]


# ---------------------------------------------------------------------------
# The magic link, the cookie, and the real get_current_student
# ---------------------------------------------------------------------------

def test_a_magic_link_signs_the_student_in(client, db):
    assert client.get(JSON).status_code == 401                          # no cookie yet
    assert db.query(Student).count() == 0                               # asking made no student
    _sign_in(client)
    assert client.get(JSON).status_code == 200
    assert client.get(PAGE).status_code == 200
    student = db.query(Student).one()
    assert student.email == "stu@example.edu"
    assert db.query(SessionModel).filter_by(student_id=student.id, revoked=False).count() == 1
    assert _trail(db) == ["link_shown", "link_opened", "ok"]
    ok = db.query(LoginAttempt).filter_by(outcome="ok").one()
    assert (ok.email, ok.student_id, ok.method, ok.host) == ("stu@example.edu", student.id, "magic", "localhost")
    assert ok.user_agent and ok.detail == "/chat"


def test_a_rostered_student_logs_into_the_row_the_roster_made(client, db):
    """A pre-registered roster row (classes._pre_register) must be adopted by the
    first login, however the student types their address, never duplicated:
    students.email is UNIQUE but case-sensitive, so the login path's .lower()
    is what makes the exact-match lookup in magic_link_login find the row."""
    from tutor import classes as classes_mod
    from tutor.db.models import ClassEnrollment, CourseClass

    db.add(Student(id=1, email="prof@example.edu", name="Prof"))
    db.commit()
    c = classes_mod.create_class(db, name="AST 101 A", course="AST101",
                                instructor_email="prof@example.edu", term="Autumn 2026")
    assert classes_mod.enroll(db, c.id, "stu@example.edu") is True
    rostered = db.query(Student).filter_by(email="stu@example.edu").one()
    assert rostered.name == "" and rostered.login_count == 0

    # The student types their address with different capitalisation.
    resp = client.post(_link(client, "STU@Example.EDU"), follow_redirects=False)
    assert resp.status_code == 303

    student = db.query(Student).filter_by(email="stu@example.edu").one()   # still exactly one
    assert student.id == rostered.id
    assert student.login_count == 1 and student.last_login is not None
    assert student.name == "stu"                                     # blank name filled in
    # The enrollment still points at the student who just logged in.
    assert db.query(ClassEnrollment).filter_by(
        class_id=c.id, student_id=student.id).count() == 1
    assert db.query(CourseClass).count() == 1


def test_a_link_works_once(client, db):
    link = _link(client)
    assert client.post(link, follow_redirects=False).status_code == 303
    client.cookies.clear()
    again = client.post(link, follow_redirects=False)
    assert again.status_code == 400 and "session_token" not in client.cookies
    assert client.get(link).status_code == 400                           # the page says so too
    assert _trail(db)[-2:] == ["link_invalid", "link_invalid"]


def test_opening_the_link_spends_nothing(client, db):
    """Some university mail systems rewrite every link through a URL scanner,
    which fetches it on delivery. Until 2026-10-02 that GET signed the scanner in
    and burnt the one-time link; the student's own tap then answered 400."""
    link = _link(client)
    scanner = {"user-agent": "Mozilla/5.0 (compatible; URL scanner)"}
    for _ in range(3):             # on delivery, a preview, the student's own tap
        page = client.get(link, headers=scanner)
        assert page.status_code == 200 and "session_token" not in client.cookies
    assert db.query(SessionModel).count() == 0
    assert db.query(MagicLink).one().used is False
    resp = client.post(link, follow_redirects=False)                     # Continue
    assert resp.status_code == 303 and "session_token" in client.cookies
    rows = db.query(LoginAttempt).order_by(LoginAttempt.id).all()
    assert [r.outcome for r in rows] == ["link_shown"] + ["link_opened"] * 3 + ["ok"]
    assert {r.user_agent for r in rows[1:4]} == {scanner["user-agent"]}   # the admin can tell


def test_an_expired_link_is_refused(client, db):
    link = _link(client)
    db.query(MagicLink).update({MagicLink.expires_at: utcnow() - datetime.timedelta(minutes=1)})
    db.commit()
    assert client.get(link).status_code == 400
    assert client.post(link, follow_redirects=False).status_code == 400


# ---------------------------------------------------------------------------
# The 6-digit code: the sign-in a mail scanner cannot spend
# ---------------------------------------------------------------------------

def test_the_code_signs_the_student_in(client, db):
    code = _code(client)
    resp = client.post("/auth/magic-code", follow_redirects=False,
                       data={"email": "STU@example.edu ", "code": f"{code[:3]} {code[3:]}"})
    assert resp.status_code == 303 and resp.headers["location"] == "/chat"
    assert client.get(JSON).status_code == 200
    assert db.query(MagicLink).one().used is True
    assert db.query(Student).one().email == "stu@example.edu"
    # A second press from the browser the first signed in goes on
    # (finding R32); from anywhere else the
    # code is spent.
    again = client.post("/auth/magic-code", data={"email": "stu@example.edu", "code": code},
                        follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/chat"
    client.cookies.clear()
    again = client.post("/auth/magic-code", data={"email": "stu@example.edu", "code": code},
                        follow_redirects=False)
    assert again.status_code == 400 and "already used" in again.text      # once only, and said so
    assert _trail(db) == ["link_shown", "ok", "link_invalid"]


def test_the_code_returns_to_the_requested_page(client):
    page = client.post("/auth/magic-link", data={"email": "stu@example.edu", "next": "/map"})
    assert '<input type="hidden" name="next" value="/map">' in page.text
    code = re.search(r"the code is (\d{6})", page.text).group(1)
    resp = client.post("/auth/magic-code", follow_redirects=False,
                       data={"email": "stu@example.edu", "code": code, "next": "/map"})
    assert resp.status_code == 303 and resp.headers["location"] == "/map"


def test_a_wrong_code_counts_and_five_kill_the_link(client, db):
    code = _code(client)
    wrong = "000000" if code != "000000" else "111111"
    for n in range(1, 6):
        r = client.post("/auth/magic-code", data={"email": "stu@example.edu", "code": wrong},
                        follow_redirects=False)
        assert r.status_code == 400 and "did not match" in r.text
        db.expire_all()
        assert db.query(MagicLink).one().code_attempts == n
    r = client.post("/auth/magic-code", data={"email": "stu@example.edu", "code": code},
                    follow_redirects=False)
    assert r.status_code == 400 and "session_token" not in client.cookies
    assert db.query(LoginAttempt).filter_by(outcome="code_invalid").count() == 6
    assert client.post(_link(client), follow_redirects=False).status_code == 303   # a new email works


def test_a_code_needs_a_live_link_for_that_address(client, db):
    code = _code(client)
    r = client.post("/auth/magic-code", data={"email": "other@example.edu", "code": code},
                    follow_redirects=False)
    assert r.status_code == 400 and "session_token" not in client.cookies
    r = client.post("/auth/magic-code", data={"email": "stu@example.edu", "code": "12"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert db.query(LoginAttempt).filter_by(outcome="code_invalid").count() == 2


def test_the_code_form_follows_the_request(client):
    page = client.post("/auth/magic-link", data={"email": "stu@example.edu"})
    assert 'action="/auth/magic-code"' in page.text and 'value="stu@example.edu"' in page.text
    assert 'action="/auth/magic-code"' in client.get("/auth/login?code=1").text
    plain = client.get("/auth/login").text
    assert 'action="/auth/magic-link"' in plain and "Already have a code?" in plain


# ---------------------------------------------------------------------------
# The email: who sent it, why, what to do when unexpected, whom to ask
# ---------------------------------------------------------------------------

def _with_resend(monkeypatch, send):
    from tutor.routers import auth as auth_router

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("FROM_EMAIL", "tutor@example.edu")
    config.get_config.cache_clear()
    monkeypatch.setattr(auth_router.resend.Emails, "send", send)


def test_the_email_says_who_why_and_whom_to_ask(client, monkeypatch, db):
    sent: list[dict] = []
    _with_resend(monkeypatch, lambda payload: sent.append(payload))
    page = client.post("/auth/magic-link", data={"email": "stu@example.edu"})
    assert page.status_code == 200 and "/auth/magic/" not in page.text    # emailed, not shown
    assert "the code is" not in page.text
    (mail,) = sent
    link = db.query(MagicLink).one()
    minutes = config.get_config()["magic_link_expiry_minutes"]
    assert (mail["from"], mail["to"], mail["reply_to"]) == ("tutor@example.edu", "stu@example.edu", "prof@example.edu")
    assert mail["subject"] == f"{link.code} is your Astra sign-in code"
    for body in (mail["text"], mail["html"]):
        assert link.code[:3] in body and f"/auth/magic/{link.token}" in body
        assert "localhost" in body                                        # where it came from
        flat = " ".join(body.split())                                    # the text is wrapped at 72
        assert "Dr. Alex Morgan" in flat and "Example College" in flat         # who sent it
        assert "Did not ask for this?" in body                            # when unexpected
        assert "prof@example.edu" in body                                      # whom to contact
        assert f"{minutes} minutes" in body and "Continue" in body
    assert _trail(db) == ["link_sent"]


def test_a_failed_send_is_told_and_recorded(client, monkeypatch, db):
    def boom(payload):
        raise RuntimeError("provider down")

    _with_resend(monkeypatch, boom)
    page = client.post("/auth/magic-link", data={"email": "stu@example.edu"})
    assert page.status_code == 200
    assert "Email delivery failed" in page.text and "prof@example.edu" in page.text
    assert 'action="/auth/magic-link"' in page.text                       # ask again, not a code
    row = db.query(LoginAttempt).filter_by(outcome="email_failed").one()
    assert (row.email, row.detail) == ("stu@example.edu", "RuntimeError")


def test_an_address_off_the_whitelist_gets_no_link(client, db):
    from tutor.auth.access import set_whitelist_enabled

    set_whitelist_enabled(db, True)
    page = client.post("/auth/magic-link", data={"email": "stranger@example.edu"})
    assert "/auth/magic/" not in page.text
    assert db.query(MagicLink).count() == 0
    denied = db.query(LoginAttempt).one()
    assert (denied.method, denied.outcome, denied.email) == ("magic", "denied", "stranger@example.edu")


def test_a_tampered_cookie_is_refused(client):
    token = _sign_in(client)
    head, body, sig = token.split(".")
    client.cookies.set("session_token", f"{head}.{body}.{sig[:-2]}xx")
    assert client.get(JSON).status_code == 401
    page = client.get(PAGE, headers={"accept": "text/html"}, follow_redirects=False)   # a browser
    assert page.status_code == 303 and page.headers["location"].startswith("/auth/login?next=")


def test_a_cookie_signed_with_another_key_is_refused(client, db):
    _sign_in(client)
    sess = db.query(SessionModel).one()
    forged = jwt.encode({"sub": str(sess.student_id), "jti": sess.jwt_jti,
                         "exp": utcnow() + datetime.timedelta(days=1)}, "not-the-key", algorithm="HS256")
    client.cookies.set("session_token", forged)
    assert client.get(JSON).status_code == 401


def test_an_expired_session_is_refused(client, db):
    _sign_in(client)
    db.query(SessionModel).update({SessionModel.expires_at: utcnow() - datetime.timedelta(seconds=1)})
    db.commit()
    assert client.get(JSON).status_code == 401


def test_an_expired_token_is_refused(client, db, monkeypatch):
    from tutor.auth import jwt_utils

    _sign_in(client)
    sess = db.query(SessionModel).one()
    old = jwt.encode({"sub": str(sess.student_id), "jti": sess.jwt_jti,
                      "exp": utcnow() - datetime.timedelta(minutes=1)},
                     config.get_config()["secret_key"], algorithm=jwt_utils.ALGORITHM)
    client.cookies.set("session_token", old)
    assert client.get(JSON).status_code == 401


def test_logout_revokes_the_session_and_clears_the_cookie(client, db):
    token = _sign_in(client)
    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/auth/login"
    assert any(c.startswith('session_token=""') or c.startswith("session_token=;")
               for c in resp.headers.get_list("set-cookie"))
    db.expire_all()
    assert db.query(SessionModel).one().revoked is True
    client.cookies.set("session_token", token)                          # the old cookie, replayed
    assert client.get(JSON).status_code == 401


def test_the_auth_root_sends_you_to_the_login_page(client):
    resp = client.get("/auth/", follow_redirects=False)
    assert resp.status_code in (302, 307) and resp.headers["location"] == "/auth/login"


def test_jwt_round_trip():
    from tutor.auth.jwt_utils import create_token, decode_token

    token, jti, expires = create_token(7)
    payload = decode_token(token)
    assert payload["sub"] == "7" and payload["jti"] == jti
    assert expires > utcnow() + datetime.timedelta(days=config.get_config()["jwt_expiry_days"] - 1)


# ---------------------------------------------------------------------------
# Google OAuth, against a stand-in for Google's client
# ---------------------------------------------------------------------------

def _fake_oauth(userinfo: dict):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def create_authorization_url(self, url):
            return f"{url}?client_id={self.kwargs['client_id']}&state=st-123", "st-123"

        async def fetch_token(self, url, code):
            assert code == "the-code"

        async def get(self, url):
            return types.SimpleNamespace(json=lambda: userinfo)

    return FakeClient


@pytest.fixture
def google(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    config.get_config.cache_clear()
    return client


def _start(google, monkeypatch, userinfo) -> str:
    from tutor.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "AsyncOAuth2Client", _fake_oauth(userinfo))
    resp = google.get("/auth/google?next=/review?course=AST101", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith(auth_router.GOOGLE_AUTH_URL)
    return parse_qs(urlsplit(location).query)["state"][0]


def test_google_is_off_without_a_client_id(client):
    assert client.get("/auth/google", follow_redirects=False).status_code == 501


def test_google_signs_in_a_verified_allowed_address(google, monkeypatch, db):
    info = {"email": "Stu@example.edu", "email_verified": True, "name": "Stu", "sub": "g-1"}
    state = _start(google, monkeypatch, info)
    resp = google.get(f"/auth/google/callback?code=the-code&state={state}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/review?course=AST101"
    assert google.get(JSON).status_code == 200
    assert db.query(Student).one().email == "stu@example.edu"
    assert _trail(db) == ["started", "ok"]


def test_google_errors_land_on_the_login_page_and_in_the_trail(google, monkeypatch, db):
    """Google sends ?error= when the student cancels or their Workspace
    forbids the app; it used to be a 422 for the missing code."""
    state = _start(google, monkeypatch, {"email": "stu@example.edu", "email_verified": True})
    resp = google.get(f"/auth/google/callback?error=admin_policy_enforced&state={state}",
                      follow_redirects=False)
    assert resp.status_code == 400 and "admin_policy_enforced" in resp.text
    assert "session_token" not in google.cookies
    assert _trail(db) == ["started", "oauth_error"]
    assert db.query(LoginAttempt).filter_by(outcome="oauth_error").one().detail == "admin_policy_enforced"


def test_a_failed_token_exchange_is_a_page_not_a_500(google, monkeypatch, db):
    from tutor.routers import auth as auth_router

    class Broken(_fake_oauth({})):
        async def fetch_token(self, url, code):
            raise RuntimeError("token endpoint down")

    state = _start(google, monkeypatch, {"email": "stu@example.edu", "email_verified": True})
    monkeypatch.setattr(auth_router, "AsyncOAuth2Client", Broken)
    resp = google.get(f"/auth/google/callback?code=the-code&state={state}", follow_redirects=False)
    assert resp.status_code == 502 and "session_token" not in google.cookies
    assert "Google sign-in failed" in resp.text
    assert db.query(LoginAttempt).filter_by(outcome="oauth_error").one().detail == "RuntimeError"


def test_google_refuses_a_forged_state(google, monkeypatch):
    _start(google, monkeypatch, {"email": "stu@example.edu", "email_verified": True})
    resp = google.get("/auth/google/callback?code=the-code&state=forged", follow_redirects=False)
    assert resp.status_code == 400 and "session_token" not in google.cookies
    assert "lost the sign-in" in resp.text


def test_google_refuses_an_unverified_email(google, monkeypatch, db):
    state = _start(google, monkeypatch, {"email": "stu@example.edu", "email_verified": False})
    resp = google.get(f"/auth/google/callback?code=the-code&state={state}", follow_redirects=False)
    assert resp.status_code == 400 and "session_token" not in google.cookies
    assert db.query(LoginAttempt).filter_by(outcome="unverified_email").one().email == "stu@example.edu"


def test_google_denies_an_address_off_the_whitelist(google, monkeypatch, db):
    from tutor.auth.access import set_whitelist_enabled

    set_whitelist_enabled(db, True)
    state = _start(google, monkeypatch, {"email": "stranger@example.edu", "email_verified": True})
    resp = google.get(f"/auth/google/callback?code=the-code&state={state}", follow_redirects=False)
    assert "session_token" not in google.cookies and resp.status_code in (200, 403)
    assert db.query(Student).filter_by(email="stranger@example.edu").count() == 0
    assert _trail(db) == ["started", "denied"]
