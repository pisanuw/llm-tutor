"""Auth routes: Google OAuth 2.0, and an emailed 6-digit code with a link.

The email carries both a code and a link (2026-10-02). Some university mail
systems rewrite every link through a URL scanner (Proofpoint URL Defense, for
one) that fetches it on delivery, so a one-time link that signed in on GET
was spent seconds after it was sent, and the student's own tap found it
used. The code, typed on the login page, is a sign-in no scanner can spend;
the link still works, through a confirmation page whose Continue button is
the request that consumes it.
Every step, whatever its outcome, is recorded by tutor.auth.attempts for the
admin Logins tab.
"""

from __future__ import annotations

import os

import collections
import datetime
import logging
import re
import secrets
import textwrap
import threading
from html import escape
from typing import Optional
from urllib.parse import quote, urlparse

import resend
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from tutor.templating import templates
from sqlalchemy.orm import Session as DBSession
from starlette.concurrency import run_in_threadpool

from tutor.auth import attempts
from tutor.auth.access import is_allowed
from tutor.auth.deps import get_db
from jose import JWTError

from tutor.auth.jwt_utils import create_token, decode_token
from tutor.config import get_config
from tutor.courses import IDENTITY
from tutor.db.models import MagicLink, Session as SessionModel, Student
from tutor.utctime import utcnow

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("tutor")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# ---------------------------------------------------------------------------
# Rate limiters (per server-observed client IP).
#
# SCOPE / EFFECTIVENESS — read before relying on this as a control:
# These limiters are in-memory and per-process. They are keyed on the
# server-observed client IP (request.client.host), which the client cannot
# forge end-to-end, so this is NOT the "limiter keyed on a client-controlled
# id" anti-pattern. BUT:
#   * State lives in this process only. It resets on restart/redeploy and does
#     NOT coordinate across instances — so it is effective ONLY on a single,
#     long-lived instance. The deployment IS single-instance today (see
#     render.yaml: one Render web service, no horizontal scaling). If this app
#     is ever scaled out or moved to a serverless/multi-replica platform, this
#     limiter silently stops limiting and MUST be moved to a durable shared
#     store (Redis / the database with an atomic upsert) keyed on IP.
#   * Behind a reverse proxy, request.client.host is the proxy's address unless
#     a trusted X-Forwarded-For is parsed. Render's proxy reaches the container
#     from a private address, and uvicorn trusts the header only from
#     127.0.0.1 by default, so until 2026-10-07 every student shared one key
#     and these limits were per class. render.yaml now sets
#     FORWARDED_ALLOW_IPS to the private ranges (never "*", which would take
#     the leftmost forwarded entry, one the caller can write themselves);
#     tests/test_deploy_gate.py pins it. Locally, with no proxy, the address
#     is the client's already.
# The limiters evict stale per-IP entries so the maps cannot grow unbounded.
# ---------------------------------------------------------------------------
_MAGIC_LINK_LIMIT = 5            # emails per address per hour
_CODE_LIMIT = 20                 # code guesses per address per hour (typos happen)
_MAGIC_LINK_WINDOW = datetime.timedelta(hours=1)
_magic_link_log: dict[str, collections.deque] = {}
_code_log: dict[str, collections.deque] = {}
_magic_link_lock = threading.Lock()


def _rate_check(hits_by_ip: dict[str, collections.deque], ip: str, limit: int) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    NOTE: single-instance only; see the module-level scope comment above.
    """
    now = utcnow()
    cutoff = now - _MAGIC_LINK_WINDOW
    with _magic_link_lock:
        # Evict IPs whose entire window has aged out, so the map does not grow
        # without bound (one entry would otherwise persist forever per IP seen).
        stale = [k for k, dq in hits_by_ip.items() if not dq or dq[-1] < cutoff]
        for k in stale:
            del hits_by_ip[k]

        hits = hits_by_ip.setdefault(ip, collections.deque())
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            # Leave the (full) deque in place; the next sub-window pass prunes it.
            return False
        hits.append(now)
    return True


def _retry_after(hits_by_ip: dict[str, collections.deque], ip: str) -> str:
    """The local wall-clock minute the limit lifts for ``ip`` (its oldest
    hit plus the window), for the banner (review step 0.6, F36)."""
    from tutor.utctime import to_local

    with _magic_link_lock:
        hits = hits_by_ip.get(ip)
        oldest = hits[0] if hits else utcnow()
    return to_local(oldest + _MAGIC_LINK_WINDOW).strftime("%-I:%M %p")


def _magic_link_rate_check(ip: str) -> bool:
    """Emails: at most _MAGIC_LINK_LIMIT per address per hour."""
    return _rate_check(_magic_link_log, ip, _MAGIC_LINK_LIMIT)


def _code_rate_check(ip: str) -> bool:
    """Code guesses: at most _CODE_LIMIT per address per hour. With
    _CODE_MAX_ATTEMPTS per link as well, a 6-digit code cannot be searched."""
    return _rate_check(_code_log, ip, _CODE_LIMIT)


def _safe_next(raw) -> str:
    """Where to land after login: a path on this site, else /chat.

    ``next`` arrives in a query string, a form field, and the emailed magic
    link, so it is untrusted. Only a single-slash absolute path passes:
    "//host", "/\\host", schemes, control characters, and /auth/ paths (which
    could loop or log the student straight back out) fall back to /chat.
    """
    if not isinstance(raw, str):
        return "/chat"
    path = raw.strip()
    if (not path.startswith("/") or path.startswith("//") or "\\" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or path.startswith("/auth")):
        return "/chat"
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return "/chat"
    return path


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _origin(request: Request) -> str:
    """Origin (scheme://host) to anchor login on.

    Use the host the user actually arrived on so login is "sticky": arrive on
    your-service.onrender.com and you come back there, not the custom
    domain. Forces https for non-local hosts (prod sits behind a TLS proxy that
    may forward the request to the app as http).

    SECURITY: the Host header is client-controlled, and this origin is baked
    into the emailed magic-link URL (a bearer login credential). Trusting an
    arbitrary Host would let an attacker request a link for a victim and have
    the email point at attacker.com, exfiltrating the token (account takeover).
    So we only honour hosts in the configured allowlist (BASE_URL host, the
    Render host, localhost, ALLOWED_HOSTS); anything else falls back to the
    trusted BASE_URL.

    NOTE: Google OAuth requires the resulting `{origin}/auth/google/callback`
    to be in the console's Authorized redirect URIs. The custom domain is
    registered; to use Google login on the onrender host, add its callback
    there too. Magic-link login has no such requirement and works immediately.
    """
    cfg = get_config()
    host = request.headers.get("host", "").strip()
    if not host:
        return cfg["base_url"]
    hostname = host.split(":")[0].lower()
    if hostname not in cfg["allowed_hosts"]:
        return cfg["base_url"]
    scheme = "http" if hostname in ("localhost", "127.0.0.1") else "https"
    return f"{scheme}://{host}"


def _secure_cookies(request: Request) -> bool:
    """Whether auth cookies should carry the Secure flag.

    Secure everywhere except local http dev (localhost/127.0.0.1), mirroring
    _origin's scheme choice: prod sits behind TLS, so the session cookie (a
    bearer credential) must never ride a plaintext request, while local dev
    over http still needs the cookie to be sent.
    """
    host = request.headers.get("host", "").strip()
    hostname = host.split(":")[0].lower()
    return bool(hostname) and hostname not in ("localhost", "127.0.0.1")


def _support_email(cfg) -> str:
    """Whom a student writes to when sign-in fails: the first admin address,
    else the sender address."""
    admins = sorted(cfg["admin_emails"])
    return admins[0] if admins else cfg["from_email"]


def _login_page(request: Request, *, status_code: int = 200, **context) -> HTMLResponse:
    """login.html with the Google button state filled in. ``code_form`` shows
    the 6-digit code entry (prefilled with ``code_email``) instead of the
    address form; ``error`` and ``message`` are the banners."""
    cfg = get_config()
    ctx: dict = {"google_enabled": bool(cfg["google_client_id"]), "next": "/chat",
                 "code_form": False, "code_email": "",
                 "support_email": _support_email(cfg)}
    ctx.update(context)
    return templates.TemplateResponse(request, "login.html", ctx, status_code=status_code)


def _denied(request: Request, db: DBSession, email: str, *, method: str, next_path: str = "/chat") -> HTMLResponse:
    """Render the login page with an access-denied message (HTTP 403)."""
    attempts.record(db, request, method=method, outcome=attempts.DENIED, email=email)
    return _login_page(
        request, status_code=403, next=next_path,
        error=(f"{email} is not authorized to access {IDENTITY.name}. "
               "Ask the instructor to add you to the access list."),
    )


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: Optional[str] = None, code: Optional[str] = None):
    """``?code=1`` opens straight on the code form, for a student who closed
    the page between asking for the email and reading it."""
    return _login_page(request, next=_safe_next(next), code_form=bool(code))


@router.get("/", response_class=HTMLResponse)
async def root_redirect():
    return RedirectResponse("/auth/login")


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

@router.get("/google")
async def google_login(request: Request, next: Optional[str] = None,
                       db: DBSession = Depends(get_db)):
    cfg = get_config()
    if not cfg["google_client_id"]:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")
    redirect_uri = f"{_origin(request)}/auth/google/callback"
    async with AsyncOAuth2Client(
        client_id=cfg["google_client_id"],
        redirect_uri=redirect_uri,
        scope="openid email profile",
    ) as client:
        uri, state = client.create_authorization_url(GOOGLE_AUTH_URL)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = _safe_next(next)
    # The one record that shows a student reached Google and never came back
    # (a Workspace that blocks the app, an unprovisioned school Google account, a
    # host whose callback the console does not list). Off the loop.
    await run_in_threadpool(attempts.record, db, request,
                            method=attempts.GOOGLE, outcome=attempts.STARTED)
    return RedirectResponse(uri)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: DBSession = Depends(get_db),
):
    cfg = get_config()
    expected_state = request.session.pop("oauth_state", None)
    next_path = _safe_next(request.session.pop("oauth_next", None))

    async def failed(outcome: str, message: str, status_code: int, *,
                     email: Optional[str] = None, detail: Optional[str] = None) -> Response:
        # The student reads the message; the admin reads the row (off the loop).
        await run_in_threadpool(attempts.record, db, request, method=attempts.GOOGLE,
                                outcome=outcome, email=email, detail=detail)
        return _login_page(request, status_code=status_code, error=message, next=next_path)

    if error or not code:
        # Google sends the student back with ?error= when they cancel, or when
        # their Workspace forbids the app (access_denied, admin_policy_enforced).
        return await failed(
            attempts.OAUTH_ERROR,
            f"Google did not complete the sign-in ({error or 'no code returned'}). "
            "Try again, or sign in with the emailed code below.",
            400, detail=error or "no code",
        )
    # CSRF defence: the state we issued in /auth/google must come back unchanged.
    # Without this check the stored oauth_state is dead weight and the OAuth login
    # is open to login-CSRF / authorization-code injection.
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return await failed(
            attempts.INVALID_STATE,
            "This browser lost the sign-in it started (cookies blocked, or Google "
            "returned to a different browser). Start again from this page.",
            400,
        )
    redirect_uri = f"{_origin(request)}/auth/google/callback"
    try:
        async with AsyncOAuth2Client(
            client_id=cfg["google_client_id"],
            client_secret=cfg["google_client_secret"],
            redirect_uri=redirect_uri,
        ) as client:
            await client.fetch_token(GOOGLE_TOKEN_URL, code=code)
            resp = await client.get(GOOGLE_USERINFO_URL)
        info = resp.json()
    except Exception as exc:
        # Was an unhandled 500 with a blank page; the admin now sees the class.
        log.warning("google sign-in: token exchange failed", exc_info=True)
        return await failed(
            attempts.OAUTH_ERROR,
            "Google sign-in failed on Google's side. Try again, or sign in with "
            "the emailed code below.",
            502, detail=type(exc).__name__,
        )
    email = (info.get("email") or "").lower()
    if not info.get("email_verified"):
        return await failed(
            attempts.UNVERIFIED_EMAIL,
            "Google reports this account's email address as unverified. Sign in "
            "with the emailed code below instead.",
            400, email=email,
        )
    if not email:
        return await failed(attempts.NO_EMAIL, "Google did not return an email address.", 400)
    name = info.get("name", "")
    google_sub = info.get("sub", "")

    def finish():
        if not is_allowed(db, email):
            return _denied(request, db, email, method=attempts.GOOGLE)
        return _login_or_create(
            db, email=email, name=name, google_sub=google_sub,
            secure=_secure_cookies(request), next_path=next_path, request=request,
        )

    # The database work runs in the threadpool, off the event loop (plan
    # step 2.6, C1); only the OAuth calls above are awaited here.
    return await run_in_threadpool(finish)


# ---------------------------------------------------------------------------
# Magic link + code: asking for the email
# ---------------------------------------------------------------------------

@router.get("/magic-link")
@router.get("/magic-code")
async def code_form_again():
    """The code page is the answer to a POST, so the address bar, a restored
    tab or Back after leaving it asks for it with a GET: that was a 405
    error page (R17). The login page's code form is where the student
    was."""
    return RedirectResponse("/auth/login?code=1", status_code=303)


@router.post("/magic-link")
async def send_magic_link(request: Request, db: DBSession = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    if not _magic_link_rate_check(ip):
        await run_in_threadpool(attempts.record, db, request, method=attempts.MAGIC,
                                outcome=attempts.RATE_LIMITED, email=email)
        # The page keeps where the student was going (a "Sign in again"
        # link's next), so the second try lands there (the Stage 3 review).
        return _login_page(request, status_code=429, next=_safe_next(form.get("next")),
                           error=f"Too many sign-in emails requested. Try again after {_retry_after(_magic_link_log, ip)}; "
                                 "the newest code in your inbox still works.")
    # 254 is the longest address SMTP carries; a longer one would overflow
    # magic_links.email, a 500 on Postgres (R3).
    if not email or "@" not in email or len(email) > 254:
        await run_in_threadpool(attempts.record, db, request, method=attempts.MAGIC,
                                outcome=attempts.INVALID_EMAIL, email=email)
        return _login_page(request, status_code=400, next=_safe_next(form.get("next")),
                           error="Enter a valid email address.")
    # The database work and the blocking email send run in the threadpool,
    # off the event loop (plan step 2.6, C1).
    return await run_in_threadpool(_issue_magic_link, request, db, email, form.get("next"))


def _issue_magic_link(request: Request, db: DBSession, email: str, raw_next) -> Response:
    cfg = get_config()

    if not is_allowed(db, email):
        return _denied(request, db, email, method=attempts.MAGIC, next_path=_safe_next(raw_next))

    # The link carries the email; the students row is created by a successful
    # login, not by asking for a link (plan step 2.9, finding C21).
    token = secrets.token_urlsafe(48)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    expires_at = utcnow() + datetime.timedelta(
        minutes=cfg["magic_link_expiry_minutes"]
    )
    _prune_links(db, email)
    student = db.query(Student).filter_by(email=email).first()
    _save_link(db, email, student, token, code, expires_at)

    next_path = _safe_next(raw_next)
    magic_url = f"{_origin(request)}/auth/magic/{token}"
    if next_path != "/chat":
        magic_url += f"?next={quote(next_path, safe='')}"
    # Dev mode shows the link and the code on the page, so whoever typed an
    # address can sign in as it (an admin included: admins pass the
    # whitelist). Local development only: never beside a real database.
    dev_mode = not cfg["resend_api_key"] and not os.getenv("DATABASE_URL", "").strip()
    outcome = attempts.LINK_SHOWN
    detail = None
    error = None

    if cfg["resend_api_key"]:
        try:
            resend.api_key = cfg["resend_api_key"]
            resend.Emails.send(_magic_link_email(cfg, to=email, code=code, magic_url=magic_url))
            message = (f"We emailed a 6-digit code and a sign-in link to {email}. "
                       "Type the code here, or open the link and press Continue.")
            outcome = attempts.LINK_SENT
        except Exception as exc:
            # The student reads the failure; the operator must see it too
            # (plan step 5.6). No address in the log.
            log.warning("magic-link email failed", exc_info=True)
            message = None
            error = (f"Email delivery failed. Write to {_support_email(cfg)} "
                     "and we will let you in another way.")
            dev_mode = False
            outcome = attempts.EMAIL_FAILED
            detail = type(exc).__name__
    elif dev_mode:
        message = "No email provider configured. Use the code or the link below to log in (dev mode)."
    else:
        log.warning("magic-link sign-in requested but RESEND_API_KEY is not set on a deployed site")
        message = None
        error = ("Email sign-in is not set up on this site yet. Sign in with Google if it is "
                 f"offered, or write to {_support_email(cfg)}.")
        outcome = attempts.EMAIL_FAILED
        detail = "no email provider"

    attempts.record(db, request, method=attempts.MAGIC, outcome=outcome, email=email,
                    student_id=student.id if student else None, detail=detail)
    return _login_page(
        request,
        message=message,
        error=error,
        dev_link=magic_url if dev_mode else None,
        dev_code=code if dev_mode else None,
        next=next_path,
        code_form=outcome != attempts.EMAIL_FAILED,
        code_email=email,
    )


def _magic_link_email(cfg, *, to: str, code: str, magic_url: str) -> resend.Emails.SendParams:
    """The Resend payload for a sign-in email.

    It can land in a university inbox beside phishing lookalikes, and some
    university mail systems rewrite every link through a URL scanner
    (2026-10-02), so it says who sent it and why, leads with the code, says
    what to do when it was not asked for, and names a person to write to
    when it does not work. Reply-To is that person, so "reply to this email"
    is true.
    """
    minutes = cfg["magic_link_expiry_minutes"]
    site = urlparse(magic_url).netloc
    contact = _support_email(cfg)
    spaced = f"{code[:3]} {code[3:]}"
    name = IDENTITY.name
    # Who runs it, from courses.yaml: "the introductory astronomy study
    # assistant run by Dr. Alex Morgan at Example College".
    about = f"the {IDENTITY.subject} study assistant run by {IDENTITY.instructor}" + (
        f" at {IDENTITY.institution}" if IDENTITY.institution else "")
    intro = textwrap.fill(
        f"Someone, probably you, entered {to} on the login page of {name} ({site}), {about}.",
        width=72, break_long_words=False, break_on_hyphens=False)
    text = (
        f"Your {name} sign-in code: {code}\n"
        f"\n"
        f"{intro}\n"
        f"\n"
        f"To sign in, type the code above on that login page. Or open this link\n"
        f"and press Continue:\n"
        f"{magic_url}\n"
        f"\n"
        f"The code and the link work once and expire in {minutes} minutes.\n"
        f"\n"
        f"Did not ask for this? Ignore this email. Nothing happens unless the\n"
        f"code is typed or the link is confirmed, and your account is unchanged.\n"
        f"\n"
        f"Trouble signing in? Reply to this email or write to {contact}, and say\n"
        f"what the page showed.\n"
        f"\n"
        f"Sent by {name} ({site}) to {to}.\n"
    )
    e_to, e_site, e_contact, e_url = escape(to), escape(site), escape(contact), escape(magic_url)
    e_name, e_about = escape(name), escape(about)
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.5;color:#1f2937;max-width:32em">'
        f"<p>Your {e_name} sign-in code:</p>"
        f'<p style="font-size:32px;letter-spacing:0.3em;font-family:ui-monospace,Menlo,monospace;'
        f'margin:0 0 1em">{spaced}</p>'
        f"<p>Someone, probably you, entered <b>{e_to}</b> on the login page of "
        f"<a href=\"https://{e_site}/auth/login\">{e_name}</a> ({e_site}), {e_about}.</p>"
        "<p>To sign in, type the code above on that login page. Or open this link "
        "and press <b>Continue</b>:</p>"
        f'<p><a href="{e_url}" style="display:inline-block;padding:10px 18px;background:#1d4ed8;'
        f'color:#fff;border-radius:8px;text-decoration:none">Sign in to {e_name}</a></p>'
        f'<p style="font-size:12px;color:#6b7280;word-break:break-all">{e_url}</p>'
        f"<p>The code and the link work once and expire in {minutes} minutes.</p>"
        "<p><b>Did not ask for this?</b> Ignore this email. Nothing happens unless the "
        "code is typed or the link is confirmed, and your account is unchanged.</p>"
        f"<p><b>Trouble signing in?</b> Reply to this email or write to "
        f'<a href="mailto:{e_contact}">{e_contact}</a>, and say what the page showed.</p>'
        f'<p style="font-size:12px;color:#6b7280">Sent by {e_name} ({e_site}) to {e_to}.</p>'
        "</div>"
    )
    return {
        "from": cfg["from_email"],
        "to": to,
        "reply_to": contact,
        "subject": f"{code} is your {name} sign-in code",
        "html": html,
        "text": text,
    }


def _prune_links(db: DBSession, email: str) -> None:
    """Drop this address's used and expired links (plan step 2.9, C21), so
    the table does not grow one row per request forever; the retention sweep
    prunes everyone's."""
    from sqlalchemy import or_

    db.query(MagicLink).filter(
        MagicLink.email == email,
        or_(MagicLink.used == True, MagicLink.expires_at < utcnow()),  # noqa: E712
    ).delete(synchronize_session=False)


def _save_link(db: DBSession, email: str, student: Optional[Student], token: str, code: str,
               expires_at: datetime.datetime) -> None:
    from sqlalchemy.exc import IntegrityError

    db.add(MagicLink(student_id=student.id if student else None, email=email,
                     token=token, code=code, expires_at=expires_at))
    try:
        db.commit()
    except IntegrityError:
        # A database still on the old schema (student_id NOT NULL) until the
        # migration runs: keep logins working the old way.
        db.rollback()
        student = student or _insert_student(db, Student(email=email, name=email.split("@")[0]))
        db.add(MagicLink(student_id=student.id, email=email, token=token, code=code,
                         expires_at=expires_at))
        db.commit()


# ---------------------------------------------------------------------------
# Magic link + code: signing in
# ---------------------------------------------------------------------------

_CODE_MAX_ATTEMPTS = 5
_LINK_GONE = ("That sign-in link has expired or was already used. "
              "Enter your email below for a new one.")
_CODE_WRONG = "That code did not match. Check the newest email, or ask for a new code below."


def _live_link(db: DBSession, token: str) -> Optional[MagicLink]:
    """The unused, unexpired link with this token, or None. Reads only."""
    return (
        db.query(MagicLink)
        .filter(MagicLink.token == token, MagicLink.used == False,  # noqa: E712
                MagicLink.expires_at >= utcnow())
        .first()
    )


def _link_email(db: DBSession, link: MagicLink) -> Optional[str]:
    """The address a link was sent to: on the row since f3b9d2e7a1c6, else
    the student's."""
    if link.email:
        return link.email
    student = db.get(Student, link.student_id) if link.student_id else None
    return student.email if student else None


def _signed_in_as(db: DBSession, request: Request, *, token: Optional[str] = None,
                  email: Optional[str] = None) -> bool:
    """The browser already holds a live session for the link's (or the
    address's) student: a second tap of Continue or Sign in, a double
    submit, or the email's link opened again after signing in. That request
    used to say the link or code had failed although the first had worked
    (R32)."""
    from tutor.auth.deps import session_student

    me = session_student(db, request.cookies.get("session_token"))
    if me is None:
        return False
    if token is not None:
        # A link pruned since (used links go when the address asks for a new
        # code) is a signed-in browser's old link: it goes on too. Only a
        # dead link of another student's stays the error page.
        link = db.query(MagicLink).filter_by(token=token).first()
        return link is None or link.student_id == me.id or (link.email or "").lower() == (me.email or "").lower()
    return bool(email) and (me.email or "").lower() == (email or "").lower()


def _claim(db: DBSession, token: str) -> Optional[MagicLink]:
    """Consume the link with one conditional update, so two uses of the same
    link cannot both log in (plan step 2.9, finding C21). Returns the claimed
    row, or None when it was expired, used, or never existed."""
    claimed = (
        db.query(MagicLink)
        .filter(MagicLink.token == token, MagicLink.used == False,  # noqa: E712
                MagicLink.expires_at >= utcnow())
        .update({MagicLink.used: True}, synchronize_session=False)
    )
    db.commit()
    if claimed != 1:
        return None
    return db.query(MagicLink).filter_by(token=token).first()


def _finish_link(request: Request, db: DBSession, link: MagicLink, next_path: str) -> Response:
    """A link just claimed, by its button or its code: find or make the
    student, check access, and issue the session."""
    student = db.get(Student, link.student_id) if link.student_id else None
    email = link.email or (student.email if student else None)
    if not email:
        # The student row was deleted while an unused link survived; treat the
        # link as invalid rather than dereferencing None into a 500.
        attempts.record(db, request, method=attempts.MAGIC, outcome=attempts.LINK_INVALID,
                        detail="no address on the link")
        return _login_page(request, status_code=400, error=_LINK_GONE)
    if not is_allowed(db, email):
        return _denied(request, db, email, method=attempts.MAGIC)
    if student is None:
        student = db.query(Student).filter_by(email=email).first() or _insert_student(
            db, Student(email=email, name=email.split("@")[0]))
    if not student.name:
        # A roster pre-registered this student (classes._pre_register), so the
        # row exists with a blank name. Fill it the way a fresh magic-link row
        # is filled, else the instructor's roster shows "—" forever; Google
        # login does the same with the real name in _login_or_create.
        student.name = email.split("@")[0]
    link.student_id = student.id
    db.commit()
    return _set_session_cookie(
        student, db, method="magic", secure=_secure_cookies(request), next_path=next_path,
        request=request,
    )


@router.get("/magic/{token}", response_class=HTMLResponse)
def magic_link_page(
    request: Request, token: str, next: Optional[str] = None, db: DBSession = Depends(get_db),
):
    """The emailed link lands here, on a page with one Continue button, and
    nothing is consumed.

    Mail scanners and link previews fetch every URL in a message (a GET,
    sometimes a HEAD first). Until 2026-10-02 this GET signed the fetcher in
    and spent the link: in production, two links to one student each
    "logged in" eight seconds after being sent, and every tap of the
    student's own answered 400 for an hour. Some university mail systems
    rewrite every link through a URL scanner (Proofpoint URL Defense, for
    one), which does exactly that. A form POST is the one request a scanner
    does not make.
    """
    link = _live_link(db, token)
    if link is None:
        if _signed_in_as(db, request, token=token):
            return RedirectResponse(_safe_next(next), status_code=303)
        _record_dead_link(db, request, token, "link opened")
        return _login_page(request, status_code=400, error=_LINK_GONE)
    email = _link_email(db, link)
    if email and not is_allowed(db, email):
        return _denied(request, db, email, method=attempts.MAGIC)
    attempts.record(db, request, method=attempts.MAGIC, outcome=attempts.LINK_OPENED,
                    email=email, student_id=link.student_id)
    return templates.TemplateResponse(
        request, "magic_confirm.html",
        {"email": email, "token": token, "next": _safe_next(next)},
    )


@router.post("/magic/{token}")
def magic_link_login(
    request: Request, token: str, next: Optional[str] = None, db: DBSession = Depends(get_db),
):
    """The Continue button: claim the link and sign in. ``next`` rides in the
    form's action query string, so this route parses no body."""
    link = _claim(db, token)
    if link is None:
        if _signed_in_as(db, request, token=token):
            return RedirectResponse(_safe_next(next), status_code=303)
        _record_dead_link(db, request, token, "link confirmed")
        return _login_page(request, status_code=400, error=_LINK_GONE)
    return _finish_link(request, db, link, _safe_next(next))


def _record_dead_link(db: DBSession, request: Request, token: str, what: str) -> None:
    """A tap on an expired or used link, with the address the link carried
    (the failure a student repeats most, and until review step 0.6 the one
    row without their address, F29), and whether the browser was a mail
    scanner, which the repeated-failure alert then ignores."""
    from tutor.request_errors import BROWSER, classify_agent

    link = db.query(MagicLink).filter_by(token=token).first() if token else None
    email = _link_email(db, link) if link is not None else None
    why = "unknown" if link is None else ("used" if link.used else "expired")
    agent = classify_agent(request.headers.get("user-agent"))
    detail = f"{what} ({why})" + ("" if agent == BROWSER else f"; {agent}")
    attempts.record(db, request, method=attempts.MAGIC, outcome=attempts.LINK_INVALID,
                    email=email, student_id=link.student_id if link is not None else None, detail=detail)


@router.post("/magic-code")
def magic_code_login(
    request: Request,
    email: str = Form(default=""),
    code: str = Form(default=""),
    next: Optional[str] = Form(default=None),
    db: DBSession = Depends(get_db),
):
    """Sign in with the 6-digit code from the email: the sign-in a mail
    scanner cannot spend. The code matches any of the address's live links
    (a student who asked twice may type either), a wrong guess counts against
    all of them, and a link with _CODE_MAX_ATTEMPTS wrong guesses is dead."""
    ip = request.client.host if request.client else "unknown"
    email = email.strip().lower()
    digits = re.sub(r"\D", "", code)
    next_path = _safe_next(next)

    def back(outcome: str, message: str, status_code: int, detail: str) -> HTMLResponse:
        attempts.record(db, request, method=attempts.MAGIC, outcome=outcome, email=email,
                        detail=detail)
        return _login_page(request, status_code=status_code, error=message, code_form=True,
                           code_email=email, next=next_path)

    if not _code_rate_check(ip):
        return back(attempts.RATE_LIMITED,
                    f"Too many code attempts. Try again after {_retry_after(_code_log, ip)}, "
                    "or open the link in the email.",
                    429, "code")
    if not email or len(email) > 254 or len(digits) != 6:
        return back(attempts.CODE_INVALID, "Enter your address and the 6-digit code from the email.",
                    400, "malformed")
    live = (
        db.query(MagicLink)
        .filter(MagicLink.email == email, MagicLink.used == False,  # noqa: E712
                MagicLink.expires_at >= utcnow(), MagicLink.code.isnot(None))
        .order_by(MagicLink.created_at.desc())
        .all()
    )
    match = None
    for link in live:
        if link.code_attempts < _CODE_MAX_ATTEMPTS and secrets.compare_digest(link.code or "", digits):
            match = link
            break
    if match is None:
        if _signed_in_as(db, request, email=email):
            # The first press of Sign in already used the code.
            return RedirectResponse(next_path, status_code=303)
        spent = (
            db.query(MagicLink)
            .filter(MagicLink.email == email, MagicLink.used == True,  # noqa: E712
                    MagicLink.expires_at >= utcnow(), MagicLink.code == digits)
            .first()
        )
        if spent is not None:
            # The right code, already used (the browser closed before the
            # first press's answer, or the link signed in elsewhere): saying
            # it did not match was wrong (the Stage 4 review).
            return back(attempts.LINK_INVALID, "That code was already used. Ask for a new one below.",
                        400, "code used")
        for link in live:
            link.code_attempts += 1
        db.commit()
        return back(attempts.CODE_INVALID, _CODE_WRONG, 400,
                    "wrong code" if live else "no live link")
    claimed = _claim(db, match.token)
    if claimed is None:
        # The link's own button won the race.
        if _signed_in_as(db, request, email=email):
            return RedirectResponse(next_path, status_code=303)
        return back(attempts.LINK_INVALID, _LINK_GONE, 400, "code after link")
    return _finish_link(request, db, claimed, next_path)


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.get("/logout", response_class=HTMLResponse)
def logout_page():
    """Old links and bookmarks land here. Logging out changes state, so it is
    a POST (plan step 4.5, finding C19): this page only offers the button."""
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Log out of {escape(IDENTITY.name)}</title>'
        '<link rel="stylesheet" href="/static/vendor/tailwind.css"></head>'
        '<body class="bg-gray-50"><main class="max-w-sm mx-auto px-4 py-16 text-center">'
        '<form method="post" action="/auth/logout">'
        '<button type="submit" class="min-h-[44px] px-6 rounded-lg bg-blue-700 text-white font-medium">'
        'Log out</button></form>'
        '<p class="mt-4 text-sm"><a class="text-blue-700 underline" href="/chat">Back to the chat</a></p>'
        '</main></body></html>'
    )


@router.post("/logout")
def logout(request: Request, db: DBSession = Depends(get_db)):
    token = request.cookies.get("session_token")
    if token:
        try:
            payload = decode_token(token)
            jti = payload.get("jti")
            if jti:
                sess = db.query(SessionModel).filter_by(jwt_jti=jti).first()
                if sess:
                    sess.revoked = True
                    db.commit()
        except JWTError:
            pass   # an unreadable cookie: nothing to revoke, just clear it
        except Exception:
            # The cookie is cleared either way, but a session left unrevoked
            # stays valid until it expires: say so (plan step 5.6).
            db.rollback()
            log.warning("logout: could not revoke the session", exc_info=True)
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie("session_token")
    response.delete_cookie("admin_session")   # a token admin's session too
    return response


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _login_or_create(
    db: DBSession,
    email: str,
    name: str = "",
    google_sub: str = "",
    method: str = "google",
    *,
    secure: bool = False,
    next_path: str = "/chat",
    request: Optional[Request] = None,
) -> Response:
    student = db.query(Student).filter_by(email=email).first()
    if not student:
        student = _insert_student(db, Student(email=email, name=name, google_sub=google_sub or None))
    if google_sub and not student.google_sub:
        student.google_sub = google_sub
    if name and not student.name:
        student.name = name
    db.commit()
    db.refresh(student)
    return _set_session_cookie(student, db, method=method, secure=secure, next_path=next_path,
                               request=request)


def _insert_student(db: DBSession, student: Student) -> Student:
    """Insert and commit a new student row. A double submit or two tabs can
    race on the unique email; the loser reads the winner's row instead of
    answering 500 and paging the operator (plan step 2.2, finding C21)."""
    from sqlalchemy.exc import IntegrityError

    db.add(student)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(Student).filter_by(email=student.email).first()
        if existing is None:
            raise
        return existing
    db.refresh(student)
    return student


def _set_session_cookie(
    student: Student, db: DBSession, method: str = "", *, secure: bool = False,
    next_path: str = "/chat", request: Optional[Request] = None,
) -> Response:
    token, jti, expires_at = create_token(student.id)
    cfg = get_config()
    sess = SessionModel(
        student_id=student.id,
        jwt_jti=jti,
        expires_at=expires_at,
        method=method or None,
    )
    db.add(sess)
    # Login tracking: bump last_login + login_count on the student row so the
    # admin Students/Logins views have a cheap last-login + count, while the
    # sessions row above is the per-login audit trail (with method).
    student.last_login = utcnow()
    student.login_count = (student.login_count or 0) + 1
    db.commit()

    from tutor.logging_config import activity
    activity("login", student_id=student.id,
             method=method or "unknown", login_count=student.login_count)
    attempts.record(db, request, method=method or "unknown", outcome=attempts.OK,
                    email=student.email, student_id=student.id, detail=next_path)

    response = RedirectResponse(_safe_next(next_path), status_code=303)
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=cfg["jwt_expiry_days"] * 86400,
    )
    return response
