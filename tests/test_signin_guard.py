"""The on-page sign-in link is local development only.

Without an email provider (RESEND_API_KEY), the magic-link page used to show
the link and the code to whoever typed the address, and admins pass the
whitelist, so on a deployed site anyone who knew an admin's address could sign
in as that admin. Beside a real database (DATABASE_URL) the page now refuses
instead, and says how to get in.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor.db.models import LoginAttempt, MagicLink
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    yield s
    s.close()


@pytest.fixture
def client(db, monkeypatch):
    import tutor.main as main_mod
    from tutor.auth.deps import get_db
    from tutor.routers import auth as auth_router

    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    config.get_config.cache_clear()
    auth_router._magic_link_log.clear()
    main_mod.app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_mod.app, base_url="http://localhost")
    finally:
        main_mod.app.dependency_overrides.clear()
        config.get_config.cache_clear()


def test_local_development_shows_the_link(client):
    page = client.post("/auth/magic-link", data={"email": "prof@example.edu"})
    assert page.status_code == 200
    assert "/auth/magic/" in page.text and "dev mode" in page.text


def test_a_deployed_site_without_email_never_shows_the_link_or_code(client, db, monkeypatch):
    # Only the check reads DATABASE_URL here; the request still uses the test session.
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/tutor")
    page = client.post("/auth/magic-link", data={"email": "prof@example.edu"})
    assert page.status_code == 200
    assert "/auth/magic/" not in page.text
    assert "the code is" not in page.text
    assert "Email sign-in is not set up on this site yet" in page.text
    attempt = db.query(LoginAttempt).order_by(LoginAttempt.id.desc()).first()
    assert attempt is not None and attempt.detail == "no email provider"
    # The link row exists but nobody saw it: it is useless without the email.
    assert db.query(MagicLink).count() == 1
