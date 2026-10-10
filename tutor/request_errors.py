"""Every error page a student was shown, recorded and reported.

Until 2026-10-07 a 404 was FastAPI's raw JSON and left no trace but a line in
uvicorn's access log: a dead link in the map, a deck missing from the seed, a
stale bookmark after a deploy, all reported by students if at all
(section 3.1 and step 0.1). Now the exception handlers in ``tutor/main.py``
write one ``request_errors`` row per 4xx or 5xx the app answered (401s
excepted: a session that expired is the login trail's business), render an HTML page for a navigation, and, for the
one case that means a link the app handed out is dead (a signed-in student,
a same-site referrer, a browser, a 404), email the operator through
``tutor.alerts`` with the usual cooldown per path family. Bots and mail
scanners are counted and never alerted.

Rows carry ids and paths only: the query string is dropped, a magic-link
token in the path is redacted, the referrer is kept only when it is this
site's, and the user agent is reduced to a class. The 90-day prune in
``scripts/retention_sweep.py`` and the erasure scope cover the table.
"""

from __future__ import annotations

import collections
import datetime
import inspect
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Optional, cast
from urllib.parse import urlsplit

from sqlalchemy.orm import Session as DBSession
from starlette.requests import Request

from tutor.db.models import RequestError, Student
from tutor.courses import DEFAULT_COURSE, IDENTITY

log = logging.getLogger("tutor.errors")

PAGE, FETCH, DECK, AUDIO, BOOK, EXPLORABLE, STATIC, REPORT = (
    "page", "fetch", "deck", "audio", "book", "explorable", "static", "report")
BROWSER, BOT, SCANNER = "browser", "bot", "scanner"

PATH_MAX = 200
# Paths whose errors are noise by design: the throttled health probe, and
# the requests a browser makes on its own (the tab icon, iOS's touch icon,
# the well-known probes), which no student followed a link to.
_SKIP_PREFIXES = ("/healthz", "/favicon.ico", "/apple-touch-icon", "/.well-known/")
# A bearer credential in a path (the magic link) is never stored.
_TOKEN_PATHS = (re.compile(r"^(/auth/magic/)[^/?#]+"),)
_BOT_MARKERS = ("bot", "crawl", "spider", "slurp", "python-requests", "python-urllib", "curl/", "wget/",
                "go-http-client", "httpx/", "scrapy", "facebookexternalhit", "headlesschrome", "lighthouse",
                "java/", "libwww", "okhttp")
_SCANNER_MARKERS = ("proofpoint", "urldefense", "mimecast", "barracuda", "safelinks", "linkcheck",
                    "link preview", "whatsapp", "slackbot", "twitterbot", "discordbot")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_agent(user_agent: Optional[str]) -> str:
    """``browser``, ``bot`` or ``scanner`` from the user agent string; no
    user agent at all is a bot."""
    ua = (user_agent or "").strip().lower()
    if not ua:
        return BOT
    if any(m in ua for m in _SCANNER_MARKERS):
        return SCANNER
    if any(m in ua for m in _BOT_MARKERS):
        return BOT
    return BROWSER


def scrub_path(path: str) -> str:
    """The path as stored: no query, a magic-link token redacted, clipped."""
    path = (path or "").split("?", 1)[0].split("#", 1)[0] or "/"
    for pattern in _TOKEN_PATHS:
        path = pattern.sub(r"\1[token]", path)
    return path[:PATH_MAX]


def kind_for(path: str, accepts_html: bool) -> str:
    """What the student was after: a deck, a clip, a book chapter, an
    explorable, a static file, a page, or a fetch from a page script."""
    if path.startswith("/slides/"):
        return AUDIO if path.endswith((".mp3", ".wav", ".m4a")) else DECK
    if path.startswith("/lecture-audio/"):
        return AUDIO
    if path.startswith("/book/"):
        return BOOK
    if path.startswith("/explorables/"):
        return EXPLORABLE
    if path.startswith("/static/"):
        return STATIC
    return PAGE if accepts_html else FETCH


def same_site_referrer(referrer: Optional[str], host: Optional[str]) -> Optional[str]:
    """The referrer's path when it is this site's (same host, or relative);
    None for another site or none at all."""
    if not referrer:
        return None
    try:
        parts = urlsplit(referrer)
    except ValueError:
        return None
    if parts.netloc and host and parts.netloc.lower() != host.lower():
        return None
    if parts.netloc and not host:
        return None
    return scrub_path(parts.path or "/")


def same_site_return(referrer: Optional[str], host: Optional[str]) -> Optional[str]:
    """The referrer's path with its query when it is this site's, for the
    login page's ``next`` (which ``_safe_next`` checks again); None
    otherwise. Unlike ``same_site_referrer`` it is not scrubbed: it is never
    stored, and the query is what reopens the page as it was."""
    if same_site_referrer(referrer, host) is None:
        return None
    parts = urlsplit(referrer or "")
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def family(path: str) -> str:
    """The alert key's path family: the first two segments (``/slides/AST101``,
    ``/lectures/AST102``, ``/book/02-moon-phases.html``), so one dead deck does
    not silence a dead chapter and one chapter does not email per student."""
    segments = [s for s in path.split("/") if s]
    return "/" + "/".join(segments[:2])


def new_ref() -> str:
    """A short reference a student can quote: six hex characters."""
    return secrets.token_hex(3).upper()


def copy_for(status: int, kind: str) -> tuple[str, str]:
    """The heading and the sentence the error page shows."""
    if status == 404:
        if kind in (DECK, AUDIO):
            return ("Those slides are not here",
                    "The lecture you asked for is not available right now. The lectures that "
                    "are ready are listed on the Lectures page.")
        return ("That page is not here",
                "The link you followed points at something that does not exist, or that was "
                f"moved. If a page on this site sent you here, {IDENTITY.instructor_short} will "
                "want to know.")
    if status == 403:
        return ("You cannot open that", "This needs a different account.")
    if status == 429:
        return ("Too many requests", "Please wait a minute and try again.")
    if status >= 500:
        return ("Something broke on our side",
                "The error has been recorded and the site's operator has been emailed. "
                "Please try again in a minute.")
    return ("That request did not make sense", "The address was not quite right.")


# ---------------------------------------------------------------------------
# What a request tells us, taken on the event loop and used off it
# ---------------------------------------------------------------------------

@dataclass
class RequestFacts:
    method: str
    path: str
    referrer_path: Optional[str]
    user_agent: Optional[str]
    token: Optional[str]
    accepts_html: bool
    # A top-level navigation (Sec-Fetch-Dest absent or "document"): the one
    # that gets the error page. The chat's deck iframe asks for HTML too, and
    # it needs the JSON its script detects a missing deck by (chat.js
    # relayoutDeck), not a page with scripts of its own inside the panel.
    top_level: bool = True

    @classmethod
    def from_request(cls, request: Request) -> "RequestFacts":
        headers = request.headers
        dest = (headers.get("sec-fetch-dest") or "document").strip().lower()
        return cls(
            method=request.method,
            path=scrub_path(request.url.path),
            referrer_path=same_site_referrer(headers.get("referer"), headers.get("host")),
            user_agent=headers.get("user-agent"),
            token=request.cookies.get("session_token"),
            accepts_html="text/html" in headers.get("accept", ""),
            top_level=dest == "document",
        )

    @property
    def wants_page(self) -> bool:
        # A form a page posts (the course picker, the Tell-the-instructor
        # button) is a navigation too: its 4xx was JSON in the tab (R19). A
        # fetch says Sec-Fetch-Dest "empty" and does not ask for HTML.
        return self.method in ("GET", "POST") and self.accepts_html and self.top_level

    @property
    def skipped(self) -> bool:
        return self.path.startswith(_SKIP_PREFIXES)


@dataclass
class Recorded:
    ref: str
    kind: str
    agent: str
    student: Optional[Student]
    nav: dict[str, Any]
    alerted: str = ""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def record(db: DBSession, *, status: int, method: str, path: str, referrer_path: Optional[str],
           student_id: Optional[int], user_agent: Optional[str], kind: Optional[str] = None,
           ref: Optional[str] = None, detail: Optional[str] = None,
           accepts_html: bool = True) -> Optional[RequestError]:
    """One row, committed on its own. Never raises: recording an error must
    not make a second one; a failure is logged and rolled back."""
    row = RequestError(
        status=int(status), method=(method or "GET")[:8], path=scrub_path(path),
        referrer_path=(referrer_path or None), student_id=student_id,
        kind=(kind or kind_for(scrub_path(path), accepts_html))[:16],
        agent=classify_agent(user_agent), ref=(ref or new_ref())[:8],
        detail=(detail or None) and str(detail)[:120],
    )
    try:
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:  # pragma: no cover - the session is already broken
            pass
        log.warning("request error not recorded (%s %s -> %s)", method, path, status, exc_info=True)
        return None
    return row


def dead_link(row: RequestError) -> bool:
    """The one case worth an email at once: a signed-in student's browser
    followed a link on this site to a 404. A stale bookmark (no referrer), a
    bot, a scanner, and a fetch that 404s on purpose are not."""
    return (row.status == 404 and row.agent == BROWSER and row.student_id is not None
            and bool(row.referrer_path) and row.kind != FETCH)


def alert_dead_link(row: RequestError, student: Optional[Student]) -> str:
    from tutor import alerts
    from tutor.config import get_config

    who = (f"{student.name or ''} <{student.email}>".strip() if student is not None
           else f"student {row.student_id}")
    body = (
        f"{who} followed a link on {row.referrer_path} to {row.method} {row.path} and got a {row.status}.\n"
        f"Reference {row.ref}. Kind: {row.kind}.\n\n"
        f"Every error of the day: {get_config()['base_url']}/admin (Today, \"Errors students hit\").\n"
        "One email per path family per hour."
    )
    return alerts.send_alert(f"404:{family(row.path)}", f"Dead link: {row.path} (from {row.referrer_path})", body)


def _db_for(app_) -> tuple[DBSession, Any]:
    """A session through the app's database dependency, so a test override
    applies; returns the session and the generator to close."""
    from tutor.auth.deps import get_db

    provider = app_.dependency_overrides.get(get_db, get_db) if app_ is not None else get_db
    made = provider()
    if inspect.isgenerator(made):
        return cast(DBSession, next(made)), made
    return cast(DBSession, made), None


def record_from_facts(app_, facts: RequestFacts, status: int, *, detail: Optional[str] = None,
                      ref: Optional[str] = None) -> Optional[Recorded]:
    """Everything the handlers need, off the event loop: the student behind
    the cookie, the row, the alert when it is a dead link, and the header's
    context for the page. None for a path that is never recorded."""
    from tutor.auth.deps import session_student

    if facts.skipped:
        return None
    db, gen = _db_for(app_)
    try:
        student = session_student(db, facts.token) if facts.token else None
        row = record(
            db, status=status, method=facts.method, path=facts.path, referrer_path=facts.referrer_path,
            student_id=student.id if student is not None else None, user_agent=facts.user_agent,
            ref=ref, detail=detail, accepts_html=facts.accepts_html,
        )
        if row is None:
            return Recorded(ref=ref or new_ref(), kind=kind_for(facts.path, facts.accepts_html),
                            agent=classify_agent(facts.user_agent), student=student,
                            nav=_safe_nav(student, db))
        alerted = ""
        if dead_link(row):
            try:
                alerted = alert_dead_link(row, student)
            except Exception:  # noqa: BLE001 - the alert path must never break the page
                log.warning("dead-link alert failed for %s", row.path, exc_info=True)
        return Recorded(ref=row.ref, kind=row.kind, agent=row.agent, student=student,
                        nav=_safe_nav(student, db), alerted=alerted)
    finally:
        # A generator dependency owns its session (get_db); a plain override
        # (a test's shared session) is the caller's and stays open.
        if gen is not None:
            gen.close()


def _safe_nav(student: Optional[Student], db: DBSession) -> dict[str, Any]:
    from tutor.nav import nav_context

    try:
        return nav_context(student, db)
    except Exception:  # noqa: BLE001 - a header is not worth a second error
        log.warning("nav context failed on the error page", exc_info=True)
        return {"student": None, "is_admin": False, "is_instructor": False, "course": DEFAULT_COURSE}


def report(db: DBSession, student: Student, ref: str, user_agent: Optional[str]) -> Optional[RequestError]:
    """The Tell-the-instructor button: a ``report`` row pointing at the error
    the student saw (by its reference), and one email per student per hour."""
    from tutor import alerts
    from tutor.config import get_config

    ref = (ref or "").strip().upper()[:8]
    original = (
        db.query(RequestError).filter(RequestError.ref == ref, RequestError.kind != REPORT)
        .order_by(RequestError.id.desc()).first()
    ) if ref else None
    row = record(
        db, status=original.status if original else 0, method=original.method if original else "GET",
        path=original.path if original else "/", referrer_path=original.referrer_path if original else None,
        student_id=student.id, user_agent=user_agent, kind=REPORT, detail=ref or None,
    )
    if row is None:
        return None
    who = f"{student.name or ''} <{student.email}>".strip()
    where = (f"{original.method} {original.path} answered {original.status}"
             if original else "an error whose reference was not found")
    body = (f"{who} pressed \"Tell {IDENTITY.instructor_short}\" on {where} (reference {ref or 'none'}).\n"
            f"The day's errors: {get_config()['base_url']}/admin (Today).")
    try:
        alerts.send_alert(f"report:{student.id}", f"A student reported a problem: {student.name or student.email}", body)
    except Exception:  # noqa: BLE001
        log.warning("report alert failed", exc_info=True)
    return row


# ---------------------------------------------------------------------------
# Reading: the Today card and the daily digest
# ---------------------------------------------------------------------------

@dataclass
class TopPath:
    path: str
    status: int
    hits: int
    students: int
    referrers: list[str]


@dataclass
class ErrorSummary:
    total: int = 0            # from browsers
    bots: int = 0             # bots and scanners, counted, never alerted
    students: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    top: list[TopPath] = field(default_factory=list)
    reports: list[tuple[datetime.datetime, str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.total or self.reports)


def summary(db: DBSession, since: datetime.datetime, until: Optional[datetime.datetime] = None,
            limit: int = 8) -> ErrorSummary:
    """The window's errors from browsers, by kind and by path, bots counted
    apart, and the students' reports with who sent them."""
    q = db.query(RequestError).filter(RequestError.ts >= since)
    if until is not None:
        q = q.filter(RequestError.ts < until)
    rows = q.order_by(RequestError.ts.desc()).limit(5000).all()
    out = ErrorSummary()
    hits: collections.Counter = collections.Counter()
    who: dict[tuple[str, int], set] = collections.defaultdict(set)
    refs: dict[tuple[str, int], set] = collections.defaultdict(set)
    students: set[int] = set()
    names: dict[int, str] = {}
    for r in rows:
        if r.kind == REPORT:
            if r.student_id not in names:
                s = db.get(Student, r.student_id) if r.student_id else None
                names[r.student_id or 0] = (s.name or s.email) if s else f"student {r.student_id}"
            out.reports.append((r.ts, names[r.student_id or 0], r.path))
            continue
        if r.agent != BROWSER:
            out.bots += 1
            continue
        out.total += 1
        out.by_kind[r.kind] = out.by_kind.get(r.kind, 0) + 1
        key = (r.path, r.status)
        hits[key] += 1
        if r.student_id is not None:
            who[key].add(r.student_id)
            students.add(r.student_id)
        if r.referrer_path:
            refs[key].add(r.referrer_path)
    out.students = len(students)
    out.top = [TopPath(path=p, status=s, hits=n, students=len(who[(p, s)]), referrers=sorted(refs[(p, s)])[:3])
               for (p, s), n in hits.most_common(limit)]
    return out
