"""Automatic Supabase restore when the production database is down.

Supabase's Free Plan pauses a project after ~7 days without database
activity; a paused DB turns every DB-touching request into a 500 (the
2026-09-01 login outage: both Google and magic-link auth 500'd at their
first query). The scheduled db-keepalive workflow prevents the pause;
this module is the second line of defence: when the app itself observes
the DB down (/healthz probe, or an unhandled DB error in any request),
it calls the Supabase Management API restore endpoint so recovery starts
without an operator.

Requires SUPABASE_ACCESS_TOKEN + SUPABASE_PROJECT_REF in the app env
(the same values as the GitHub Actions secrets; see render.yaml and
DEPLOY.md). Unset means the module reports "unconfigured" and does
nothing, so the feature degrades cleanly.

A 10-minute in-memory cooldown (single-instance trade-off, same as the
magic-link limiter in routers/auth.py) means anonymous visitors hitting
/healthz cannot hammer the Management API. Restore takes ~2-3 minutes;
callers should report "recovery started" rather than wait.
"""

from __future__ import annotations

import datetime
import logging
import threading

import httpx

from tutor.config import get_config
from tutor.utctime import utcnow

logger = logging.getLogger("tutor.selfheal")

_API = "https://api.supabase.com/v1/projects"
_COOLDOWN = datetime.timedelta(minutes=10)

_lock = threading.Lock()
_last_attempt: datetime.datetime | None = None


def _reset_for_tests() -> None:
    global _access_status
    _access_status = "unchecked"
    global _last_attempt
    with _lock:
        _last_attempt = None


def _supabase_config() -> tuple[str, str]:
    cfg = get_config()
    return cfg["supabase_access_token"], cfg["supabase_project_ref"]


def _get_status(token: str, ref: str) -> str:
    r = httpx.get(
        f"{_API}/{ref}", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    r.raise_for_status()
    return str(r.json().get("status", ""))


def _post_restore(token: str, ref: str) -> None:
    r = httpx.post(
        f"{_API}/{ref}/restore",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()


_access_status = "unchecked"


def check_access() -> str:
    """Verify the self-heal credentials once (called at boot) without pausing
    anything: a read-only GET of the project via the Management API. Result is
    cached for /healthz so a mis-set token or ref is visible without waiting for
    a real outage to find out. Never raises.

    Values: "unconfigured", "ok:<project status>", "unauthorized",
    "not_found", "error:<ExceptionType>".
    """
    global _access_status
    token, ref = _supabase_config()
    if not token or not ref:
        _access_status = "unconfigured"
        return _access_status
    try:
        _access_status = f"ok:{_get_status(token, ref) or 'unknown'}"
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        _access_status = "unauthorized" if code in (401, 403) else "not_found" if code == 404 else f"error:HTTP{code}"
    except Exception as exc:  # noqa: BLE001 - boot must not fail on a probe
        _access_status = f"error:{type(exc).__name__}"
    logger.info("self-heal access check: %s", _access_status)
    return _access_status


def access_status() -> str:
    return _access_status


def restore_supabase_if_down(reason: str = "") -> str:
    """Kick off a Supabase restore if the project is not active.

    Returns "initiated" | "active" (Supabase says healthy, so the outage is
    elsewhere) | "cooldown" | "unconfigured" | "error". Never raises.
    """
    global _last_attempt
    token, ref = _supabase_config()
    if not token or not ref:
        return "unconfigured"
    now = utcnow()
    with _lock:
        if _last_attempt is not None and now - _last_attempt < _COOLDOWN:
            return "cooldown"
        _last_attempt = now
    try:
        status = _get_status(token, ref)
        if status == "ACTIVE_HEALTHY":
            logger.warning(
                "selfheal: DB observed down (%s) but Supabase reports "
                "ACTIVE_HEALTHY; not restoring",
                reason,
            )
            return "active"
        logger.warning(
            "selfheal: Supabase status=%s (%s); requesting restore", status, reason
        )
        _post_restore(token, ref)
        return "initiated"
    except Exception as exc:  # noqa: BLE001 - self-heal must never raise
        logger.error(
            "selfheal: restore attempt failed: %s: %s", type(exc).__name__, exc
        )
        return "error"
