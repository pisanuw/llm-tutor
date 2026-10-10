"""Admin email alerts for system errors (Resend), with dedup + rate caps.

One job: when something breaks in production, email the operator once,
not zero times (silent outage, the 2026-09-01 login incident) and not
four hundred times (error storm).

Design:
- Per-key cooldown (ALERT_COOLDOWN_MINUTES, default 60): the same *kind*
  of problem alerts at most once per window. Keys are chosen by callers
  ("db-down", "exc:<Type>:<path>", ...).
- Global cap (20/hour): a burst of *distinct* keys still cannot flood
  the inbox; suppressed alerts are logged instead.
- Delivery is Resend (already used for magic links + the daily report),
  to ALERT_EMAILS or, when that is unset, ADMIN_EMAILS. With no
  RESEND_API_KEY the module logs the alert and sends nothing, so dev/CI
  never emails.
- State is in-memory and per-process: the same single-instance trade-off
  as the magic-link rate limiter in routers/auth.py (see the scope
  comment there). A restart forgetting cooldowns risks at most one
  duplicate email.
"""

from __future__ import annotations

import collections
import datetime
import logging
import threading
import traceback

from tutor.config import get_config
from tutor.courses import IDENTITY
from tutor.utctime import utcnow

logger = logging.getLogger("tutor.alerts")

_GLOBAL_CAP_PER_HOUR = 20
# Never crowded out by a storm of distinct exception keys from a bad deploy
# (F55): the per-key cooldown still holds.
_PRIORITY_KEYS = frozenset({"db-down", "db-recovered", "boot-degraded"})
# Alerts one student's browser can cause (a dead link per path family, a
# sign-in failing, a report from the Tell-the-instructor button) share a
# budget of ten a day (decision 10), so a student walking distinct 404 paths
# cannot spend the hourly cap the system alerts need.
_STUDENT_PREFIXES = ("404:", "login:", "login-anon:", "report:")
_STUDENT_CAP_PER_DAY = 10
_HOUR = datetime.timedelta(hours=1)
_DAY = datetime.timedelta(days=1)

_lock = threading.Lock()
_last_sent: dict[str, datetime.datetime] = {}
_sent_log: collections.deque[datetime.datetime] = collections.deque()
_student_log: collections.deque[datetime.datetime] = collections.deque()


def _reset_for_tests() -> None:
    with _lock:
        _last_sent.clear()
        _sent_log.clear()
        _student_log.clear()


def _resend_config() -> tuple[str, str, tuple[str, ...]]:
    """(api_key, from_email, recipients). Recipients fall back to ADMIN_EMAILS."""
    cfg = get_config()
    recipients = cfg["alert_emails"] or cfg["admin_emails"]
    return cfg["resend_api_key"], cfg["from_email"], tuple(sorted(recipients))


def _deliver_safe(
    api_key: str, from_email: str, to: tuple[str, ...], subject: str, body: str
) -> str:
    """Actually send via Resend. Never raises (alerting must not create errors)."""
    try:
        import resend

        resend.api_key = api_key
        resend.Emails.send(
            {
                "from": from_email,
                "to": list(to),
                "subject": subject,
                "text": body,
            }
        )
        logger.info("alert sent: %s -> %s", subject, ",".join(to))
        return "sent"
    except Exception as exc:  # noqa: BLE001 - the alert path must never raise
        logger.error("alert delivery failed (%s): %s", type(exc).__name__, exc)
        return "error"


def send_alert(key: str, subject: str, body: str, *, background: bool = True) -> str:
    """Email the operator about a system problem, deduped by ``key``.

    Returns one of: "sent" | "queued" (background thread) | "cooldown" |
    "capped" | "disabled" (no Resend key or recipients) | "error".
    """
    now = utcnow()
    cooldown = datetime.timedelta(minutes=get_config()["alert_cooldown_minutes"])
    student_related = key.startswith(_STUDENT_PREFIXES)
    with _lock:
        while _sent_log and _sent_log[0] < now - _HOUR:
            _sent_log.popleft()
        while _student_log and _student_log[0] < now - _DAY:
            _student_log.popleft()
        last = _last_sent.get(key)
        if last is not None and now - last < cooldown:
            return "cooldown"
        if student_related and len(_student_log) >= _STUDENT_CAP_PER_DAY:
            logger.warning("alert suppressed by the student budget: [%s] %s", key, subject)
            return "capped"
        if len(_sent_log) >= _GLOBAL_CAP_PER_HOUR and key not in _PRIORITY_KEYS:
            logger.warning("alert suppressed by global cap: [%s] %s", key, subject)
            return "capped"
        # Reserve the slot before the (possibly backgrounded) send so a burst
        # cannot race past the dedup between the check and the delivery.
        _last_sent[key] = now
        _sent_log.append(now)
        if student_related:
            _student_log.append(now)

    api_key, from_email, recipients = _resend_config()
    if not api_key or not recipients:
        logger.warning(
            "ALERT (email disabled, no RESEND_API_KEY/recipients): %s | %s",
            subject,
            body[:500],
        )
        return "disabled"

    full_subject = f"[{IDENTITY.name}] {subject}"
    stamped = f"{body}\n\n--\n{get_config()['base_url']} at {now.isoformat()}Z"
    if background:
        threading.Thread(
            target=_deliver_or_release,
            args=(key, now, api_key, from_email, recipients, full_subject, stamped),
            daemon=True,
        ).start()
        return "queued"
    return _deliver_or_release(key, now, api_key, from_email, recipients, full_subject, stamped)


def _deliver_or_release(key: str, reserved_at: datetime.datetime, api_key: str, from_email: str,
                        to: tuple[str, ...], subject: str, body: str) -> str:
    """Deliver; on a failure give the key's slot back, so the next
    occurrence is not silenced for the whole cooldown by a Resend hiccup."""
    result = _deliver_safe(api_key, from_email, to, subject, body)
    if result == "error":
        with _lock:
            if _last_sent.get(key) == reserved_at:
                del _last_sent[key]
    return result


def alert_exception(key: str, path: str, exc: BaseException, *, note: str = "") -> str:
    """Alert on an unhandled exception; traceback tail goes in the email body."""
    tb = "".join(traceback.format_exception(exc))[-6000:]
    subject = f"{type(exc).__name__} on {path}"
    body = (f"{note}\n\n" if note else "") + tb
    return send_alert(key, subject, body)
