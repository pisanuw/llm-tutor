"""Plan step 4.5 (findings C17, C18, C19, C20, D30): admin and security edges.

C17: ``/admin?token=`` put the bearer in URLs, logs, and history, then kept
the raw token in a week-long cookie with no revocation. C18: the API docs
were public. C19: logging out was a GET, and the OAuth session cookie was
never Secure. C20: no security headers. D30: the admin 403 advertised the
token trick.
Done when: headers present on every response; no bearer in any URL.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor.db.models import Session as SessionModel, Student
from tests.helpers import make_session

TOKEN = "tkn-edge-test"
HEADERS = ("x-content-type-options", "referrer-policy", "x-frame-options",
           "content-security-policy-report-only")


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session()
    db.add(Student(id=1, email="kim@example.edu", name="Kim"))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, base_url="http://localhost")    # localhost: cookies are not Secure
    try:
        yield client, db
    finally:
        app.dependency_overrides.clear()
        db.close()
        config.get_config.cache_clear()


def _has_headers(resp) -> bool:
    return all(h in resp.headers for h in HEADERS)


@pytest.mark.parametrize("path", ["/auth/login", "/healthz", "/static/vendor/tailwind.css",
                                  "/no-such-page", "/admin", "/admin/login", "/", "/help"])
def test_every_response_carries_the_security_headers(setup, path):
    client, _db = setup
    resp = client.get(path, follow_redirects=False)
    assert _has_headers(resp), (path, resp.status_code, dict(resp.headers))
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"


def test_a_500_carries_them_too():
    from starlette.requests import Request

    import tutor.main as main

    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})
    resp = asyncio.run(main._alert_on_unhandled(request, RuntimeError("boom")))
    assert resp.status_code == 500 and _has_headers(resp)


def test_the_api_docs_are_off(setup):
    client, _db = setup
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_a_token_in_the_url_is_not_honoured(setup):
    client, _db = setup
    resp = client.get(f"/admin?token={TOKEN}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert TOKEN not in resp.headers["location"] and "set-cookie" not in resp.headers
    # The old raw-token cookie opens nothing either: off to the login page.
    client.cookies.set("admin_token", TOKEN)
    bounced = client.get("/admin", follow_redirects=False)
    assert bounced.status_code == 303 and bounced.headers["location"] == "/auth/login?next=/admin"


def test_token_sign_in_is_a_post_and_the_cookie_is_not_the_token(setup):
    client, _db = setup
    wrong = client.post("/admin/login", data={"token": "nope"}, follow_redirects=False)
    assert wrong.status_code == 403 and "That token is not right." in wrong.text
    resp = client.post("/admin/login", data={"token": TOKEN}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("admin_session=") and TOKEN not in cookie
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert client.get("/admin").status_code == 200


def test_changing_the_token_ends_token_sessions(setup, monkeypatch):
    client, _db = setup
    client.post("/admin/login", data={"token": TOKEN})
    assert client.get("/admin").status_code == 200
    monkeypatch.setenv("ADMIN_TOKEN", "a-new-token")
    config.get_config.cache_clear()
    bounced = client.get("/admin", follow_redirects=False)
    assert bounced.status_code == 303 and bounced.headers["location"] == "/auth/login?next=/admin"


def test_the_header_token_still_serves_scripts(setup):
    client, _db = setup
    assert client.get("/admin/api/students").status_code == 403
    assert client.get("/admin/api/students", headers={"X-Admin-Token": TOKEN}).status_code == 200


def test_the_denial_does_not_advertise_the_token(setup):
    """A visitor is sent to the login page with a way back (2026-10-02: the
    403 page's own 'Sign in' link looped an instructor); still no word of
    the token mechanism anywhere in the answer."""
    client, _db = setup
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/auth/login?next=/admin"
    assert "token" not in resp.text.lower() and "token" not in resp.headers["location"]


def test_no_link_or_page_carries_a_token():
    root = pathlib.Path(__file__).resolve().parent.parent
    for path in list((root / "tutor").rglob("*.py")) + list((root / "tutor").rglob("*.html")):
        assert not re.search(r"[?&]token=", path.read_text()), path


def test_logout_is_a_post(setup):
    client, db = setup
    from tutor.auth.jwt_utils import create_token

    token, jti, expires = create_token(1)
    db.add(SessionModel(student_id=1, jwt_jti=jti, expires_at=expires, method="magic"))
    db.commit()
    client.cookies.set("session_token", token)
    page = client.get("/auth/logout")
    assert page.status_code == 200 and '<form method="post" action="/auth/logout">' in page.text
    assert db.query(SessionModel).one().revoked is False         # a GET changes nothing
    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/auth/login"
    db.expire_all()
    assert db.query(SessionModel).one().revoked is True
    cookies = resp.headers.get_list("set-cookie")
    assert any(c.startswith("session_token=") for c in cookies)
    assert any(c.startswith("admin_session=") for c in cookies)


def test_the_oauth_session_cookie_is_secure_behind_https():
    """The flag follows BASE_URL: https in production, http for local work."""
    from starlette.middleware.sessions import SessionMiddleware

    import tutor.main as main

    options = [m.kwargs for m in main.app.user_middleware if m.cls is SessionMiddleware][0]
    assert options["https_only"] is main._cfg["base_url"].startswith("https://")
    source = pathlib.Path(main.__file__).read_text()
    assert 'https_only=_cfg["base_url"].startswith("https://")' in source
