"""Tests for /healthz, the admin-alert dedup/caps, and the Supabase
self-heal cooldown (follow-up to the 2026-09-01 paused-DB login outage)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tutor import alerts, selfheal
from tests.helpers import make_session


@pytest.fixture(autouse=True)
def _fresh_state():
    """Cooldown/dedup/throttle state is module-global; isolate every test."""
    from tutor.routers import status as status_router

    alerts._reset_for_tests()
    selfheal._reset_for_tests()
    status_router._reset_for_tests()
    yield
    alerts._reset_for_tests()
    selfheal._reset_for_tests()
    status_router._reset_for_tests()


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

def test_healthz_ok(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers import status as status_router

    monkeypatch.setattr(status_router, "_kb_status", lambda: "ok")
    session = make_session()
    app.dependency_overrides[get_db] = lambda: session
    try:
        r = TestClient(app).get("/healthz")
    finally:
        app.dependency_overrides.clear()
        session.close()
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] == "ok" and body["kb"] == "ok"
    # Config-presence flags ride along (values are booleans/labels, never secrets).
    assert set(body) == {"status", "db", "kb", "sentry", "selfheal"}


class _BrokenSession:
    """A DB session whose first query fails, as a paused Supabase does."""

    def execute(self, *args, **kwargs):
        raise RuntimeError("connection refused (simulated paused database)")

    def close(self):
        pass


def test_healthz_down_triggers_selfheal_and_alert(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app

    seen = {}

    def fake_restore(reason=""):
        seen["restore_reason"] = reason
        return "initiated"

    def fake_alert(key, subject, body, **kw):
        seen["alert_key"] = key
        return "queued"

    monkeypatch.setattr(selfheal, "restore_supabase_if_down", fake_restore)
    monkeypatch.setattr(alerts, "send_alert", fake_alert)
    app.dependency_overrides[get_db] = lambda: _BrokenSession()
    try:
        r = TestClient(app).get("/healthz")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "db": "down", "restore": "initiated"}
    assert seen["alert_key"] == "db-down"
    assert seen["restore_reason"] == "healthz probe failed"


def test_healthz_recovery_sends_one_shot_alert(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers import status as status_router

    keys = []
    monkeypatch.setattr(
        selfheal, "restore_supabase_if_down", lambda reason="": "unconfigured"
    )
    monkeypatch.setattr(
        alerts, "send_alert", lambda key, *a, **kw: keys.append(key) or "queued"
    )

    session = make_session()
    client = TestClient(app)

    app.dependency_overrides[get_db] = lambda: _BrokenSession()
    try:
        assert client.get("/healthz").status_code == 503
        app.dependency_overrides[get_db] = lambda: session
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
    finally:
        app.dependency_overrides.clear()
        session.close()
        status_router._last_db_ok = None
    # Exactly one down alert, then exactly one recovery alert.
    assert keys == ["db-down", "db-recovered"]


# ---------------------------------------------------------------------------
# alerts: dedup + caps
# ---------------------------------------------------------------------------

def test_alert_dedup_per_key(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_resend_config", lambda: ("key", "f@x", ("a@x",)))
    monkeypatch.setattr(
        alerts, "_deliver_safe", lambda *a: sent.append(a[3]) or "sent"
    )
    assert alerts.send_alert("k1", "subj", "body", background=False) == "sent"
    assert alerts.send_alert("k1", "subj", "body", background=False) == "cooldown"
    assert alerts.send_alert("k2", "subj", "body", background=False) == "sent"
    assert len(sent) == 2


def test_alert_disabled_without_resend(monkeypatch):
    monkeypatch.setattr(alerts, "_resend_config", lambda: ("", "f@x", ()))
    assert alerts.send_alert("k", "subj", "body", background=False) == "disabled"


def test_alert_global_cap(monkeypatch):
    monkeypatch.setattr(alerts, "_resend_config", lambda: ("key", "f@x", ("a@x",)))
    monkeypatch.setattr(alerts, "_deliver_safe", lambda *a: "sent")
    for i in range(alerts._GLOBAL_CAP_PER_HOUR):
        assert alerts.send_alert(f"key-{i}", "s", "b", background=False) == "sent"
    assert alerts.send_alert("one-more", "s", "b", background=False) == "capped"


def test_alert_exception_uses_traceback(monkeypatch):
    captured = {}

    def fake_send(key, subject, body, **kw):
        captured.update(key=key, subject=subject, body=body)
        return "sent"

    monkeypatch.setattr(alerts, "send_alert", fake_send)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        alerts.alert_exception("exc:ValueError:/x", "/x", exc, note="context")
    assert captured["key"] == "exc:ValueError:/x"
    assert "ValueError on /x" in captured["subject"]
    assert "context" in captured["body"] and "boom" in captured["body"]


# ---------------------------------------------------------------------------
# selfheal: config gate + cooldown
# ---------------------------------------------------------------------------

def test_selfheal_unconfigured(monkeypatch):
    monkeypatch.setattr(selfheal, "_supabase_config", lambda: ("", ""))
    assert selfheal.restore_supabase_if_down("x") == "unconfigured"


def test_selfheal_restores_then_cools_down(monkeypatch):
    monkeypatch.setattr(selfheal, "_supabase_config", lambda: ("tok", "ref"))
    monkeypatch.setattr(selfheal, "_get_status", lambda t, r: "INACTIVE")
    posts = []
    monkeypatch.setattr(selfheal, "_post_restore", lambda t, r: posts.append(r))
    assert selfheal.restore_supabase_if_down("first") == "initiated"
    assert selfheal.restore_supabase_if_down("second") == "cooldown"
    assert posts == ["ref"]


def test_selfheal_skips_restore_when_active(monkeypatch):
    monkeypatch.setattr(selfheal, "_supabase_config", lambda: ("tok", "ref"))
    monkeypatch.setattr(selfheal, "_get_status", lambda t, r: "ACTIVE_HEALTHY")
    monkeypatch.setattr(
        selfheal,
        "_post_restore",
        lambda t, r: pytest.fail("must not restore an active project"),
    )
    assert selfheal.restore_supabase_if_down("x") == "active"


def test_selfheal_error_is_contained(monkeypatch):
    def boom(t, r):
        raise RuntimeError("management api down")

    monkeypatch.setattr(selfheal, "_supabase_config", lambda: ("tok", "ref"))
    monkeypatch.setattr(selfheal, "_get_status", boom)
    assert selfheal.restore_supabase_if_down("x") == "error"


# ---------------------------------------------------------------------------
# Config flags on /healthz + boot-time self-heal access probe
# ---------------------------------------------------------------------------

def test_healthz_reports_config_flags(monkeypatch, tmp_path):
    import tutor.config as config
    from tutor.main import app
    from tutor.auth.deps import get_db

    selfheal._reset_for_tests()
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    # The lifespan runs for real here (init_db, ensure_schema, ensure_rls,
    # seeds) on app_engine(), not on the get_db override below. Pin it to a
    # throwaway SQLite file so it can never reach a DATABASE_URL.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TUTOR_DB", str(tmp_path / "lifespan.db"))
    config.get_config.cache_clear()
    session = make_session()
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as client:
            body = client.get("/healthz").json()
            assert body["sentry"] is False
            # Lifespan ran check_access(); test env has no token -> unconfigured.
            assert body["selfheal"] == "unconfigured"
            monkeypatch.setenv("SENTRY_DSN", "https://k@o1.ingest.sentry.io/1")
            assert client.get("/healthz").json()["sentry"] is True
    finally:
        app.dependency_overrides.clear()
        session.close()
        config.get_config.cache_clear()


def test_check_access_classifies_outcomes(monkeypatch):
    import httpx

    selfheal._reset_for_tests()
    monkeypatch.setattr(selfheal, "_supabase_config", lambda: ("sbp_x", "refx"))

    monkeypatch.setattr(selfheal, "_get_status", lambda t, r: "ACTIVE_HEALTHY")
    assert selfheal.check_access() == "ok:ACTIVE_HEALTHY"
    assert selfheal.access_status() == "ok:ACTIVE_HEALTHY"

    def _raise(code):
        def _f(t, r):
            resp = httpx.Response(code, request=httpx.Request("GET", "https://api.supabase.com/v1/projects/refx"))
            raise httpx.HTTPStatusError("boom", request=resp.request, response=resp)
        return _f

    monkeypatch.setattr(selfheal, "_get_status", _raise(401))
    assert selfheal.check_access() == "unauthorized"
    monkeypatch.setattr(selfheal, "_get_status", _raise(404))
    assert selfheal.check_access() == "not_found"
    monkeypatch.setattr(selfheal, "_get_status", _raise(500))
    assert selfheal.check_access() == "error:HTTP500"

    def _timeout(t, r):
        raise httpx.ConnectTimeout("slow")
    monkeypatch.setattr(selfheal, "_get_status", _timeout)
    assert selfheal.check_access() == "error:ConnectTimeout"

    monkeypatch.setattr(selfheal, "_supabase_config", lambda: ("", ""))
    assert selfheal.check_access() == "unconfigured"
