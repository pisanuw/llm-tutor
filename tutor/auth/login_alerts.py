"""A student who keeps failing to sign in is told to the operator at once
(step 0.6; F28).

``login_attempts`` held every failure and nobody read it until the admin
opened the Logins tab. :func:`note_failure` runs after each failed row is
committed: it counts the address's failures in the last :data:`WINDOW`
(for a row with no address, the same browser's) and at :data:`THRESHOLD`
sends one email through ``tutor.alerts`` (key ``login:<hash of the
address>``, so the cooldown is per student) naming the student, the
outcomes in order with local times, the browser, and the one line to tell
them. What never alerts: a bot's or a mail scanner's rows, and an address
that is not whitelisted and not a student (decision 6: those go to the
daily digest). Never raises.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor.auth import attempts as attempts_mod
from tutor.db.models import LoginAttempt, Student
from tutor.utctime import to_local, utcnow

log = logging.getLogger("tutor.auth")

THRESHOLD = 3
WINDOW = datetime.timedelta(minutes=60)

# What to tell the student, by their last failure.
ADVICE = {
    attempts_mod.LINK_INVALID: "Their link was expired or already used: ask them to request a new code on the login page.",
    attempts_mod.CODE_INVALID: "Their code was wrong or exhausted: ask them to request a new code and type it from the newest email.",
    attempts_mod.RATE_LIMITED: "They asked too often: the limit lifts an hour after the first request; the newest code still works.",
    attempts_mod.OAUTH_ERROR: "Google did not finish: ask them to try the email code instead.",
    attempts_mod.INVALID_STATE: "Google sent them back to a different browser or tab: ask them to start again from the login page.",
    attempts_mod.UNVERIFIED_EMAIL: "Their Google address is not verified: ask them to use the email code.",
    attempts_mod.NO_EMAIL: "Google shared no address: ask them to use the email code.",
    attempts_mod.EMAIL_FAILED: "The sign-in email could not be sent (Resend): check the provider; Google login still works.",
    attempts_mod.DENIED: "Their address is not on the whitelist (Admin → Access).",
    attempts_mod.INVALID_EMAIL: "They typed something that is not an address.",
}


def _key(email: Optional[str], user_agent: Optional[str]) -> str:
    raw = (email or "").strip().lower() or f"ua:{(user_agent or '').strip()}"
    return ("login:" if email else "login-anon:") + hashlib.sha256(raw.encode()).hexdigest()[:12]


def recent_failures(db: DBSession, *, email: Optional[str], user_agent: Optional[str],
                    now: Optional[datetime.datetime] = None) -> list[LoginAttempt]:
    """The window's failed rows for this address, or this browser when the
    row carries none, oldest first; a bot's or scanner's rows excluded, and
    the rows anyone could produce by typing the address (rate-limited, no
    live link) too, so a stranger cannot make a student look locked out."""
    from tutor.request_errors import BROWSER, classify_agent

    now = now or utcnow()
    q = db.query(LoginAttempt).filter(
        LoginAttempt.created_at >= now - WINDOW,
        LoginAttempt.outcome.in_(sorted(attempts_mod.FAILED)),
    )
    if email:
        q = q.filter(LoginAttempt.email == email.strip().lower())
    else:
        q = q.filter(LoginAttempt.email.is_(None), LoginAttempt.user_agent == (user_agent or None))
    rows = q.order_by(LoginAttempt.created_at.asc(), LoginAttempt.id.asc()).all()
    return [r for r in rows
            if classify_agent(r.user_agent) == BROWSER and attempts_mod.attributable(r.outcome, r.detail)]


def note_failure(db: DBSession, *, email: Optional[str], user_agent: Optional[str],
                 now: Optional[datetime.datetime] = None) -> str:
    """Count this failure; at the threshold, email the operator once. Returns
    ``skipped`` (a bot, or an unknown address), ``counted``, or what
    ``alerts.send_alert`` returned."""
    from tutor import alerts
    from tutor.config import get_config
    from tutor.request_errors import BROWSER, classify_agent

    try:
        if classify_agent(user_agent) != BROWSER:
            return "skipped"
        email = (email or "").strip().lower() or None
        student = db.query(Student).filter(Student.email == email).first() if email else None
        rows = recent_failures(db, email=email, user_agent=user_agent, now=now)
        if email and student is None and all(r.outcome == attempts_mod.DENIED for r in rows):
            return "skipped"           # not a student, not whitelisted: the digest's business
        if len(rows) < THRESHOLD:
            return "counted"
        who = (f"{student.name or ''} <{student.email}>".strip() if student is not None
               else email or "a browser with no address")
        last = rows[-1]
        lines = [f"{who} failed to sign in {len(rows)} times in the last {int(WINDOW.total_seconds() // 60)} minutes:", ""]
        for r in rows:
            when = to_local(r.created_at).strftime("%H:%M %Z") if r.created_at else "?"
            lines.append(f"- {when}  {r.method}  {r.outcome}" + (f"  ({r.detail})" if r.detail else ""))
        browser = (last.user_agent or "")[:80]
        lines += ["", f"Browser: {browser or 'unknown'}",
                  f"What to tell them: {ADVICE.get(last.outcome, 'Ask them what they see on the login page.')}",
                  "", f"The trail: {get_config()['base_url']}/admin?tab=logins"
                  + (f"&student={email}" if email else ""),
                  "One email per student per hour."]
        return alerts.send_alert(_key(email, user_agent), f"Sign-in failing: {who}", "\n".join(lines))
    except Exception:  # noqa: BLE001 - never break a login over its alert
        log.warning("login failure alert failed", exc_info=True)
        return "error"
