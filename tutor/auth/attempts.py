"""The trail behind every sign-in, successful or not (the admin Logins tab).

Until 2026-10-02 a failed login left nothing an admin could read: ``sessions``
holds only the logins that worked, and the process log carries no address and
no user agent (docs/retention-policy.md keeps it that way). A student who
"could not log in by Google or by magic link" had to be diagnosed from the
uvicorn access log, which showed the symptom (every tap of the link answered
400) but not who made the first, successful fetch of it. It was the
university's mail scanner, and only a recorded user agent can show that.

:func:`record` appends one ``login_attempts`` row per step and never raises.
The database is in the erasure and retention scope, so the row may carry the
address; the structured ``tutor.activity`` event it also emits for a failure
carries the student id and the outcome only.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session as DBSession

from tutor.db.models import LoginAttempt
from tutor.logging_config import activity

log = logging.getLogger("tutor.auth")

# ``method``
GOOGLE = "google"
MAGIC = "magic"
SESSION = "session"            # a live session that stopped working (no sign-in step)

# ``outcome``: in progress ...
STARTED = "started"            # google: sent to Google's consent screen
LINK_SENT = "link_sent"        # magic: code + link emailed
LINK_SHOWN = "link_shown"      # magic: no mail provider, shown on the page (dev)
LINK_OPENED = "link_opened"    # magic: the link's confirmation page was shown
# ... done ...
OK = "ok"                      # a session cookie was issued
# ... or failed.
DENIED = "denied"              # not an admin, not on the whitelist
RATE_LIMITED = "rate_limited"
INVALID_EMAIL = "invalid_email"
EMAIL_FAILED = "email_failed"  # the mail provider refused the send
LINK_INVALID = "link_invalid"  # expired, already used, or unknown token
CODE_INVALID = "code_invalid"  # wrong or exhausted 6-digit code
INVALID_STATE = "invalid_state"  # google: the callback's state did not match
OAUTH_ERROR = "oauth_error"    # google: an error parameter, or the token exchange failed
UNVERIFIED_EMAIL = "unverified_email"
NO_EMAIL = "no_email"
# ... or a session that ended without a sign-in (review step 0.6, F31):
EXPIRED = "expired"            # a page opened with a cookie past its session (the login redirect)
ACCESS_REMOVED = "access_removed"  # taken off the whitelist while signed in

IN_PROGRESS = frozenset({STARTED, LINK_SENT, LINK_SHOWN, LINK_OPENED})
LOCKOUTS = frozenset({EXPIRED, ACCESS_REMOVED})
FAILED = frozenset({
    DENIED, RATE_LIMITED, INVALID_EMAIL, EMAIL_FAILED, LINK_INVALID, CODE_INVALID,
    INVALID_STATE, OAUTH_ERROR, UNVERIFIED_EMAIL, NO_EMAIL,
})

_CLIP = 255

# Failures the public login form records under whatever address was typed,
# which spent nothing of that student's: a rate-limited request, a malformed
# submission, a code typed with no live link to check it against. Anyone can
# produce them for anyone, so they are never counted as the student's own.
_UNATTRIBUTABLE_DETAILS = frozenset({"malformed", "no live link"})


def attributable(outcome: str, detail: Optional[str]) -> bool:
    """Whether a failed row says something about the student at the address,
    rather than about whoever typed it (review step 0.9's review)."""
    if outcome in (RATE_LIMITED, INVALID_EMAIL):
        return False
    if outcome == CODE_INVALID and (detail or "").strip() in _UNATTRIBUTABLE_DETAILS:
        return False
    return True


def tone(outcome: str) -> str:
    """How the admin page colours an outcome: ``ok``, ``progress`` or ``fail``."""
    if outcome == OK:
        return "ok"
    if outcome in IN_PROGRESS or outcome in LOCKOUTS:
        return "progress"
    return "fail"


def _clip(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    return value[:_CLIP] if value else None


def record(
    db: DBSession,
    request: Optional[Request],
    *,
    method: str,
    outcome: str,
    email: Optional[str] = None,
    student_id: Optional[int] = None,
    detail: Optional[str] = None,
) -> None:
    """Append one row and commit it. Never raises: a broken trail must not
    break a login, so a failure is logged and rolled back.

    Call it after the flow's own commit or rollback, since it commits the
    session it is given. ``detail`` is for a next path or an exception class;
    never pass a token, a code, or a student's message.
    """
    host = ua = None
    if request is not None:
        host = _clip(request.headers.get("host"))
        ua = _clip(request.headers.get("user-agent"))
    record_values(db, method=method, outcome=outcome, email=email, student_id=student_id,
                  detail=detail, host=host, user_agent=ua)


def record_values(
    db: DBSession,
    *,
    method: str,
    outcome: str,
    email: Optional[str] = None,
    student_id: Optional[int] = None,
    detail: Optional[str] = None,
    host: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """:func:`record` with the request's facts already taken (for a caller
    off the event loop). After a failure is committed, the repeated-failure
    alert counts it (tutor.auth.login_alerts, review step 0.6)."""
    try:
        db.add(LoginAttempt(
            email=_clip((email or "").lower()), student_id=student_id, method=method,
            outcome=outcome, host=host, user_agent=_clip(user_agent), detail=_clip(detail),
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:  # pragma: no cover - the session is already broken
            pass
        log.warning("login attempt not recorded (%s/%s)", method, outcome, exc_info=True)
    if outcome in FAILED:
        activity("login_failed", level="minimal", method=method, outcome=outcome,
                 student_id=student_id, detail=detail)
        from tutor.auth import login_alerts

        login_alerts.note_failure(db, email=email, user_agent=user_agent)


def record_expiry(app_, *, token: Optional[str], host: Optional[str], user_agent: Optional[str]) -> None:
    """A page opened with a cookie past its session (the 401 redirect to
    the login page): one ``expired`` row with the student when the token
    names one (review step 0.6, F31). Runs off the event loop through the
    app's database dependency, so a test override applies. Never raises."""
    import inspect
    from typing import cast

    from jose import jwt

    from tutor.auth.deps import get_db
    from tutor.auth.jwt_utils import ALGORITHM
    from tutor.config import get_config

    student_id: Optional[int] = None
    email: Optional[str] = None
    try:
        # The signature is checked (only the expiry is not), so a forged
        # cookie never writes a row under a student's address.
        claims = (jwt.decode(token, get_config()["secret_key"], algorithms=[ALGORITHM],
                             options={"verify_exp": False}) if token else {})
        student_id = int(claims.get("sub", 0)) or None
    except Exception:  # noqa: BLE001 - a garbage or forged cookie is still an expiry, of nobody
        student_id = None
    provider = app_.dependency_overrides.get(get_db, get_db) if app_ is not None else get_db
    made = provider()
    db = cast(DBSession, next(made) if inspect.isgenerator(made) else made)
    try:
        if student_id:
            from tutor.db.models import Student

            student = db.get(Student, student_id)
            email = student.email if student is not None else None
            if student is None:
                student_id = None
        record_values(db, method=SESSION, outcome=EXPIRED, email=email, student_id=student_id,
                      host=host, user_agent=user_agent)
    finally:
        if inspect.isgenerator(made):
            made.close()
