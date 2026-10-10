"""Admin dashboard routes (HTML + JSON API).

Open to a logged-in user in ADMIN_EMAILS, or with the ADMIN_TOKEN:
  - Header:  X-Admin-Token: <ADMIN_TOKEN>  (scripts and the JSON API)
  - Form:    POST /admin/login with the token, which sets an ``admin_session``
             cookie holding a random id and a fingerprint of the token, signed
             with SECRET_KEY (plan step 4.5, finding C17). The token itself
             never sits in a URL or a cookie, and changing ADMIN_TOKEN ends
             every such session. A token in the query string is not accepted.
"""

from __future__ import annotations

import collections
import datetime
import json
import logging
import secrets
import urllib.parse
from html import escape
from typing import Mapping, Optional

from fastapi import (
    APIRouter, Cookie, Depends, Form, HTTPException, Header, Query, Request,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from tutor.templating import templates
from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from tutor.auth.access import (
    add_to_whitelist,
    find_student,
    is_admin,
    is_instructor,
    list_instructors,
    list_whitelist,
    remove_from_whitelist,
    set_instructor,
    set_whitelist_enabled,
    whitelist_enabled,
)
from tutor import conversations as conv_mod
from tutor import supervision_view
from tutor import transcripts
from tutor.courses import COURSES, IDENTITY
from tutor.auth.deps import get_db, session_student
from tutor.routers.params import bounded_int, optional_int
from tutor.routers.auth import _secure_cookies
from tutor.config import get_config
from tutor.db.models import (
    ExerciseAttempt, LoginAttempt, Message, MessageFeedback, Session as SessionModel, Setting,
    Student, TopicAccess, TurnEvent,
)
from tutor.utctime import utcnow

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Auth
#
# Admin access is granted two ways:
#   1. A logged-in user whose email is in ADMIN_EMAILS (preferred).
#   2. The ADMIN_TOKEN (header / cookie / one-time query param) — bootstrap
#      and script access.
# ---------------------------------------------------------------------------

def _admin_email_from_session(session_token: Optional[str], db: DBSession) -> Optional[str]:
    """Return the email of the logged-in user if they are an admin, else None."""
    student = session_student(db, session_token)
    return student.email if student is not None and is_admin(student.email) else None


def _token_matches(provided: Optional[str]) -> bool:
    required = get_config()["admin_token"]
    if not required or not provided:
        return False
    # Constant-time compare: a plain == on the admin bearer token short-circuits
    # on the first mismatched byte and leaks length/prefix via timing.
    return secrets.compare_digest(provided, required)


ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_DAYS = 7


def _session_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_config()["secret_key"], salt="tutor-admin-session")


def _token_fingerprint() -> str:
    import hashlib

    return hashlib.sha256((get_config()["admin_token"] or "").encode()).hexdigest()[:24]


def issue_admin_session() -> str:
    """A signed cookie value for a token login: a random session id and the
    token's fingerprint, never the token."""
    return _session_signer().dumps({"sid": secrets.token_urlsafe(16), "fp": _token_fingerprint()})


def _admin_session_ok(value: Optional[str]) -> bool:
    """A valid, unexpired admin session issued under the current token."""
    if not value or not get_config()["admin_token"]:
        return False
    try:
        data = _session_signer().loads(value, max_age=ADMIN_SESSION_DAYS * 86400)
    except BadSignature:
        return False
    return isinstance(data, dict) and secrets.compare_digest(str(data.get("fp", "")), _token_fingerprint())


def require_admin(
    db: DBSession = Depends(get_db),
    x_admin_token: Optional[str] = Header(default=None),
    admin_session: Optional[str] = Cookie(alias=ADMIN_SESSION_COOKIE, default=None),
    session_token: Optional[str] = Cookie(default=None),
) -> str:
    """Dependency: allow token- or login-based admins. Returns acting identity."""
    if _token_matches(x_admin_token) or _admin_session_ok(admin_session):
        return "token"
    email = _admin_email_from_session(session_token, db)
    if email:
        return email
    raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

def _parse_json(raw: Optional[str]):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _outcome_correct(kind: str, verdicts: dict) -> Optional[bool]:
    """Did a confidence-bearing turn end up correct? None when undetermined.

    Review turns pass at score >= 2 (of 3); exam turns pass on ``passed``.
    """
    if kind == "review":
        s = verdicts.get("score")
        return (s >= 2) if isinstance(s, (int, float)) else None
    if kind == "exam" and "passed" in verdicts:
        return bool(verdicts["passed"])
    return None


def _calibration_report(pairs: list[tuple[int, bool]]) -> "_Namespace":
    """Calibration summary from (stated confidence, was-correct) pairs (Engram P2).

    Pure. Groups by confidence band and reports observed accuracy vs. the stated
    confidence (the gap is the miscalibration), plus a Brier score over all
    graded-with-confidence turns and the count of high-confidence misses.
    """
    bands: dict[int, dict] = collections.defaultdict(lambda: {"n": 0, "correct": 0})
    brier_sum = 0.0
    for conf, correct in pairs:
        b = bands[conf]
        b["n"] += 1
        b["correct"] += 1 if correct else 0
        brier_sum += (conf / 100.0 - (1.0 if correct else 0.0)) ** 2
    band_rows = []
    for conf in sorted(bands, reverse=True):
        b = bands[conf]
        acc = round(100 * b["correct"] / b["n"]) if b["n"] else 0
        band_rows.append(_Namespace(
            confidence=conf, n=b["n"], correct=b["correct"], accuracy=acc, gap=conf - acc,
        ))
    total = sum(b["n"] for b in bands.values())
    high_conf_wrong = sum(1 for conf, correct in pairs if conf >= 70 and not correct)
    return _Namespace(
        bands=band_rows,
        total=total,
        high_conf_wrong=high_conf_wrong,
        brier=round(brier_sum / total, 3) if total else None,
    )


def _event_row(te: TurnEvent, email: str) -> "_Namespace":
    """Flatten a TurnEvent into a display row exposing every column (S30)."""
    env = _parse_json(te.envelope_json) or {}
    verd = _parse_json(te.verdicts_json) or {}
    det = _parse_json(te.det_check_json) or {}
    goals = _parse_json(te.goal_ids_json) or []
    return _Namespace(
        ts=te.ts,
        email=email,
        kind=te.kind,
        intent=te.intent,
        rung_used=te.rung_used,
        max_rung=env.get("max_rung"),
        move=env.get("move"),
        exam_lockdown=env.get("exam_lockdown") or [],
        allow_exercise=env.get("allow_exercise"),
        must_ground=env.get("must_ground"),
        escalate_review=env.get("escalate_review"),
        envelope_reason=env.get("reason", ""),
        verdict=verd.get("verdict") or verd.get("score"),
        eval_source=verd.get("eval_source"),
        verdicts_raw=te.verdicts_json or "",
        det_check=det,
        det_raw=te.det_check_json or "",
        # The turn records the detector's cut in its verdicts; nothing writes
        # det_check any more, and a record of {"cut": false} is not a cut
        # (plan step 6.2a: the column showed "cut" for any record, and never
        # for a real cut).
        detector_cut=bool(verd.get("detector_cut")) or det.get("cut") is True,
        # What the Verdict, Detector and Rung cells read: the three verdicts
        # the judge can return, a detector that says "clean" when it ran and
        # removed nothing, and the ceiling with the band it moved from. A row
        # that is one supervisor model call rather than a turn says "n/a"
        # instead of showing six blanks (tutor/supervision_view.py).
        is_turn=supervision_view.is_turn(te.kind),
        verdict_cell=supervision_view.verdict_cell(te.kind, verd),
        detector_cell=supervision_view.detector_cell(te.kind, verd, det),
        rung_cell=supervision_view.rung_cell(te.kind, te.rung_used, env),
        grounding_hit=te.grounding_hit,
        model=te.model,
        in_tokens=te.in_tokens,
        out_tokens=te.out_tokens,
        cache_read_tokens=te.cache_read_tokens,
        cost_usd=te.cost_usd,
        latency_ms=te.latency_ms,
        goal_ids=goals,
        envelope_raw=te.envelope_json or "",
    )


# The section tabs, grouped (plan step 4.1, finding D22: twelve ungrouped
# tabs, the analyst tools beside the operational ones). Today leads.
TAB_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("", [("today", "Today")]),
    ("People", [("students", "Students"), ("classes", "Classes"), ("access", "Access"),
                ("logins", "Logins")]),
    ("Learning", [("topics", "Topics"), ("exercises", "Exercises"),
                  ("misconceptions", "Misconceptions")]),
    ("Tutor", [("conversations", "Conversations"), ("timeline", "Timeline"), ("supervision", "Supervision"),
               ("calibration", "Calibration"), ("activity", "Activity"), ("health", "Health")]),
]

TODAY_DAYS = 7


def _today_panel(db: DBSession, days: int = TODAY_DAYS) -> "_Namespace":
    """What an admin lands on (plan step 4.1, decision 13's default): for
    each open class over the last ``days`` days, who is active, who went
    quiet, who never started, and what the class asked about most; the
    replies students flagged; and a health dot. Each class links into
    /instructor for the roster and the drill-down."""
    from tutor import class_metrics, feedback_report, request_errors, student_days
    from tutor import classes as classes_mod
    from tutor.agent.lecture_plan import lecture_titles
    from tutor.auth.access import is_staff
    from tutor.routers.status import _kb_status

    until = utcnow()
    since = until - datetime.timedelta(days=days)
    # The rollup the Outliers card reads (review step 0.9), brought up to
    # date by the pages that compare students; throttled, never raises.
    student_days.refresh(db, now=until)
    cards = []
    enrolled: dict[int, tuple[str, str, int]] = {}      # student id -> (name, email, a class id)
    for c in classes_mod.all_classes(db, include_archived=False):
        roster = classes_mod.class_roster(db, c.id)
        summary, rows = class_metrics.class_report(db, c, roster, since, until)
        titles = lecture_titles(c.course)
        for s in roster:
            if not is_staff(s):
                enrolled.setdefault(s.id, (s.name or "", s.email, c.id))
        cards.append(_Namespace(
            summary=summary,
            quiet=[r for r in rows if r.status == class_metrics.STATUS_DORMANT],
            never=[r for r in rows if r.status == class_metrics.STATUS_NEVER],
            locked_out=[r for r in rows if r.status == class_metrics.STATUS_LOCKED_OUT],
            signed_in=[r for r in rows if r.status == class_metrics.STATUS_SIGNED_IN],
            lectures=[_Namespace(num=n, asked=k, title=titles.get(n, "")) for n, k in summary.top_lectures],
        ))
    # Students who asked anything, anywhere: admins and instructors left out
    # (step 0.9, F39: the owner's own questions counted).
    askers = [
        s for s in db.query(Student)
        .filter(Student.id.in_(
            db.query(Message.student_id).filter(Message.role == "user", Message.created_at >= since).distinct()
        ))
        .all()
    ]
    active_anywhere = sum(1 for s in askers if not is_staff(s))
    # Where students stand over the week, from the rollup: the two highest
    # and two lowest per measure among the open classes' rosters, rank only.
    today = student_days.local_date(until)
    outliers = []
    if enrolled:
        for e in student_days.extremes(db, list(enrolled), today - datetime.timedelta(days=days - 1), today):
            def named(pairs):
                return [_Namespace(name=enrolled[sid][0] or enrolled[sid][1], email=enrolled[sid][1],
                                   class_id=enrolled[sid][2], value=v) for sid, v in pairs]
            outliers.append(_Namespace(key=e.key, label=e.label, highest=named(e.highest),
                                       lowest=named(e.lowest), without=e.without))
    flagged = list(reversed(feedback_report.thumbs_down_in_window(db, since, until)))
    kb = _kb_status()
    from tutor import quotas as quotas_mod

    quota_hits = quotas_mod.hits(db, until - datetime.timedelta(hours=24))
    errors = request_errors.summary(db, until - datetime.timedelta(hours=24), until)
    return _Namespace(
        days=days,
        errors=errors,
        classes=cards,
        active_anywhere=active_anywhere,
        outliers=outliers,
        quota_hits=quota_hits[:8],
        quota_hits_total=len(quota_hits),
        flagged=[_Namespace(f=f, link=feedback_report.conversation_link("", f.feedback_id))
                 for f in flagged[:5]],
        flagged_total=len(flagged),
        flagged_unseen=sum(1 for f in flagged if f.seen_at is None),
        health_ok=kb == "ok",
        health_text="The tutor is healthy" if kb == "ok" else f"Retrieval is degraded ({kb})",
    )


_LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{name} admin sign-in</title>
<link rel="stylesheet" href="/static/vendor/tailwind.css"></head>
<body class="bg-gray-50 text-gray-900"><main class="max-w-sm mx-auto px-4 py-16">
<h1 class="text-xl font-bold mb-4">Admin sign-in</h1>{error}
<form method="post" action="/admin/login" class="space-y-3">
<label class="block text-sm" for="token">Admin token</label>
<input id="token" name="token" type="password" autocomplete="off" required
       class="w-full border border-gray-300 rounded-lg px-3 min-h-[44px]">
<button type="submit" class="w-full min-h-[44px] rounded-lg bg-blue-700 text-white font-medium">Sign in</button>
</form>
<p class="mt-6 text-sm"><a class="text-blue-700 underline" href="/auth/login?next=/admin">Sign in with an admin account</a> instead.</p>
</main></body></html>"""


def _secure(request: Request) -> bool:
    host = request.headers.get("host", "").split(":")[0].lower()
    return bool(host) and host not in ("localhost", "127.0.0.1")


@router.get("/login", response_class=HTMLResponse)
def admin_login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_PAGE.format(error="", name=escape(IDENTITY.name)))


@router.post("/login")
def admin_login(request: Request, token: str = Form(default="")):
    """Token sign-in by POST: the token stays out of URLs, logs, and history,
    and the cookie holds a signed random session id (plan step 4.5, C17)."""
    if not _token_matches(token):
        error = '<p role="alert" class="mb-3 text-sm text-red-700">That token is not right.</p>'
        return HTMLResponse(_LOGIN_PAGE.format(error=error, name=escape(IDENTITY.name)), status_code=403)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(ADMIN_SESSION_COOKIE, issue_admin_session(), httponly=True, samesite="lax",
                    secure=_secure(request), max_age=ADMIN_SESSION_DAYS * 86400)
    resp.delete_cookie("admin_token")        # the old raw-token cookie, if any
    return resp


@router.post("/logout")
def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# The dashboard: one builder per tab (plan step 5.4, finding C23: this was one
# 336-line function of ``if tab ==`` blocks). A builder returns the template
# keys its tab fills in; every other key keeps its empty value.
# ---------------------------------------------------------------------------

def _empty_tabs() -> dict:
    """Every key the template reads, empty (fresh lists per request)."""
    return {
        "today": None, "stats": [], "students": [], "attempts": [], "topics": [],
        "whitelist": [], "events": [], "restricted_students": [], "all_students": [],
        "supervision": None, "logins": None, "conversations": None, "timeline": None, "activity_rows": [],
        "calibration": None, "calibration_student": None, "health": None,
        "misconceptions": [], "classes_ctx": None,
    }


def _tab_today(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    return {"today": _today_panel(db)}


def _tab_students(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    # The all-time stats row sits on this tab only (plan step 4.1, finding
    # D22: it was on every tab, Health and Access included).
    avg_score_row = db.query(func.avg(ExerciseAttempt.score)).scalar()
    stats = [
        ("Students", db.query(func.count(Student.id)).scalar(), "border-blue-400"),
        ("Messages", db.query(func.count(Message.id)).scalar(), "border-purple-400"),
        ("Exercise Attempts", db.query(func.count(ExerciseAttempt.id)).scalar(), "border-green-400"),
        ("Avg Score (0-4)", round(avg_score_row, 2) if avg_score_row else 0, "border-yellow-400"),
    ]
    # One GROUP BY per aggregate, whatever the class size (plan step 4.2,
    # finding C16: this ran three queries per student and loaded every
    # exercise attempt, code submissions included, to count them).
    msg_counts: dict[int, int] = {
        sid: n for sid, n in
        db.query(Message.student_id, func.count(Message.id)).group_by(Message.student_id).all()
    }
    ex_stats = {
        sid: (n, avg)
        for sid, n, avg in db.query(
            ExerciseAttempt.student_id, func.count(ExerciseAttempt.id),
            func.avg(ExerciseAttempt.score),   # AVG skips unscored attempts
        ).group_by(ExerciseAttempt.student_id).all()
    }
    topic_counts: dict[int, int] = {
        sid: n for sid, n in
        db.query(TopicAccess.student_id, func.count(TopicAccess.id)).group_by(TopicAccess.student_id).all()
    }
    students = []
    for s in db.query(Student).order_by(Student.last_seen.desc()).all():
        ex_count, avg_s = ex_stats.get(s.id, (0, None))
        students.append(_Namespace(
            email=s.email, name=s.name,
            msg_count=msg_counts.get(s.id, 0),
            ex_count=ex_count,
            avg_score=round(float(avg_s), 1) if avg_s is not None else None,
            topic_count=topic_counts.get(s.id, 0),
            last_seen=s.last_seen,
            last_login=s.last_login,
            login_count=s.login_count or 0,
        ))
    return {"stats": stats, "students": students}


def _tab_classes(db: DBSession, student: Optional[str], class_id: Optional[int],
                 request: Optional[Request] = None) -> dict:
    note = request.query_params.get("note") if request is not None else None
    return {"classes_ctx": _classes_tab(db, class_id, note=note)}


def _tab_access(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    return {"whitelist": [
        _Namespace(email=w.email, added_by=w.added_by or "—", created_at=w.created_at)
        for w in list_whitelist(db)
    ]}


ATTEMPT_DAYS = 7      # the outcome counts above the attempt trail
ATTEMPT_ROWS = 200    # the trail's length


def _browser(user_agent: Optional[str]) -> str:
    """The user agent, short enough for a table cell; the cell's title has it all."""
    ua = (user_agent or "").strip()
    if not ua:
        return "—"
    return ua if len(ua) <= 60 else ua[:57] + "..."


def _tab_logins(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    """A per-student summary (most logins first), a recent-login feed, and the
    attempt trail: every step of every sign-in, failures included
    (tutor.auth.attempts), newest first, filtered by ``student`` as an
    address fragment. One sessions row per login is the audit trail of the
    logins that worked; the trail is where the ones that did not went."""
    from tutor import login_digest
    from tutor.auth import attempts as attempts_mod
    from tutor.utctime import to_local

    def when(ts, seconds=True):
        return to_local(ts).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M") if ts else "—"

    until = utcnow()
    since = until - datetime.timedelta(days=ATTEMPT_DAYS)
    problems = login_digest.summary(db, since, until)
    counts = sorted(
        db.query(LoginAttempt.outcome, func.count(LoginAttempt.id))
        .filter(LoginAttempt.created_at >= since)
        .group_by(LoginAttempt.outcome)
        .all()
    )
    trail = db.query(LoginAttempt).order_by(LoginAttempt.created_at.desc(), LoginAttempt.id.desc())
    needle = (student or "").strip().lower()
    if needle:
        trail = trail.filter(LoginAttempt.email.ilike(f"%{needle}%"))
    attempts = [
        _Namespace(created_at=a.created_at, when=when(a.created_at), email=a.email or "—", method=a.method,
                   outcome=a.outcome, tone=attempts_mod.tone(a.outcome), host=a.host or "—",
                   browser=_browser(a.user_agent), user_agent=a.user_agent or "",
                   detail=a.detail or "")
        for a in trail.limit(ATTEMPT_ROWS).all()
    ]
    summary = [
        _Namespace(email=s.email, name=s.name,
                   login_count=s.login_count or 0, last_login=s.last_login)
        for s in db.query(Student).order_by(Student.login_count.desc()).all()
    ]
    feed = [
        _Namespace(email=email, method=sess.method or "—", when=when(sess.created_at, seconds=False),
                   created_at=sess.created_at, revoked=sess.revoked)
        for sess, email in (
            db.query(SessionModel, Student.email)
            .join(Student, SessionModel.student_id == Student.id)
            .order_by(SessionModel.created_at.desc())
            .limit(200)
            .all()
        )
    ]
    return {"logins": _Namespace(summary=summary, feed=feed, attempts=attempts,
                                 attempt_counts=counts, attempt_filter=needle,
                                 attempt_days=ATTEMPT_DAYS, problems=problems)}


def _tab_topics(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    return {"topics": [
        _Namespace(student_email=email, lecture_num=ta.lecture_num,
                   topic_tag=ta.topic_tag, accessed_at=ta.accessed_at)
        for ta, email in (
            db.query(TopicAccess, Student.email)
            .join(Student, TopicAccess.student_id == Student.id)
            .order_by(TopicAccess.accessed_at.desc())
            .limit(200)
            .all()
        )
    ]}


def _tab_exercises(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    return {"attempts": [
        _Namespace(student_email=email, exercise_id=attempt.exercise_id,
                   score=attempt.score or 0, passed=attempt.passed,
                   feedback=attempt.feedback, created_at=attempt.created_at)
        for attempt, email in (
            db.query(ExerciseAttempt, Student.email)
            .join(Student, ExerciseAttempt.student_id == Student.id)
            .order_by(ExerciseAttempt.created_at.desc())
            .limit(200)
            .all()
        )
    ]}


def _tab_misconceptions(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    """The misconception library (pedagogy item 12): candidates await curation."""
    from tutor.db.models import GoalMisconception

    return {"misconceptions": [
        {
            "id": r.id, "goal_id": r.goal_id, "description": r.description,
            "distractor": r.distractor, "evidence": r.evidence_count,
            "status": r.status, "run_label": r.run_label,
        }
        for r in db.query(GoalMisconception)
        .order_by(GoalMisconception.status.asc(), GoalMisconception.goal_id.asc(),
                  GoalMisconception.evidence_count.desc())
        .all()
    ]}


CONVERSATIONS_COOKIE = "admin_convos_seen"
CONVERSATIONS_COOKIE_DAYS = 90


def _conversation_links(c: conv_mod.Conversation) -> dict[str, str]:
    q = urllib.parse.quote(c.email, safe="")
    thread = urllib.parse.urlencode({"student": c.email, "start": c.start_iso, "end": c.end_iso})
    return {
        "open": f"/admin?tab=conversations&student={q}#msg-{c.first_message_id}",
        "thread": f"/admin/conversations/thread?{thread}",
        "timeline": f"/admin?tab=timeline&student={q}",
        "supervision": f"/admin?tab=supervision&student={q}",
        "logins": f"/admin?tab=logins&student={q}",
    }


def _tab_conversations(db: DBSession, student: Optional[str], class_id: Optional[int],
                       request: Optional[Request] = None) -> dict:
    """With ``student``, that student's transcript (the last 300 messages, as
    always; the thumbs-down email deep-links here). Without one, the list of
    recent conversations: the window, sort and filters come from the query
    string, the "new since your last visit" baseline from a cookie the
    dashboard sets on the way out."""
    from tutor import classes as classes_mod

    all_students = [_Namespace(email=s.email) for s in db.query(Student).order_by(Student.email).all()]
    sel = find_student(db, student) if student else None
    params: Mapping[str, str] = request.query_params if request is not None else {}
    if sel:
        # ``msg``: the window around that message, whatever its age, the
        # target of a flag's link (review step 0.4, F18).
        around = bounded_int(params.get("msg")) or 0          # a hand-edited id is no id (R4)
        messages = (conv_mod.thread_around(db, sel.id, around) if around
                    else conv_mod.thread(db, sel.id, limit=300))
        view = _Namespace(selected_student=sel.email, messages=messages, listing=None)
        return {"all_students": all_students, "conversations": view}

    cookies: Mapping[str, str] = request.cookies if request is not None else {}
    days = conv_mod.clamp_days(params.get("days"))
    key, desc = conv_mod.sort_spec(params.get("sort"), params.get("dir"))
    flagged = params.get("flagged") == "1"
    course = params.get("course") if params.get("course") in COURSES else None
    now = utcnow()
    roster = classes_mod.roster_ids(db, class_id) if class_id else None
    listing = conv_mod.recent(db, now - datetime.timedelta(days=days), now=now, student_ids=roster,
                              course=course, flagged_only=flagged)
    seen = conv_mod.seen_state(cookies.get(CONVERSATIONS_COOKIE), now)
    new_count = conv_mod.mark_new(listing.rows, seen.baseline)
    rows = conv_mod.sort_rows(listing.rows, key, desc)
    for c in rows:
        c.links = _conversation_links(c)
    base: dict[str, object] = {"tab": "conversations", "days": days, "flagged": "1" if flagged else "",
                               "course": course or "", "class_id": class_id or ""}
    view = _Namespace(
        selected_student=None, messages=[], listing=listing, rows=rows, days=days,
        day_choices=conv_mod.DAY_CHOICES, flagged=flagged, course=course, courses=COURSES,
        class_id=class_id, students=all_students, student=None, new_count=new_count,
        classes=[_Namespace(id=k.id, label=k.label) for k in classes_mod.all_classes(db, include_archived=False)],
        show_cost=True, show_admin_links=True, form_action="/admin", hidden=[("tab", "conversations")],
        sort_links=conv_mod.sort_links("/admin", base, key, desc),
    )
    out: dict = {"all_students": all_students, "conversations": view}
    if seen.cookie:
        out["set_cookie"] = dict(key=CONVERSATIONS_COOKIE, value=seen.cookie,
                                 max_age=CONVERSATIONS_COOKIE_DAYS * 86400, httponly=True,
                                 samesite="lax", secure=request is not None and _secure_cookies(request), path="/admin")
    return out


def _tab_supervision(db: DBSession, student: Optional[str], class_id: Optional[int],
                     kind: Optional[str] = None) -> dict:
    from tutor.agent import usage as usage_mod
    from tutor.agent.envelope import EXPLAIN_MODE_SETTING
    from tutor.db.models import JudgeSample

    q = db.query(TurnEvent).order_by(TurnEvent.ts.desc())
    counts = db.query(TurnEvent.kind, func.count(TurnEvent.id))
    selected_student = None
    sel = find_student(db, student) if student else None
    if sel:
        selected_student = sel.email
        q = q.filter(TurnEvent.student_id == sel.id)
        counts = counts.filter(TurnEvent.student_id == sel.id)
    # Three rows in four are one supervisor model call, not a turn, and carry
    # no intent, ceiling or verdict; the table opens on the turns so a reader
    # (or a demo audience) is not looking at six empty columns.
    kind_counts = [(str(k), int(n)) for k, n in counts.group_by(TurnEvent.kind).all()]
    selected_kind = supervision_view.normalize_kind(kind, [k for k, _n in kind_counts])
    if selected_kind != supervision_view.ALL:
        q = q.filter(TurnEvent.kind == selected_kind)
    recent_events = q.limit(200).all()

    # Auditor disagreement queue: JudgeSamples written by the nightly auditor
    # whose verdict differs from the live verdict on the same turn (Session 22).
    auditor_samples = (
        db.query(JudgeSample, TurnEvent)
        .join(TurnEvent, JudgeSample.turn_event_id == TurnEvent.id)
        .filter(JudgeSample.judge.like("auditor:%"))
        .order_by(JudgeSample.id.desc())
        .limit(50)
        .all()
    )
    # Emails for just the students on this page (plan step 4.2, finding
    # C16: every Student row was loaded to build this map).
    shown_ids = {te.student_id for te in recent_events} | {te.student_id for _s, te in auditor_samples}
    emails: dict[int, str] = {
        sid: email for sid, email in
        db.query(Student.id, Student.email).filter(Student.id.in_(shown_ids)).all()
    } if shown_ids else {}
    disagreements = []
    for sample, te in auditor_samples:
        live = (_parse_json(te.verdicts_json) or {}).get("verdict") or "allow"
        if sample.verdict != live:
            disagreements.append(_Namespace(
                turn_event_id=te.id,
                student=emails.get(te.student_id, "?"),
                live=live,
                audit=sample.verdict,
                reason=sample.reason,
                ts=te.ts,
            ))

    explain_row = db.get(Setting, EXPLAIN_MODE_SETTING)
    caps = _Namespace(
        daily_exchange_limit=usage_mod._int_env("DAILY_EXCHANGE_LIMIT", 0),
        monthly_cost_cap_usd=usage_mod._float_env("MONTHLY_COST_CAP_USD", 0.0),
        month_cost_usd=round(usage_mod.month_cost_usd(db), 4),
    )
    # Per-student dollar quotas (tutor/quotas.py): the settings, who is near
    # or over one, and the hits of the last week.
    from tutor import quotas as quotas_mod

    quota_panel = _Namespace(
        values=quotas_mod.load_quotas(db),
        notify=quotas_mod.load_notify(db),
        defaults=quotas_mod.DEFAULTS,
        spend=quotas_mod.spend_table(db),
        hits=quotas_mod.hits(db, utcnow() - datetime.timedelta(days=7))[:50],
    )
    return {
        "events": [_event_row(te, emails.get(te.student_id, "?")) for te in recent_events],
        "restricted_students": [
            _Namespace(email=s.email, name=s.name)
            for s in db.query(Student).filter_by(restricted=True).order_by(Student.email).all()
        ],
        "all_students": [
            _Namespace(email=email, restricted=restricted)
            for email, restricted in
            db.query(Student.email, Student.restricted).order_by(Student.email).all()
        ],
        "supervision": _Namespace(
            explain_value=explain_row.value if explain_row else "",
            selected_student=selected_student,
            selected_kind=selected_kind,
            kind_choices=supervision_view.kind_choices(kind_counts, selected_kind),
            all_kinds=supervision_view.ALL,
            caps=caps,
            quotas=quota_panel,
            disagreements=disagreements,
        ),
    }


def _tab_calibration(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    """Stated confidence against the actual outcome (Engram P2): the cohort by
    default, one student with the email filter. Observational: never shown
    to the student."""
    q = db.query(TurnEvent).filter(TurnEvent.confidence.isnot(None))
    calibration_student = None
    sel = find_student(db, student) if student else None
    if sel:
        calibration_student = sel.email
        q = q.filter(TurnEvent.student_id == sel.id)
    pairs: list[tuple[int, bool]] = []
    for te in q.all():
        correct = _outcome_correct(te.kind, _parse_json(te.verdicts_json) or {})
        if correct is not None and te.confidence is not None:
            pairs.append((int(te.confidence), correct))
    return {"calibration": _calibration_report(pairs), "calibration_student": calibration_student}


def _tab_activity(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    """Counts per day over the last 14 days. Bucketed in Python so the same
    code works on SQLite and Postgres (no dialect-specific date_trunc); fine
    at pilot scale."""
    since = utcnow() - datetime.timedelta(days=14)
    by_day: dict = collections.defaultdict(
        lambda: {"students": set(), "messages": 0, "turns": 0, "cost": 0.0, "logins": 0}
    )
    for sid, ts in db.query(Message.student_id, Message.created_at).filter(Message.created_at >= since).all():
        by_day[ts.date()]["messages"] += 1
        by_day[ts.date()]["students"].add(sid)
    for ts, kind, cost in (
        db.query(TurnEvent.ts, TurnEvent.kind, TurnEvent.cost_usd).filter(TurnEvent.ts >= since).all()
    ):
        by_day[ts.date()]["cost"] += (cost or 0.0)
        if kind == "chat":
            by_day[ts.date()]["turns"] += 1
    for (ts,) in db.query(SessionModel.created_at).filter(SessionModel.created_at >= since).all():
        by_day[ts.date()]["logins"] += 1
    return {"activity_rows": [
        _Namespace(date=d.isoformat(), active=len(v["students"]),
                   messages=v["messages"], turns=v["turns"],
                   logins=v["logins"], cost=round(v["cost"], 4))
        for d, v in sorted(by_day.items(), reverse=True)
    ]}


def _tab_timeline(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    """One student's last two days, signal by signal (analytics plan,
    step 4.2): the pages, clicks, questions, slides and narration in words,
    grouped into visits. The "what happened to this student" question."""
    from tutor import engagement as engagement_mod

    sel = find_student(db, student) if student else None
    rows = engagement_mod.recent_timeline(db, sel.id, hours=TIMELINE_HOURS) if sel else []
    return {
        "all_students": [_Namespace(email=s.email) for s in db.query(Student).order_by(Student.email).all()],
        "timeline": _Namespace(selected_student=sel.email if sel else None, rows=rows, hours=TIMELINE_HOURS),
    }


def _tab_health(db: DBSession, student: Optional[str], class_id: Optional[int]) -> dict:
    from tutor.health import collect_diagnostics

    return {"health": collect_diagnostics(db)}


_TAB_BUILDERS = {
    "today": _tab_today, "students": _tab_students, "classes": _tab_classes,
    "access": _tab_access, "logins": _tab_logins, "topics": _tab_topics,
    "exercises": _tab_exercises, "misconceptions": _tab_misconceptions,
    "conversations": _tab_conversations, "supervision": _tab_supervision,
    "calibration": _tab_calibration, "activity": _tab_activity, "health": _tab_health,
    "timeline": _tab_timeline,
}
TIMELINE_HOURS = 48


# The query parameters a page link carries through the login round trip
# (review step 0.4, F17): the tab and what it is opened on. Never a token
# (tests/test_security_edges.py: a token in the URL is not honoured).
_CARRIED_PARAMS = ("tab", "student", "msg", "class_id", "days", "course", "sort", "dir", "flagged",
                   "kind")


def _admin_destination(request: Request) -> str:
    kept = [(k, v) for k, v in request.query_params.multi_items() if k in _CARRIED_PARAMS]
    return request.url.path + (f"?{urllib.parse.urlencode(kept)}" if kept else "")


def _send_non_admin_on(db: DBSession, session_token: Optional[str],
                       next_path: str = "/admin") -> RedirectResponse:
    """Where someone who is not an admin goes: a signed-in instructor to
    their dashboard, a signed-in student to the chat, anyone else to the
    login page with a way back here.

    Until 2026-10-02 this was a 403 page reading "Access denied. Sign in
    with an admin account", whose one link signed an instructor in and
    brought them straight back to it; they reported that neither Google nor
    the magic link would log them in. Still nothing here names the token
    mechanism (finding D30).
    """
    student = session_student(db, session_token)
    if student is not None:
        target = "/instructor" if is_instructor(db, student.email) else "/chat"
        return RedirectResponse(target, status_code=303)
    # The whole destination survives the login round trip (review step 0.4,
    # F17: the daily email's deep link used to land on Today).
    return RedirectResponse(f"/auth/login?next={urllib.parse.quote(next_path, safe='/')}", status_code=303)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    tab: str = Query(default="today"),
    student: Optional[str] = Query(default=None),
    class_id: Optional[int] = Depends(optional_int("class_id")),
    kind: Optional[str] = Query(default=None, max_length=32),
    token: Optional[str] = Query(default=None),
    admin_session: Optional[str] = Cookie(alias=ADMIN_SESSION_COOKIE, default=None),
    session_token: Optional[str] = Cookie(default=None),
    db: DBSession = Depends(get_db),
):
    # A token in the URL is no longer honoured: it lands in logs, history, and
    # Referer headers. Send the reader to the sign-in form, without it.
    if token is not None:
        return RedirectResponse("/admin/login", status_code=303)

    acting_admin = _admin_email_from_session(session_token, db)
    if not acting_admin and _admin_session_ok(admin_session):
        acting_admin = "token"
    if not acting_admin:
        return _send_non_admin_on(db, session_token, _admin_destination(request))

    context = _empty_tabs()
    builder = _TAB_BUILDERS.get(tab)
    if builder is not None:
        extra: dict = {"request": request} if "request" in builder.__annotations__ else {}
        if "kind" in builder.__annotations__:
            extra["kind"] = kind
        context.update(builder(db, student, class_id, **extra))
    cookie = context.pop("set_cookie", None)        # the Conversations tab's "seen" baseline
    context.update(
        tab=tab,
        tab_groups=TAB_GROUPS,
        whitelist_enabled=whitelist_enabled(db),
        admin_emails=sorted(get_config()["admin_emails"]),
        acting_admin=acting_admin,
    )
    response = templates.TemplateResponse(request, "admin.html", context)
    if cookie:
        response.set_cookie(**cookie)
    return response


# ---------------------------------------------------------------------------
# Classes tab: the instructor roster
#
# Two jobs in one tab because they are one workflow: promote a student to
# instructor, then create the class they teach and put students on it.
# ---------------------------------------------------------------------------

def _classes_tab(db: DBSession, class_id: Optional[int], note: Optional[str] = None) -> "_Namespace":
    """Data for the Classes tab: instructors, classes, and one open roster
    with its instructors (``note`` is the answer to the last add or remove)."""
    from tutor import classes as classes_mod

    rows = classes_mod.all_classes(db)
    counts = classes_mod.enrollment_counts(db, [c.id for c in rows])
    # Every class's instructors, the lead first: one list per class.
    teaching: dict[int, list[int]] = {c.id: classes_mod.instructor_ids(db, c.id) for c in rows}
    all_ids = {i for ids in teaching.values() for i in ids}
    instructor_names = {
        s.id: s.email for s in db.query(Student).filter(Student.id.in_(all_ids or [0])).all()
    }
    class_rows = [
        _Namespace(
            id=c.id, name=c.name, label=c.label, course=c.course, term=c.term,
            instructor=", ".join(instructor_names.get(i, "?") for i in teaching[c.id]),
            students=counts.get(c.id, 0), archived=c.archived,
            weekly_report=c.weekly_report, created_at=c.created_at,
            quarter_start=c.quarter_start, quarter_end=c.quarter_end,
        )
        for c in rows
    ]

    instructors = [
        _Namespace(
            email=s.email, name=s.name,
            classes=sum(1 for ids in teaching.values() if s.id in ids),
            last_login=s.last_login,
        )
        for s in list_instructors(db)
    ]

    # The open class's roster and instructors, when one is selected.
    selected = None
    if class_id is not None:
        c = classes_mod.get_class(db, class_id)
        if c is not None:
            selected = _Namespace(
                id=c.id, name=c.name, label=c.label, course=c.course, term=c.term,
                instructor=", ".join(instructor_names.get(i, "?") for i in teaching[c.id]),
                instructors=[
                    _Namespace(email=s.email, name=s.name, lead=(s.id == c.instructor_id))
                    for s in classes_mod.class_instructors(db, c.id)
                ],
                archived=c.archived, weekly_report=c.weekly_report,
                roster=[
                    _Namespace(email=s.email, name=s.name, last_login=s.last_login,
                               login_count=s.login_count or 0)
                    for s in classes_mod.class_roster(db, c.id)
                ],
            )

    return _Namespace(
        classes=class_rows,
        instructors=instructors,
        selected=selected,
        note=classes_mod.NOTES.get(note or ""),
        courses=classes_mod.COURSES,
        all_emails=[s.email for s in db.query(Student).order_by(Student.email).all()],
    )


def _classes_redirect(class_id: Optional[int] = None, note: Optional[str] = None) -> RedirectResponse:
    url = "/admin?tab=classes" + (f"&class_id={class_id}" if class_id else "")
    if note:
        url += f"&note={note}"
    return RedirectResponse(url, status_code=303)


@router.post("/instructors/set")
def instructors_set(
    email: str = Form(...),
    enabled: str = Form(default="off"),
    db: DBSession = Depends(get_db),
    acting: str = Depends(require_admin),
):
    """Promote a student to instructor, or demote them.

    Demoting leaves their classes in place (see classes.set_instructor's note);
    reassign or archive those separately.
    """
    ok = set_instructor(db, email, enabled == "on")
    from tutor.logging_config import activity
    found = find_student(db, email)
    target = found.id if found is not None else None
    activity("instructor_role_set", target_student_id=target,
             enabled=(enabled == "on"), ok=ok, by=acting)
    return _classes_redirect()


@router.post("/classes/create")
def classes_create(
    name: str = Form(...),
    course: str = Form(...),
    instructor_email: str = Form(...),
    term: str = Form(default=""),
    quarter_start: str = Form(default=""),
    quarter_end: str = Form(default=""),
    db: DBSession = Depends(get_db),
    acting: str = Depends(require_admin),
):
    from tutor import classes as classes_mod

    row = classes_mod.create_class(
        db, name=name, course=course, instructor_email=instructor_email,
        term=term, created_by=acting,
        quarter_start=classes_mod.parse_day(quarter_start),
        quarter_end=classes_mod.parse_day(quarter_end),
    )
    return _classes_redirect(row.id if row else None)


@router.post("/classes/update")
def classes_update(
    class_id: int = Form(...),
    name: Optional[str] = Form(default=None),
    term: Optional[str] = Form(default=None),
    instructor_email: Optional[str] = Form(default=None),
    archived: Optional[str] = Form(default=None),
    weekly_report: Optional[str] = Form(default=None),
    quarter_start: Optional[str] = Form(default=None),
    quarter_end: Optional[str] = Form(default=None),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Edit one class. Checkbox fields are tri-state on purpose: a field absent
    from the POST is left alone, so the small inline forms (archive toggle,
    weekly-email toggle, quarter dates) do not clear the fields they do not carry."""
    from tutor import classes as classes_mod

    classes_mod.update_class(
        db, class_id,
        name=name,
        term=term,
        instructor_email=instructor_email or None,
        archived=None if archived is None else (archived == "on"),
        weekly_report=None if weekly_report is None else (weekly_report == "on"),
        quarter_start=classes_mod.parse_day(quarter_start),
        quarter_end=classes_mod.parse_day(quarter_end),
    )
    return _classes_redirect(class_id)


@router.post("/classes/delete")
def classes_delete(
    class_id: int = Form(...),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    from tutor import classes as classes_mod

    classes_mod.delete_class(db, class_id)
    return _classes_redirect()


@router.post("/classes/enroll")
def classes_enroll(
    class_id: int = Form(...),
    emails: str = Form(...),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Add students by pasted email list (comma / space / newline separated)."""
    from tutor import classes as classes_mod

    classes_mod.enroll_many(db, class_id, emails)
    return _classes_redirect(class_id)


@router.post("/classes/unenroll")
def classes_unenroll(
    class_id: int = Form(...),
    email: str = Form(...),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    from tutor import classes as classes_mod

    classes_mod.unenroll(db, class_id, email)
    return _classes_redirect(class_id)


@router.post("/classes/instructors/add")
def classes_add_instructor(
    class_id: int = Form(...),
    email: str = Form(...),
    db: DBSession = Depends(get_db),
    acting: str = Depends(require_admin),
):
    """Another instructor for a class (classes.add_instructor grants the role;
    the whitelist stays the admin's separate decision, and the note says so
    when it would keep them out)."""
    from tutor import classes as classes_mod

    note = classes_mod.add_instructor(db, class_id, email, added_by=acting)
    return _classes_redirect(class_id, note)


@router.post("/classes/instructors/remove")
def classes_remove_instructor(
    class_id: int = Form(...),
    email: str = Form(...),
    db: DBSession = Depends(get_db),
    acting: str = Depends(require_admin),
):
    """Take an instructor off a class: never the acting admin themselves,
    never the last one (classes.remove_instructor)."""
    from tutor import classes as classes_mod

    note = classes_mod.remove_instructor(db, class_id, email, actor_email=acting)
    return _classes_redirect(class_id, note)


# ---------------------------------------------------------------------------
# Conversation export (download a student's full transcript)
#
# Rendering lives in tutor/transcripts.py so this and the instructor's
# per-student download emit identical files.
# ---------------------------------------------------------------------------

@router.get("/conversations/thread", response_class=HTMLResponse)
def conversations_thread(
    request: Request,
    student: str = Query(..., description="student email"),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    db: DBSession = Depends(get_db),
    _=Depends(require_admin),
):
    """One conversation's messages as an HTML fragment, inserted under its
    row by conversations.js.
    ``start`` and ``end`` are the row's span, naive UTC ISO; unknown
    student or an unparsable time renders the empty fragment."""
    sel = find_student(db, student)
    since, until = conv_mod.parse_ts(start), conv_mod.parse_ts(end)
    messages = conv_mod.thread(db, sel.id, since, until) if sel and since and until else []
    return templates.TemplateResponse(request, "_conversation_thread.html", {"messages": messages})


@router.get("/conversations/export")
def conversations_export(
    student: str = Query(..., description="student email"),
    format: str = Query(default="md", pattern="^(md|json)$"),
    course: Optional[str] = Query(default=None, max_length=8),
    db: DBSession = Depends(get_db),
    _=Depends(require_admin),
):
    """Download a student's complete tutor transcript as Markdown or JSON.

    Unlike the Conversations tab (last 300 messages, for reading in the
    browser), this returns every stored message, oldest first, with the
    per-message tool notes included. Optional ``course`` narrows to one course.
    """
    if course is not None and course not in COURSES:
        raise HTTPException(status_code=422, detail="Unknown course")
    sel = find_student(db, student)
    if sel is None:
        raise HTTPException(status_code=404, detail="Unknown student")

    q = db.query(Message).filter_by(student_id=sel.id)
    if course:
        q = q.filter(Message.course == course)
    rows = q.order_by(Message.created_at.asc(), Message.id.asc()).all()
    feedback = {
        f.message_id: f for f in
        db.query(MessageFeedback).filter_by(student_id=sel.id).all()
    }

    if format == "json":
        body = transcripts.render_json(sel, rows, course, feedback)
        media, ext = "application/json", "json"
    else:
        body = transcripts.render_markdown(sel, rows, course, feedback)
        media, ext = "text/markdown", "md"

    return Response(
        content=body,
        media_type=f"{media}; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{transcripts.export_filename(sel.email, ext)}"'
        },
    )


@router.post("/feedback/{feedback_id}/seen")
def feedback_seen(
    feedback_id: int,
    seen: str = Form("1"),
    db: DBSession = Depends(get_db),
    _=Depends(require_admin),
):
    """The seen mark on a flag (review step 0.4): Today lists unseen flags
    first; a new rating from the student clears the mark."""
    from tutor.agent import state

    if not bounded_int(feedback_id) or state.mark_feedback_seen(db, feedback_id, seen=seen != "0") is None:
        raise HTTPException(status_code=404, detail="No such feedback")
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.get("/export/decks")
def decks_export(_=Depends(require_admin)) -> dict:
    """Which lecture decks this instance serves, and which lectures of the
    plan have none: ``{"AST101": [...], "AST102": [...], "missing": {...}}``, for
    the weekly crawl (scripts/link_check.py; review step 0.3). A deck
    missing from the seed used to be found by a student."""
    from tutor import decks

    have = decks.available_decks()
    return {**have, "missing": {course: decks.missing_lectures(course) for course in have}}


@router.get("/export/analytics/{table}")
def analytics_export(
    table: str,
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=5000, ge=1, le=20000),
    since: Optional[str] = Query(default=None),
    class_id: Optional[int] = Query(default=None),
    with_emails: bool = Query(default=False),
    with_content: bool = Query(default=False),
    db: DBSession = Depends(get_db),
    _=Depends(require_admin),
):
    """One page of a table for ``python -m tutor.analytics export --from-url``
    (step 2.3): JSON lines, the same rows the direct export reads, so a
    machine without database credentials can build the local store with the
    admin token. Incremental tables page by ``since_id``; whole tables come
    entire.
    """
    import datetime as _dt

    from tutor.analytics import store

    if table not in store.TABLE_NAMES:
        raise HTTPException(status_code=404, detail="Unknown table")
    since_dt = None
    if since:
        try:
            since_dt = _dt.datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(status_code=422, detail="since wants an ISO date") from None
    rows = store.read_rows(db, table, since_id=since_id, limit=limit, since=since_dt, class_id=class_id,
                           with_emails=with_emails, with_content=with_content)
    body = "".join(json.dumps(store.jsonable(r), default=str) + "\n" for r in rows)
    return Response(content=body, media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# JSON API (for curl / scripts)
# ---------------------------------------------------------------------------

@router.get("/api/students")
def api_students(
    db: DBSession = Depends(get_db),
    _=Depends(require_admin),
):
    students = db.query(Student).all()
    return [
        {"id": s.id, "email": s.email, "name": s.name, "last_seen": s.last_seen}
        for s in students
    ]


@router.get("/api/exercises")
def api_exercises(
    db: DBSession = Depends(get_db),
    _=Depends(require_admin),
):
    rows = db.query(ExerciseAttempt).order_by(ExerciseAttempt.created_at.desc()).limit(100).all()
    return [
        {
            "id": r.id, "student_id": r.student_id,
            "exercise_id": r.exercise_id, "score": r.score,
            "passed": r.passed, "created_at": r.created_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Whitelist management (form POSTs from the Access tab)
# ---------------------------------------------------------------------------

@router.post("/whitelist/add")
def whitelist_add(
    email: str = Form(...),
    db: DBSession = Depends(get_db),
    acting: str = Depends(require_admin),
):
    add_to_whitelist(db, email, added_by=acting)
    return RedirectResponse("/admin?tab=access", status_code=303)


@router.post("/whitelist/remove")
def whitelist_remove(
    email: str = Form(...),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    remove_from_whitelist(db, email)
    return RedirectResponse("/admin?tab=access", status_code=303)


@router.post("/whitelist/toggle")
def whitelist_toggle(
    enabled: str = Form(default="off"),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    set_whitelist_enabled(db, enabled == "on")
    return RedirectResponse("/admin?tab=access", status_code=303)


# ---------------------------------------------------------------------------
# Supervision controls (Session 21 / Task 7): conduct restriction + explain-mode
# ---------------------------------------------------------------------------

@router.post("/quotas")
def quotas_set(
    day: str = Form(default=""),
    week: str = Form(default=""),
    month: str = Form(default=""),
    notify_daily: str = Form(default="off"),
    notify_weekly: str = Form(default="off"),
    notify_monthly: str = Form(default="off"),
    db: DBSession = Depends(get_db),
    acting: str = Depends(require_admin),
):
    """Save the per-student dollar quotas and the roll-up emails
    (tutor/quotas.py). A blank or 0 turns a quota off; anything that is not a
    non-negative number is refused with a 422."""
    from tutor import quotas as quotas_mod

    values: dict[str, float] = {}
    for window, raw in (("day", day), ("week", week), ("month", month)):
        text = raw.strip().lstrip("$")
        try:
            value = float(text) if text else 0.0
        except ValueError:
            raise HTTPException(status_code=422, detail=f"The {window} quota wants a dollar amount, not {raw!r}.") from None
        if value < 0 or value != value or value > 1_000_000:
            raise HTTPException(status_code=422, detail=f"The {window} quota wants a dollar amount from 0 up.")
        values[window] = value
    quotas_mod.save_quotas(db, values)
    quotas_mod.save_notify(db, {"daily": notify_daily == "on", "weekly": notify_weekly == "on",
                                "monthly": notify_monthly == "on"})
    db.commit()
    logging.getLogger("tutor.admin").info("quotas set by %s: %s; roll-ups %s", acting,
                                          quotas_mod.describe(values),
                                          ",".join(p for p, on in quotas_mod.load_notify(db).items() if on) or "none")
    return RedirectResponse("/admin?tab=supervision", status_code=303)


@router.post("/restriction/set")
def restriction_set(
    email: str = Form(...),
    restricted: str = Form(default="off"),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Set or clear a student's conduct restriction (the H1 cap). Goes through
    state.set_student_restricted (single writer; records a TurnEvent)."""
    from tutor.agent import state
    s = find_student(db, email)
    if s is not None:
        state.set_student_restricted(db, s.id, restricted=(restricted == "on"))
        db.commit()
    return RedirectResponse("/admin?tab=supervision", status_code=303)


@router.post("/settings/explain-mode")
def settings_explain_mode(
    value: str = Form(default=""),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Set the explain-mode setting: "" (off), a course id, or "all". The
    envelope assembler reads this per course (envelope._explain_mode_on)."""
    from tutor.agent.envelope import EXPLAIN_MODE_SETTING

    value = value.strip()
    if value.lower() == "all":
        value = "all"
    elif value not in COURSES:
        value = ""
    db.merge(Setting(key=EXPLAIN_MODE_SETTING, value=value))
    db.commit()
    return RedirectResponse("/admin?tab=supervision", status_code=303)


# ---------------------------------------------------------------------------
# Simple namespace helper (avoids dataclass overhead)
# ---------------------------------------------------------------------------

class _Namespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@router.post("/misconceptions/set")
def misconceptions_set(
    row_id: int = Form(...),
    status: str = Form(...),
    db: DBSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Curate the cohort misconception library (pedagogy item 12): only approved
    rows reach prompts and review probes. Instructor decisions are sticky
    across re-mining."""
    from tutor.agent.misconception_mining import set_status

    if status not in ("approved", "rejected", "candidate"):
        return JSONResponse({"error": "bad status"}, status_code=400)
    set_status(db, row_id, status)
    db.commit()
    return RedirectResponse("/admin?tab=misconceptions", status_code=303)
