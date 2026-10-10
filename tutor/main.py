"""The tutor's FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import logging
import os
import re

from typing import Optional

from urllib.parse import quote

from fastapi import Cookie, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from tutor import request_errors
from tutor.config import get_config
from tutor.db.models import app_engine, dispose_app_engines
from tutor.logging_config import configure_logging, content_enabled
from tutor.startup import ensure_database_ready
from tutor.routers import auth, chat, admin, api, map as map_router, path as path_router, review as review_router, lectures as lectures_router, status as status_router, onboarding as onboarding_router, instructor as instructor_router, errors as errors_router, feedback as feedback_router
from tutor.templating import templates
from tutor.paths import EXPLORABLES_DIR
from tutor.courses import DEFAULT_COURSE, IDENTITY

# Configure the tutor logger hierarchy as early as possible (idempotent), so the
# startup logs below and every later tutor.* logger emit at LOG_LEVEL.
_LOG_LEVEL = configure_logging()
logger = logging.getLogger("tutor")

# Optional Sentry error reporting (stability item 4b). No-op without a
# SENTRY_DSN env var; errors only (no performance tracing, no PII) to stay in
# the free quota. The sentry-sdk Starlette/FastAPI integrations auto-enable and
# catch the re-raise from our alerting Exception handler below, so email alerts
# and Sentry capture coexist on the same unhandled 500.
_SQL_DETAIL = re.compile(r"\s*\[(?:SQL|parameters):.*", re.DOTALL)
_MAGIC_TOKEN = re.compile(r"(/auth/magic/)[^/?#\s]+")


def _scrub_sentry_event(event, hint):
    """Keep student text and credentials out of Sentry (finding C10).

    send_default_pii=False does not stop the SDK attaching request bodies, so
    this drops them, plus cookies, the query string (admin tokens, ?student=
    emails, next= paths), auth headers, the magic-link token in the URL, and
    the SQL and bound parameters SQLAlchemy puts in exception messages (a
    failed insert echoes the student's message there).
    """
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "cookies", "query_string"):
            request.pop(key, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in ("cookie", "authorization", "x-admin-token"):
                    headers.pop(name)
        if isinstance(request.get("url"), str):
            request["url"] = _MAGIC_TOKEN.sub(r"\1[redacted]", request["url"])
    for exc in (event.get("exception") or {}).get("values") or []:
        if isinstance(exc, dict) and isinstance(exc.get("value"), str):
            exc["value"] = _SQL_DETAIL.sub(" [SQL details removed]", exc["value"])
    return event


_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.logging import ignore_logger

    from starlette.requests import ClientDisconnect as _ClientDisconnect

    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        environment=os.getenv("SENTRY_ENV", "production"),
        traces_sample_rate=0.0,
        send_default_pii=False,
        max_request_body_size="never",
        before_send=_scrub_sentry_event,
        # A browser that leaves mid-request is not an error of ours
        # (Starlette's ServerErrorMiddleware re-raises it past the handler).
        ignore_errors=[_ClientDisconnect],
    )
    # The activity stream is per-student telemetry; never ship it as breadcrumbs.
    ignore_logger("tutor.activity")
    logger.info("Sentry error reporting enabled")

_ROOT = Path(__file__).parent.parent


def _boot_warnings() -> list[str]:
    """Problems entrypoint.sh wrote while seeding (plan step 2.7)."""
    path = os.getenv("BOOT_WARNINGS", "").strip()
    if not path:
        return []
    try:
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    except OSError:
        return []


def _data_warnings(book_dir: Optional[Path] = None, audio_dir: Optional[Path] = None,
                   production: Optional[bool] = None) -> list[str]:
    """The book and the narration are mounted only when their directories
    exist at import (review step 0.3, F8): on Render, where the image builds
    the book and the disk holds the clips, an absent directory is said at
    boot instead of answering 404 for a quarter. A checkout without a built
    book is the normal state everywhere else, so nothing is said there."""
    if production is None:
        production = bool(os.getenv("RENDER", "").strip())
    if not production:
        return []
    out: list[str] = []
    book = book_dir if book_dir is not None else _book_dir
    audio = audio_dir if audio_dir is not None else _lecture_audio_dir
    if not book.exists():
        out.append(f"The book is not built ({book}): every Book link answers 404 "
                   "(quarto render in the image, DEPLOY.md).")
    if not audio.exists():
        out.append(f"The narration directory is absent ({audio}): every clip under "
                   "/lecture-audio answers 404 until the audio seed lands.")
    return out


def _email_boot_warnings(warnings: list[str]) -> None:
    """One deduped operator email listing what the app started without."""
    try:
        from tutor import alerts

        alerts.send_alert(
            "boot-degraded",
            f"{IDENTITY.name} started without some of its data",
            "The app is up, but boot reported:\n\n- " + "\n- ".join(warnings)
            + "\n\nThe next boot retries each failed seed (entrypoint.sh).",
        )
    except Exception:
        logger.exception("could not send the boot warning email")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup: logging at level %s (conversation content logging: %s)",
                _LOG_LEVEL, "on" if content_enabled() else "off")
    warnings = _boot_warnings()
    # A database that is down at boot no longer stops the app (plan step 2.7,
    # finding C4): uvicorn used to exit, and Render restarted it in a loop in
    # which /healthz and its self-heal could never run. /healthz retries the
    # setup after each healthy probe.
    if not ensure_database_ready(app_engine()):
        warnings.append("Database setup failed at boot (unreachable or paused?); "
                        "/healthz retries it once the database answers.")
    # ADMIN_EMAILS has no default in code (plan step 2.9, finding C14): with
    # it empty nobody can open /admin, and with the whitelist on (its
    # default) and an empty whitelist nobody can log in at all.
    if not get_config()["admin_emails"]:
        warnings.append("ADMIN_EMAILS is empty: nobody can open /admin, and while the "
                        "whitelist is on only whitelisted addresses can log in.")
    # Code execution switched on beside a database (refused), or without the
    # namespace sandbox (plan step 6.9, findings B6 and B8). Empty while it is off.
    from tutor.agent import run_code

    warnings.extend(run_code.boot_warnings())
    warnings.extend(_data_warnings())
    # Verify self-heal credentials once at boot (read-only probe; never fails
    # startup) so /healthz can show whether the token + ref actually work.
    try:
        from tutor import selfheal
        selfheal.check_access()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("self-heal access check skipped: %s", exc)
    if warnings:
        for line in warnings:
            logger.warning("boot: %s", line)
        import asyncio

        # The email is an HTTP call: send it from the executor, not the loop.
        asyncio.get_running_loop().run_in_executor(None, _email_boot_warnings, warnings)
    # Load the retrieval embedder and every collection's index off the loop,
    # so the first question after a deploy does not wait on them (B0).
    # Fail-soft, and a no-op without a store.
    import asyncio as _asyncio
    from tutor.agent import tools as _tools

    _asyncio.get_running_loop().run_in_executor(None, _tools.warm_up)
    cfg = get_config()
    if cfg["use_claude_cli"]:
        print("Agent mode: claude CLI (subscription) — no API key required", flush=True)
    else:
        print("Agent mode: Anthropic API key", flush=True)
    yield
    # A reply still being written when the process goes (a deploy): give it
    # a few seconds, then cancel it, so its turn records the interruption
    # (R31). Left to the loop's own teardown it ran only when uvicorn
    # happened to be PID 1.
    await _close_running_turns()
    # Close the pooled connections on shutdown so a deploy does not leave
    # them open at the Supabase pooler (plan step 2.6).
    dispose_app_engines()


async def _close_running_turns(grace: float = 3.0) -> None:
    import asyncio as _aio

    from tutor import streambuf

    tasks = [b.task for b in streambuf.live_buffers() if b.task is not None and not b.task.done()]
    if not tasks:
        return
    try:
        _done, pending = await _aio.wait(tasks, timeout=grace)
        for t in pending:
            t.cancel()
        if pending:
            await _aio.wait(pending, timeout=grace)
    except Exception:
        logger.exception("shutdown: could not close the running turns")


_cfg = get_config()

# The API docs list every route, admin mutations included, and load Swagger
# from a CDN: off unless API_DOCS is set, for local work (plan step 4.5,
# finding C18).
_api_docs = os.getenv("API_DOCS", "").strip().lower() in ("1", "true", "yes")
app = FastAPI(title=f"{IDENTITY.name}: Digital Teaching Agent", lifespan=lifespan,
              docs_url="/docs" if _api_docs else None,
              redoc_url="/redoc" if _api_docs else None,
              openapi_url="/openapi.json" if _api_docs else None)

# Session middleware (needed for Google OAuth state parameter). Secure behind
# an https BASE_URL (plan step 4.5, finding C19: it never was).
app.add_middleware(SessionMiddleware, secret_key=_cfg["secret_key"],
                   https_only=_cfg["base_url"].startswith("https://"))


# Security headers on every response (plan step 4.5, finding C20). The CSP is
# report-only for now: the pages still use inline scripts and Alpine's eval,
# and the decks load Reveal.js from a CDN until step 6.6 vendors it.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy-Report-Only": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
}


class SecurityHeaders:
    """Pure ASGI, so a streamed reply passes through untouched; a header a
    route set itself wins."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders

                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)

# Static assets: tutor/web/static (CSS/JS) and output/ (slide PNGs)
_static_dir = _ROOT / "tutor" / "web" / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """The tab icon at the path every browser tries first; without it each
    page view made a 404 the error ledger counted against the student."""
    return FileResponse(_static_dir / "icon-192.png", media_type="image/png")


class SafeStaticFiles(StaticFiles):
    """StaticFiles where a path with a NUL byte (a hand-edited ``%00``) is
    "not found": ``os.path.realpath`` raises ValueError on it, which
    Starlette does not catch, so it was a 500 and one alert per distinct
    path (R6)."""

    def lookup_path(self, path):
        try:
            return super().lookup_path(path)
        except ValueError:
            return "", None


app.mount("/static", SafeStaticFiles(directory=str(_static_dir)), name="static")


class RevalidateStaticFiles(SafeStaticFiles):
    """StaticFiles that forces browsers/CDNs to revalidate before reuse.

    Slide decks are re-seeded into the (ephemeral) container fs on every boot,
    so the file behind a URL changes across deploys. Starlette's StaticFiles
    sends no Cache-Control, which lets browsers apply *heuristic* caching and
    serve a stale deck without ever asking the server — that is what made the
    "lecture-1 is black" fix appear not to land. `no-cache` means "always
    revalidate"; the ETag/Last-Modified still yield cheap 304s when unchanged.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# How long a checked session cookie is trusted for static files: an audio clip
# is several range requests, and each would otherwise be a database lookup.
_GATE_TTL_SECONDS = 60
_gate_cache: dict[str, float] = {}


def _session_allows(app_, token: str, request=None) -> bool:
    """Whether ``token`` is a live session of a student who may log in: the
    same checks as every API route (get_current_student), on the app's
    database dependency so a test override applies."""
    import hashlib
    import inspect
    import time

    from fastapi import HTTPException

    from tutor.auth.deps import get_current_student, get_db

    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    if _gate_cache.get(key, 0.0) > now:
        return True
    provider = app_.dependency_overrides.get(get_db, get_db)
    made = provider()
    db = next(made) if inspect.isgenerator(made) else made
    try:
        get_current_student(request, session_token=token, db=db)
    except HTTPException:
        return False
    finally:
        if inspect.isgenerator(made):
            made.close()
    if len(_gate_cache) > 10_000:
        _gate_cache.clear()
    _gate_cache[key] = now + _GATE_TTL_SECONDS
    return True


class SessionGate:
    """Serve a static mount only to a signed-in student (plan step 2.9,
    finding C8; decision 2's default). The decks, their notes, and the
    narration in the cloned voice were public URLs; the book and the
    explorables stay public. Narration scripts are never served."""

    async def get_response(self, path, scope):
        from starlette.concurrency import run_in_threadpool
        from starlette.requests import Request

        request = Request(scope)
        token = request.cookies.get("session_token")
        if not token or not await run_in_threadpool(_session_allows, request.app, token, request):
            # A page navigation turns this into the login redirect (below).
            raise StarletteHTTPException(status_code=401, detail="Not authenticated")
        if path.rsplit("/", 1)[-1] == "script.md":
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)  # type: ignore[misc]


class DeckStaticFiles(RevalidateStaticFiles):
    """The slide decks, each served with the deck tracking tag
    (step 1.6; tutor/deck_tracking.py).

    A deck's ``index.html`` is read and returned with the tag before its
    ``</body>`` instead of streamed from disk, so the decks in the current
    slide seed are instrumented without a re-render or a new release asset.
    No validators go with it: a browser that cached a deck before this
    change must not keep its copy on a 304. Every other file (the clips,
    frames, notes) is served as before.
    """

    async def get_response(self, path, scope):
        import stat as stat_mod

        from fastapi.responses import HTMLResponse
        from starlette.concurrency import run_in_threadpool

        from tutor import deck_tracking

        if path.rsplit("/", 1)[-1] == "index.html":
            try:
                full_path, stat_result = await run_in_threadpool(self.lookup_path, path)
            except (OSError, ValueError):
                # A name too long for the filesystem: Starlette's own lookup
                # turns this into its 404; calling lookup_path directly did
                # not (R6).
                return await super().get_response(path, scope)
            if stat_result is not None and stat_mod.S_ISREG(stat_result.st_mode):
                html = await run_in_threadpool(Path(full_path).read_text, "utf-8", "replace")
                return HTMLResponse(deck_tracking.inject(html), headers={"Cache-Control": "no-cache"})
        return await super().get_response(path, scope)


class GatedRevalidateStaticFiles(SessionGate, DeckStaticFiles):
    pass


class GatedStaticFiles(SessionGate, SafeStaticFiles):
    pass


_slides_dir = _cfg["output_dir"]
_slides_dir.mkdir(parents=True, exist_ok=True)
app.mount("/slides", GatedRevalidateStaticFiles(directory=str(_slides_dir)), name="slides")

# Lecture narration audio. Decks reference clips as /lecture-audio/<course>/
# lecture-NN/audio/slide-NN.mp3 (absolute), so the ~270 MB of MP3s can live on the
# persistent disk in prod (seeded once, LECTURE_AUDIO_DIR) instead of the ephemeral
# deck tarball. Locally it defaults to output/, where render-audio writes them.
# Plain StaticFiles (not Revalidate): clips are immutable, so browser caching helps
# streaming. Mount only if the directory exists so a fresh checkout doesn't fail.
_lecture_audio_dir = _cfg["lecture_audio_dir"]
if _lecture_audio_dir.exists():
    app.mount("/lecture-audio", GatedStaticFiles(directory=str(_lecture_audio_dir)), name="lecture-audio")

_book_dir = _ROOT / "book" / "_book"
if _book_dir.exists():
    # html=True so a directory request (e.g. the Quarto brand/logo link to the
    # site root "/book/") serves index.html instead of 404ing.
    app.mount("/book", SafeStaticFiles(directory=str(_book_dir), html=True), name="book")

# Interactive explorables for threshold concepts (Engram P4). Static, offline,
# one HTML file per goal id; RevalidateStaticFiles so a re-authored file is not
# served stale from browser cache across deploys (same rationale as /slides).
_explorables_dir = EXPLORABLES_DIR
_explorables_dir.mkdir(parents=True, exist_ok=True)
app.mount("/explorables", RevalidateStaticFiles(directory=str(_explorables_dir)), name="explorables")

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(map_router.router)
app.include_router(path_router.router)
app.include_router(review_router.router)
app.include_router(lectures_router.router)
app.include_router(status_router.router)
app.include_router(onboarding_router.router)
app.include_router(instructor_router.router)
app.include_router(errors_router.router)
app.include_router(feedback_router.router)


def _alert_unhandled(path: str, exc: Exception, ref: str = "", app_=None,
                     facts: Optional[request_errors.RequestFacts] = None) -> None:
    """The 500 handler's blocking work: a Supabase restore check (two HTTP
    calls) on a database error, the alert email (naming the reference the
    student saw), and the request_errors row. Never raises."""
    try:
        from sqlalchemy.exc import InterfaceError, OperationalError
        from sqlalchemy.exc import TimeoutError as PoolTimeout

        from tutor import alerts, selfheal

        seen = f"The student saw reference {ref}." if ref else ""
        if isinstance(exc, (OperationalError, InterfaceError)):
            restore = selfheal.restore_supabase_if_down(
                reason=f"unhandled DB error on {path}"
            )
            alerts.alert_exception(
                "db-down", path, exc, note=f"Database error. Self-heal: {restore}. {seen}".strip()
            )
        elif isinstance(exc, PoolTimeout):
            # Every connection in the pool was checked out for DB_POOL_TIMEOUT:
            # one alert for the episode, not one per path that waited
            # (R2). The database is up, so no restore.
            alerts.alert_exception(
                "db-pool", path, exc,
                note=f"The connection pool was full (DB_POOL_SIZE + DB_MAX_OVERFLOW in use). {seen}".strip(),
            )
        else:
            alerts.alert_exception(f"exc:{type(exc).__name__}:{path}", path, exc, note=seen)
    except Exception:
        logger.exception("alerting failed inside the 500 handler")
    if facts is not None and app_ is not None:
        try:
            request_errors.record_from_facts(app_, facts, 500, detail=type(exc).__name__, ref=ref)
        except Exception:
            logger.exception("recording the 500 on %s failed", path)


# The last alert job, so a test can wait for it (the handler does not).
_last_alert_job = None


@app.exception_handler(Exception)
async def _alert_on_unhandled(request, exc: Exception):
    """Email the operator on any unhandled 500 (deduped in tutor.alerts), and
    kick off a Supabase restore when the error is the database being
    unreachable (tutor.selfheal). Starlette's ServerErrorMiddleware re-raises
    after this returns, so uvicorn still logs the full traceback; this handler
    only adds alerting plus a clean client response. It must never raise.

    The alert and restore calls (up to about 20 s of HTTP, exactly when the
    database is down) run in the executor and the 500 goes out at once: on
    the event loop they stalled every other request (plan step 2.6, C12).
    """
    global _last_alert_job
    from starlette.requests import ClientDisconnect
    from starlette.responses import PlainTextResponse

    if isinstance(exc, ClientDisconnect):
        # The browser closed the connection while the body was still being
        # read (a tab closed on a beacon, a navigation mid-upload): nobody
        # is waiting for the answer and nothing failed here, so no alert,
        # no reference, no request_errors row (the review of 2026-10-07).
        return PlainTextResponse("Client disconnected", status_code=400, headers=SECURITY_HEADERS)
    ref = request_errors.new_ref()
    facts: Optional[request_errors.RequestFacts] = None
    try:
        facts = request_errors.RequestFacts.from_request(request)
    except Exception:
        logger.exception("could not read the failed request")
    try:
        import asyncio

        _last_alert_job = asyncio.get_running_loop().run_in_executor(
            None, _alert_unhandled, request.url.path, exc, ref, request.scope.get("app"), facts,
        )
    except Exception:
        logger.exception("could not schedule the 500 alert")
    # Sent by the outermost middleware, past SecurityHeaders: add them here.
    # A navigation gets the error page with the reference (review step 0.1,
    # F61); anything else, or a failure to render, the plain text.
    if facts is not None and facts.wants_page:
        try:
            heading, message = request_errors.copy_for(500, request_errors.PAGE)
            return templates.TemplateResponse(
                request, "error.html",
                {"student": None, "is_admin": False, "is_instructor": False, "course": DEFAULT_COURSE,
                 "status": 500, "ref": ref, "heading": heading, "message": message,
                 "kind": request_errors.PAGE, "reported": False},
                status_code=500, headers=SECURITY_HEADERS,
            )
        except Exception:
            logger.exception("could not render the error page for a 500")
    return PlainTextResponse(f"Internal Server Error (reference {ref})", status_code=500,
                             headers=SECURITY_HEADERS)


@app.exception_handler(StarletteHTTPException)
async def _http_error_page(request: Request, exc: StarletteHTTPException):
    """A page navigation without a live session goes to the login page; every
    other 4xx and 5xx is recorded, and a navigation gets an HTML page.

    Sessions expire every JWT_EXPIRY_DAYS, so a bookmarked or restored /chat,
    /map, or /instructor tab used to show raw JSON ("Not authenticated").
    A GET that asks for HTML gets 303 /auth/login?next=<the page> and the
    stale cookie is cleared. fetch() calls and API clients do not ask for
    HTML, so they keep the JSON 401 their code expects.

    Every other status (a 404 on a dead link, a missing deck under /slides,
    a 403, 405, 422, 429) used to be FastAPI's JSON with no record anywhere
    (F1 and F2). Now the request is one request_errors row (the student when
    the cookie is live, the same-site referrer, the agent's class), a dead
    link emails the operator, and a top-level navigation renders error.html
    with a reference and a way back; fetches, and the chat's deck iframe
    (Sec-Fetch-Dest: iframe), keep their JSON bodies, so page scripts see
    what they expect. The record runs in the threadpool (plan step 2.6).
    """
    from starlette.requests import ClientDisconnect
    from starlette.responses import PlainTextResponse

    if isinstance(exc.__cause__, ClientDisconnect):
        # FastAPI wraps a disconnect while it parses a form or an upload as
        # a 400 "error parsing the body": the student left mid-upload, which
        # is not an error page anyone saw, so no row and no alert
        # (R11).
        return PlainTextResponse("Client disconnected", status_code=400, headers=SECURITY_HEADERS)
    accepts_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == 401:
        dest = (request.headers.get("sec-fetch-dest") or "document").strip().lower()
        navigation = accepts_html and (request.method == "GET" or (request.method == "POST" and dest == "document"))
        if not navigation:
            return await http_exception_handler(request, exc)
        token = request.cookies.get("session_token")
        if token:
            # A session that ran out leaves one row in the login trail
            # (review step 0.6, F31); a visitor with no cookie leaves none.
            try:
                from tutor.auth import attempts

                await run_in_threadpool(
                    attempts.record_expiry, request.scope.get("app"), token=token,
                    host=request.headers.get("host"), user_agent=request.headers.get("user-agent"))
            except Exception:
                logger.exception("recording a session expiry failed")
        if request.method == "GET" and dest != "iframe":
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        else:
            # A form posted with a lapsed session (the course picker, the
            # Tell-the-instructor button) was raw JSON (R19), and the chat's
            # slides pane opened the login page inside itself (R20): after
            # signing in, the page the form or the frame was on, with its
            # query, never the POST or the deck.
            back = request_errors.same_site_return(request.headers.get("referer"), request.headers.get("host"))
            target = back if back and back.split("?", 1)[0] != request.url.path else "/chat"
        login = f"/auth/login?next={quote(target, safe='')}"
        response: Response
        if dest == "iframe":
            # A sentence in the pane, and a link that signs in the whole page
            # it sits in; chat.js points it at the lecture showing and keeps
            # the typed question as a draft.
            response = templates.TemplateResponse(request, "signed_out_frame.html", {"login": login},
                                                  status_code=401)
        else:
            response = RedirectResponse(login, status_code=303)
        # Cleared either way: a frame that kept the cookie wrote one more
        # expiry row on every lecture picked after it (the Stage 2 review).
        response.delete_cookie("session_token")
        return response
    if exc.status_code < 400:
        return await http_exception_handler(request, exc)
    recorded = None
    wants_page = False
    try:
        facts = request_errors.RequestFacts.from_request(request)
        wants_page = facts.wants_page
        detail = type(exc).__name__ if exc.status_code >= 500 else None
        recorded = await run_in_threadpool(
            request_errors.record_from_facts, request.scope.get("app"), facts, exc.status_code, detail=detail,
        )
    except Exception:
        logger.exception("recording a %s on %s failed", exc.status_code, request.url.path)
    if recorded is not None and wants_page:
        heading, message = request_errors.copy_for(exc.status_code, recorded.kind)
        return templates.TemplateResponse(
            request, "error.html",
            {**recorded.nav, "status": exc.status_code, "ref": recorded.ref, "heading": heading,
             "message": message, "kind": recorded.kind, "reported": False},
            status_code=exc.status_code, headers=getattr(exc, "headers", None) or None,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _validation_error_page(request: Request, exc: RequestValidationError):
    """A parameter FastAPI could not read: FastAPI's raw 422 JSON on a page,
    with no record (R13). Now it takes the path every other 4xx takes: a
    row, and the designed page for a navigation, while a fetch keeps the 422
    JSON its script reads. A path
    segment that is not what the route takes (``/feedback/abc``) is a path
    that does not exist, a 404; the routes a student reaches parse their
    numbers leniently and fall back, so this is the net under them."""
    from fastapi.encoders import jsonable_encoder

    errors = exc.errors()
    if any(tuple(err.get("loc", ()))[:1] == ("path",) for err in errors):
        return await _http_error_page(request, StarletteHTTPException(status_code=404))
    try:
        # A body that is not UTF-8 (bytes posted as text/plain to a JSON
        # route) is echoed in "input", which jsonable_encoder decodes
        # strictly: a UnicodeDecodeError, a 500 and an alert (the Stage 2
        # review; FastAPI's own handler does the same).
        detail = jsonable_encoder(errors, custom_encoder={bytes: lambda b: b.decode("utf-8", "replace")})
    except Exception:
        detail = [{k: v for k, v in err.items() if k in ("type", "loc", "msg")} for err in errors]
    return await _http_error_page(request, StarletteHTTPException(status_code=422, detail=detail))


@app.get("/")
def root(session_token: Optional[str] = Cookie(default=None)):
    """Redirect to /chat if already logged in, otherwise to /auth/login.

    A plain ``def``: FastAPI runs it in the threadpool, so its database
    lookup cannot stall the event loop (plan step 2.6, C1)."""
    if session_token:
        from tutor.auth.deps import session_student
        from tutor.db.models import get_session_factory

        try:
            db = get_session_factory(app_engine())()
            try:
                # An invalid, expired, or revoked cookie is None: to login.
                if session_student(db, session_token) is not None:
                    return RedirectResponse("/chat")
            finally:
                db.close()
        except Exception:
            # A real failure (e.g. DB outage) must not be silently masked as
            # "not logged in"; log it, then still degrade to the login page.
            logger.exception("root(): unexpected error validating session cookie")
    return RedirectResponse("/auth/login")
