"""Public health endpoint + DB self-heal trigger.

GET /healthz: no auth, no secrets in the payload. Returns 200
{"status": "ok"} when a SELECT 1 round-trips (the pool pings a connection
before handing it out and reconnects a dropped one, so this is a true
reachability test for the Supabase database), and 503 {"status":
"degraded"} when it does not, in which case a Supabase restore is kicked
off (tutor.selfheal) and the operator is emailed (tutor.alerts, deduped).

The payload's "kb" is the retrieval store's state ("ok" when any
knowledge-base collection has documents; see tutor.health.slides_status). When it is not
"ok" the probe still answers 200, with "status": "degraded", since the
database is fine. A healthy probe also finishes a database setup that failed
at boot (tutor.startup.ensure_database_ready, plan step 2.7).

Load limits (plan step 2.6, finding C11): at most PROBE_SLOTS probes touch
the database at once, and one address gets at most PER_IP_PER_MINUTE live
probes a minute. A request past either limit is answered from the last
result (marked "cached") instead of taking another connection; with no
recent result it gets 429 (per address) or 503 (all slots busy). Both
limits live in process memory, like the magic-link limiter: single-instance
only.

Three consumers:
- the login page polls it to show a live status banner (any visitor
  landing on a dead site thereby starts the recovery),
- the db-keepalive workflow curls it on a schedule (its SELECT 1 is the
  database activity that prevents the free-tier pause),
- humans, for a quick "is it the DB?" check.

Failure details go to logs + the alert email, never the public response.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session as DBSession

from tutor import alerts, selfheal
from tutor.auth.deps import get_db

router = APIRouter(tags=["status"])
logger = logging.getLogger("tutor.status")

# Last observed DB state (per-process), so a down -> up transition can send a
# one-shot "recovered" email.
_last_db_ok: bool | None = None

PROBE_SLOTS = 2
PER_IP_PER_MINUTE = 30
CACHE_SECONDS = 300          # how long a past result may stand in for a probe

_slots = threading.BoundedSemaphore(PROBE_SLOTS)
_hits: dict[str, collections.deque] = {}
_hits_lock = threading.Lock()
_last: dict = {}             # {"at": monotonic, "status": int, "body": dict}


def _reset_for_tests() -> None:
    global _slots
    with _hits_lock:
        _hits.clear()
        _last.clear()
        _kb.clear()
    _slots = threading.BoundedSemaphore(PROBE_SLOTS)


def _ip_allowed(ip: str) -> bool:
    """At most PER_IP_PER_MINUTE live probes per address per minute."""
    now = time.monotonic()
    cutoff = now - 60
    with _hits_lock:
        for key in [k for k, dq in _hits.items() if not dq or dq[-1] < cutoff]:
            del _hits[key]
        hits = _hits.setdefault(ip, collections.deque())
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= PER_IP_PER_MINUTE:
            return False
        hits.append(now)
    return True


def _cached(status_if_none: int, label: str) -> JSONResponse:
    last = dict(_last)
    if last and time.monotonic() - last["at"] <= CACHE_SECONDS:
        return JSONResponse({**last["body"], "cached": True}, status_code=last["status"])
    return JSONResponse({"status": label}, status_code=status_if_none)


KB_CACHE_SECONDS = 60
_kb: dict = {}               # {"at": monotonic, "status": str}


def _kb_status() -> str:
    """Whether retrieval has slides to search (plan step 2.7, finding E5):
    a store left empty or unreadable by a failed seed used to fail silently,
    since find_topic swallows the errors. Checked at most once a minute."""
    now = time.monotonic()
    if _kb and now - _kb["at"] < KB_CACHE_SECONDS:
        return _kb["status"]
    from tutor.health import slides_status

    status = slides_status()
    _kb.update({"at": now, "status": status})
    return status


def _finish_setup(db: DBSession) -> None:
    """A boot that could not set the database up (it was down) finishes the
    setup here, after the first healthy probe (plan step 2.7, finding C4)."""
    from sqlalchemy.engine import Connection

    from tutor import startup

    try:
        bind = db.get_bind()
    except Exception:  # noqa: BLE001 - a session without a bind (tests)
        return
    engine = bind.engine if isinstance(bind, Connection) else bind
    if startup.database_ready(engine):
        return
    if startup.ensure_database_ready(engine):
        logger.warning("healthz: database setup finished after a degraded boot")


def _remember(status: int, body: dict) -> JSONResponse:
    _last.update({"at": time.monotonic(), "status": status, "body": body})
    return JSONResponse(body, status_code=status)


@router.get("/healthz")
def healthz(request: Request, db: DBSession = Depends(get_db)) -> JSONResponse:
    ip = request.client.host if request.client else "unknown"
    if not _ip_allowed(ip):
        return _cached(429, "throttled")
    if not _slots.acquire(blocking=False):
        return _cached(503, "busy")
    try:
        return _probe(db)
    finally:
        _slots.release()


def _probe(db: DBSession) -> JSONResponse:
    global _last_db_ok
    try:
        ok = db.execute(text("SELECT 1")).scalar() == 1
        detail = "connected" if ok else "SELECT 1 returned an unexpected value"
    except Exception as exc:  # noqa: BLE001 - any failure means "db down"
        ok = False
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"

    if ok:
        if _last_db_ok is False:
            alerts.send_alert(
                "db-recovered", "Database recovered", "healthz SELECT 1 is healthy again."
            )
        _last_db_ok = True
        _finish_setup(db)
        kb = _kb_status()
        return _remember(200, {
            # "degraded" with a 200: the database is fine (the login page and
            # the keepalive read "db"), but retrieval has nothing to search.
            "status": "ok" if kb == "ok" else "degraded",
            "db": "ok",
            "kb": kb,
            # Config presence only, no values: lets an operator confirm the
            # Render env vars took effect without reading logs.
            "sentry": bool(os.getenv("SENTRY_DSN", "").strip()),
            "selfheal": selfheal.access_status(),
        })

    logger.error("healthz: database down: %s", detail)
    restore = selfheal.restore_supabase_if_down(reason="healthz probe failed")
    alerts.send_alert(
        "db-down",
        "Database unreachable",
        f"healthz SELECT 1 failed.\n\n{detail}\n\nSelf-heal: {restore}",
    )
    _last_db_ok = False
    return _remember(503, {"status": "degraded", "db": "down", "restore": restore})
