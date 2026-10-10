"""Instructor dashboard: how one instructor's students are using the tutor.

Scoped by class membership at every step:

    require_instructor      the viewer holds the role (or is an admin)
    _class_or_403           the class is one the viewer owns
    _student_or_403         the student is on a roster the viewer owns

All three fail closed, so an instructor cannot reach another instructor's
class, or a student who is enrolled nowhere, by guessing an id or an email.
The numbers come from tutor/class_metrics.py (the same computation the weekly
email sends) and transcripts from tutor/transcripts.py (the same renderer the
admin export uses).
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import types
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from tutor.templating import templates
from sqlalchemy.orm import Session as DBSession

from tutor import class_metrics, classes as classes_mod, conversations as conv_mod
from tutor import engagement as engagement_mod, student_days, transcripts
from tutor.auth.access import find_student, is_instructor, set_weekly_email
from tutor.auth.deps import get_current_student, get_db
from tutor.nav import nav_context
from tutor.routers.auth import _secure_cookies
from tutor.routers.params import int_query, optional_int
from tutor.db.models import CourseClass, Message, MessageFeedback, Student, TurnEvent
from tutor.utctime import utcnow

router = APIRouter(tags=["instructor"])

DEFAULT_DAYS = 7
MAX_DAYS = 180
CONVERSATIONS_COOKIE = "instructor_convos_seen"
CONVERSATIONS_COOKIE_DAYS = 90
TRANSCRIPT_PAGE_MESSAGES = 300
# Messages shown inline on the student drill-down. The full transcript is a
# download; the page is for a glance, not for reading a quarter of chat.
DRILLDOWN_MESSAGES = 60


def require_instructor(
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
) -> Student:
    """Dependency: the logged-in viewer holds the instructor role, or is an admin."""
    if not is_instructor(db, student.email):
        raise HTTPException(status_code=403, detail="Not an instructor")
    return student


def _window(days: int) -> tuple[datetime.datetime, datetime.datetime, int]:
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    until = utcnow()
    return until - datetime.timedelta(days=days), until, days


def _class_or_403(db: DBSession, viewer: Student, class_id: int) -> CourseClass:
    from tutor.routers.params import bounded_int

    # An id past 2^31 is "not your class", not a 500 on Postgres (R4).
    row = classes_mod.can_view_class(db, viewer, class_id) if bounded_int(class_id) else None
    if row is None:
        # 403 for both "not yours" and "no such class": an instructor should
        # not be able to probe which class ids exist.
        raise HTTPException(status_code=403, detail="Not your class")
    return row


def _student_or_403(db: DBSession, viewer: Student, email: str) -> Student:
    target = find_student(db, email)
    if target is None or not classes_mod.can_view_student(db, viewer, target.id):
        raise HTTPException(status_code=403, detail="Not your student")
    return target


def _pick_class(db: DBSession, viewer: Student, class_id: Optional[int]) -> Optional[CourseClass]:
    """The class to show: the requested one (checked), else the first visible."""
    if class_id is not None:
        return _class_or_403(db, viewer, class_id)
    visible = classes_mod.visible_classes(db, viewer)
    return visible[0] if visible else None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def grading_sources(db: DBSession, student_ids: list[int], since: datetime.datetime,
                    until: datetime.datetime) -> dict[str, int]:
    """How the window's exercises were graded (plan step 4.3, finding B26):
    ``{"tests": n, "rubric": n}``. Tests are facts from running the code; the
    rubric is a model's opinion, which alone never masters a goal. Most
    exercises have no tests yet, so instructors should know which is which."""
    counts = {"tests": 0, "rubric": 0}
    if not student_ids:
        return counts
    rows = (
        db.query(TurnEvent.verdicts_json)
        .filter(TurnEvent.kind == "exercise", TurnEvent.student_id.in_(student_ids),
                TurnEvent.ts >= since, TurnEvent.ts < until)
        .all()
    )
    for (raw,) in rows:
        try:
            source = (json.loads(raw or "{}") or {}).get("eval_source")
        except ValueError:
            source = None
        if source == "test":
            counts["tests"] += 1
        elif source and source != "unavailable":
            counts["rubric"] += 1
    return counts


def _flagged(db: DBSession, course_class: CourseClass, student_ids: set[int],
             since: datetime.datetime, until: datetime.datetime, days: int) -> list[dict]:
    """The class's thumbs-down replies in the window, newest first, each
    linked to the student's drill-down at that message (plan step 4.3,
    finding D30: the count had no link)."""
    from urllib.parse import quote

    from tutor import feedback_report

    by_email = {s.email: s.id for s in db.query(Student).filter(Student.id.in_(student_ids or [0]))}
    out = []
    for f in reversed(feedback_report.thumbs_down_in_window(db, since, until)):
        if f.student_email not in by_email or (f.course and f.course != course_class.course):
            continue
        out.append({
            "flag": f,
            # The transcript view around the reply, highlighted (review step
            # 0.4): the drill-down held only the newest 60 messages.
            "link": (f"/instructor/transcript/view?class_id={course_class.id}"
                     f"&student={quote(f.student_email, safe='')}&msg={f.message_id}#msg-{f.message_id}"),
        })
    return out


def _instructors_ctx(db: DBSession, viewer: Student, current: Optional[CourseClass]) -> list[dict]:
    """The class's instructors for the page: lead first, with who is the viewer
    (the one entry without a remove button)."""
    if current is None:
        return []
    return [
        {"email": s.email, "name": s.name, "lead": s.id == current.instructor_id,
         "is_self": s.id == viewer.id}
        for s in classes_mod.class_instructors(db, current.id)
    ]


@router.get("/instructor", response_class=HTMLResponse)
def instructor_dashboard(
    request: Request,
    class_id: Optional[int] = Depends(optional_int("class_id")),
    days: int = Depends(int_query("days", DEFAULT_DAYS)),
    student: Optional[str] = Query(default=None),
    view: Optional[str] = Query(default=None),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """Class picker, class summary, roster, and an optional student drill-down;
    ``view=all`` shows one line per open class instead."""
    since, until, days = _window(days)
    my_classes = classes_mod.visible_classes(db, viewer, include_archived=True)

    # All my classes (plan step 4.3): one summary line each, linked.
    if view == "all":
        overview = []
        for c in [c for c in my_classes if not c.archived]:
            roster = classes_mod.class_roster(db, c.id)
            c_summary, _rows = class_metrics.class_report(db, c, roster, since, until)
            overview.append(c_summary)
        return templates.TemplateResponse(request, "instructor.html", {
            **nav_context(viewer, db),
            "viewer": viewer,
            "classes": [
                {"id": c.id, "label": c.label, "course": c.course, "archived": c.archived}
                for c in my_classes
            ],
            "current": None, "summary": None, "rows": [], "detail": None,
            "days": days, "overview": overview, "lecture_titles": {}, "conversations": None,
            "instructors": [], "note": None,
        })

    current = _pick_class(db, viewer, class_id)

    if current is not None and view == "conversations":
        # The class's recent conversations instead of the roster
        # (section 2.7).
        convos, cookie = _conversations_view(db, request, viewer, current, days, student)
        response = templates.TemplateResponse(request, "instructor.html", {
            **nav_context(viewer, db),
            "viewer": viewer,
            "classes": [
                {"id": c.id, "label": c.label, "course": c.course, "archived": c.archived}
                for c in my_classes
            ],
            "current": current, "summary": None, "rows": [], "days": days, "detail": None,
            "flagged": [], "sources": {"tests": 0, "rubric": 0}, "standings": [],
            "activity": engagement_mod.ClassActivity(), "thresholds": engagement_mod.live_thresholds(),
            "overview": None, "lecture_titles": {}, "conversations": convos,
            "instructors": [], "note": None,
        })
        if cookie:
            response.set_cookie(**cookie)
        return response

    summary = None
    rows: list = []
    detail = None
    flagged: list = []
    sources = {"tests": 0, "rubric": 0}
    activity = engagement_mod.ClassActivity()
    standings: list = []
    if current is not None:
        roster = classes_mod.class_roster(db, current.id)
        summary, rows = class_metrics.class_report(db, current, roster, since, until)
        roster_ids = [s.id for s in roster]
        flagged = _flagged(db, current, set(roster_ids), since, until, days)
        sources = grading_sources(db, roster_ids, since, until)
        # How the roster used the site, from the browser's activity events
        # (phase 4): time on screen, decks opened, narration played, beside
        # the questions and goals.
        activity = engagement_mod.class_activity(db, current.course, roster_ids, since, until,
                                                 staff_ids=[r.student_id for r in rows if r.is_staff])
        # Where each student stands within the class (review step 0.9): a
        # percentile per measure, rank only, nothing under five students.
        standings = class_metrics.standings(rows, activity.rows)
        if student:
            detail = _student_detail(db, viewer, current, student, rows, since, until)
            detail["activity"] = activity.rows.get(detail["student"].id)
            detail["standing"] = next((st for st in standings if st.student_id == detail["student"].id), None)
        # The rollup the Today panel and the offline store read (step 0.9),
        # kept current by the pages that compare students; throttled.
        student_days.refresh(db)

    return templates.TemplateResponse(request, "instructor.html", {
        **nav_context(viewer, db),
        "viewer": viewer,
        "classes": [
            {"id": c.id, "label": c.label, "course": c.course, "archived": c.archived}
            for c in my_classes
        ],
        "current": current,
        "summary": summary,
        "rows": rows,
        "days": days,
        "detail": detail,
        "flagged": flagged,
        "sources": sources,
        "activity": activity,
        "standings": standings,
        "percentile_metrics": class_metrics.PERCENTILE_METRICS,
        "thresholds": engagement_mod.live_thresholds(),
        "overview": None,
        "lecture_titles": _lecture_titles(current.course) if current else {},
        "conversations": None,
        "instructors": _instructors_ctx(db, viewer, current),
        "note": classes_mod.NOTES.get(request.query_params.get("note") or ""),
    })


def _roster_student_or_403(db: DBSession, viewer: Student, current: CourseClass, email: str) -> Student:
    """The student, who must be visible to the viewer and on this class's roster."""
    target = _student_or_403(db, viewer, email)
    if target.id not in set(classes_mod.roster_ids(db, current.id)):
        raise HTTPException(status_code=403, detail="Not in this class")
    return target


def _conversations_view(db: DBSession, request: Request, viewer: Student, current: CourseClass,
                        days: int, student: Optional[str]) -> tuple[types.SimpleNamespace, Optional[dict]]:
    """The list for one class: the roster's conversations in the class's course,
    sorted and filtered from the query string, with no cost column and no admin
    links; the second value is the "seen" cookie to set, if any."""
    params = request.query_params
    days = conv_mod.clamp_days(days)
    key, desc = conv_mod.sort_spec(params.get("sort"), params.get("dir"))
    flagged = params.get("flagged") == "1"
    ids = classes_mod.roster_ids(db, current.id)
    target_email = None
    if student:
        target = _roster_student_or_403(db, viewer, current, student)
        ids, target_email = [target.id], target.email
    now = utcnow()
    listing = conv_mod.recent(db, now - datetime.timedelta(days=days), now=now, student_ids=ids,
                              course=current.course, flagged_only=flagged)
    seen = conv_mod.seen_state(request.cookies.get(CONVERSATIONS_COOKIE), now)
    new_count = conv_mod.mark_new(listing.rows, seen.baseline)
    rows = conv_mod.sort_rows(listing.rows, key, desc)
    for c in rows:
        who = {"class_id": current.id, "student": c.email}
        c.links = {
            "thread": "/instructor/conversations/thread?" + urllib.parse.urlencode(
                {**who, "start": c.start_iso, "end": c.end_iso}),
            "open": "/instructor/transcript/view?" + urllib.parse.urlencode(who) + f"#msg-{c.first_message_id}",
        }
    base: dict[str, object] = {"view": "conversations", "class_id": current.id, "days": days,
                               "flagged": "1" if flagged else "", "student": target_email or ""}
    view = types.SimpleNamespace(
        listing=listing, rows=rows, days=days, day_choices=conv_mod.DAY_CHOICES, flagged=flagged,
        course=None, courses=None, classes=None, class_id=current.id, students=[], student=target_email,
        new_count=new_count, show_cost=False, show_admin_links=False, form_action="/instructor",
        hidden=[("view", "conversations"), ("class_id", str(current.id))],
        sort_links=conv_mod.sort_links("/instructor", base, key, desc),
    )
    cookie = None
    if seen.cookie:
        cookie = dict(key=CONVERSATIONS_COOKIE, value=seen.cookie, max_age=CONVERSATIONS_COOKIE_DAYS * 86400,
                      httponly=True, samesite="lax", secure=_secure_cookies(request), path="/instructor")
    return view, cookie


@router.get("/instructor/conversations/thread", response_class=HTMLResponse)
def instructor_conversations_thread(
    request: Request,
    class_id: int = Query(...),
    student: str = Query(..., description="student email"),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """One conversation's messages as a fragment for the list's expand-in-place,
    narrowed to the class's course and guarded like the transcript download."""
    current = _class_or_403(db, viewer, class_id)
    target = _roster_student_or_403(db, viewer, current, student)
    since, until = conv_mod.parse_ts(start), conv_mod.parse_ts(end)
    messages = conv_mod.thread(db, target.id, since, until, course=current.course) if since and until else []
    return templates.TemplateResponse(request, "_conversation_thread.html", {"messages": messages})


@router.get("/instructor/transcript/view", response_class=HTMLResponse)
def instructor_transcript_view(
    request: Request,
    class_id: int = Query(...),
    student: str = Query(..., description="student email"),
    msg: Optional[int] = Depends(optional_int("msg")),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """The on-screen twin of the transcript download: one student's messages
    in this class's course, the newest TRANSCRIPT_PAGE_MESSAGES of them, the
    target of the list's new-tab link; with ``msg``, the window around that
    message, whatever its age (review step 0.4, F18)."""
    current = _class_or_403(db, viewer, class_id)
    target = _roster_student_or_403(db, viewer, current, student)
    if msg:
        messages = conv_mod.thread_around(db, target.id, msg, course=current.course)
    else:
        messages = conv_mod.thread(db, target.id, course=current.course, limit=TRANSCRIPT_PAGE_MESSAGES)
    return templates.TemplateResponse(request, "instructor_transcript.html", {
        **nav_context(viewer, db), "viewer": viewer, "current": current, "target": target,
        "messages": messages, "truncated": len(messages) >= TRANSCRIPT_PAGE_MESSAGES,
    })


def _lecture_titles(course: str) -> dict:
    from tutor.agent.lecture_plan import lecture_titles

    return lecture_titles(course)


def _student_detail(
    db: DBSession,
    viewer: Student,
    course_class: CourseClass,
    email: str,
    rows: list,
    since: datetime.datetime,
    until: datetime.datetime,
) -> dict:
    """One student's engagement row plus their recent messages in this course."""
    target = _student_or_403(db, viewer, email)
    engagement = next((r for r in rows if r.student_id == target.id), None)
    if engagement is None:
        # On the roster of another of the viewer's classes, but not this one.
        raise HTTPException(status_code=403, detail="Not in this class")

    recent = (
        db.query(Message)
        .filter(Message.student_id == target.id, Message.course == course_class.course)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(DRILLDOWN_MESSAGES)
        .all()
    )
    recent.reverse()
    feedback = {
        f.message_id: f
        for f in db.query(MessageFeedback)
        .filter(MessageFeedback.message_id.in_([m.id for m in recent] or [0]))
        .all()
    }
    return {
        "student": target,
        "engagement": engagement,
        "sources": grading_sources(db, [target.id], since, until),
        "messages": [
            {
                "id": m.id, "role": m.role, "content": m.content,
                "lecture_num": m.lecture_num, "created_at": m.created_at,
                "feedback": feedback.get(m.id),
            }
            for m in recent
        ],
        "truncated": len(recent) >= DRILLDOWN_MESSAGES,
    }


@router.get("/instructor/report", response_class=HTMLResponse)
def instructor_report_preview(
    request: Request,
    days: int = Depends(int_query("days", DEFAULT_DAYS)),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """Preview this week's email in the browser.

    Renders through the same functions scripts/instructor_report.py sends, so
    what an instructor reads here is what lands in their inbox. Sending is not
    exposed as a button on purpose: a page that can email a cohort on click is
    a page that emails a cohort by accident.
    """
    from tutor import instructor_report as report

    since, until, days = _window(days)
    # The preview shows every reportable class, quiet ones too, so an
    # instructor can see what a week with nothing in it would have said.
    weeks = report.gather(db, viewer, since, until, include_quiet=True)
    label = report.window_label(since, until)
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request, "instructor_report.html", {
        **nav_context(viewer, db),
        "viewer": viewer,
        "days": days,
        "label": label,
        "subject": report.subject(weeks, label),
        "body_html": report.render_html(viewer, weeks, label, base_url),
        "classes": [
            {"id": w.course_class.id, "label": w.summary.label} for w in weeks
        ],
    })


@router.post("/instructor/weekly-email")
def instructor_weekly_email(
    enabled: str = Form(default="off"),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """The instructor's own switch for the weekly email: on by default, off
    here. An admin's per-class switch (Admin → Classes) is the other gate."""
    set_weekly_email(db, viewer.email, enabled == "on")
    from tutor.logging_config import activity
    activity("weekly_email_set", student_id=viewer.id, enabled=(enabled == "on"))
    return RedirectResponse("/instructor", status_code=303)


def _back_to_class(class_id: int, note: str) -> RedirectResponse:
    return RedirectResponse(f"/instructor?class_id={class_id}&note={note}", status_code=303)


@router.post("/instructor/instructors/add")
def instructor_add_instructor(
    class_id: int = Form(...),
    email: str = Form(...),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """Any instructor of a class (or an admin) can add another to it. The
    class check is the same one every route here applies; the role comes
    with the add (classes.add_instructor), the login whitelist does not."""
    current = _class_or_403(db, viewer, class_id)
    note = classes_mod.add_instructor(db, current.id, email, added_by=viewer.email)
    from tutor.logging_config import activity
    activity("instructor_added", student_id=viewer.id, class_id=current.id, result=note)
    return _back_to_class(current.id, note)


@router.post("/instructor/instructors/remove")
def instructor_remove_instructor(
    class_id: int = Form(...),
    email: str = Form(...),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """Take another instructor off a class: never oneself, never the last one
    (classes.remove_instructor)."""
    current = _class_or_403(db, viewer, class_id)
    note = classes_mod.remove_instructor(db, current.id, email, actor_email=viewer.email)
    from tutor.logging_config import activity
    activity("instructor_removed", student_id=viewer.id, class_id=current.id, result=note)
    return _back_to_class(current.id, note)


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

_CSV_COLUMNS = (
    "email", "name", "status", "login_count", "last_login", "last_active",
    "messages", "replies", "active_days", "goals_seen", "goals_mastered",
    "goals_confirmed", "exercises", "avg_score", "exams_passed", "exams_failed",
    "reviews_due", "thumbs_down",
)


def _iso(value) -> str:
    return value.isoformat() + "Z" if isinstance(value, datetime.datetime) else ""


@router.get("/instructor/export")
def instructor_export(
    class_id: int = Query(...),
    days: int = Depends(int_query("days", DEFAULT_DAYS)),
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """Download the class's engagement table (one row per student)."""
    since, until, days = _window(days)
    current = _class_or_403(db, viewer, class_id)
    roster = classes_mod.class_roster(db, current.id)
    summary, rows = class_metrics.class_report(db, current, roster, since, until)

    stem = f"tutor-class-{current.id}-{utcnow().strftime('%Y%m%d')}"
    if format == "json":
        body = json.dumps({
            "class": {"id": current.id, "name": current.name, "course": current.course,
                      "term": current.term, "instructor": viewer.email},
            "window": {"days": days, "since": _iso(since), "until": _iso(until)},
            "summary": {
                "students": summary.students, "active": summary.active,
                "dormant": summary.dormant, "never": summary.never,
                "messages": summary.messages, "replies": summary.replies,
                "exercises": summary.exercises, "avg_score": summary.avg_score,
                "exams_passed": summary.exams_passed, "exams_failed": summary.exams_failed,
                "goals_mastered": summary.goals_mastered,
                "goals_confirmed": summary.goals_confirmed,
                "median_messages": summary.median_messages,
                "top_lectures": summary.top_lectures,
            },
            "students": [
                {c: (_iso(getattr(r, c)) if c in ("last_login", "last_active")
                     else getattr(r, c))
                 for c in _CSV_COLUMNS}
                for r in rows
            ],
        }, ensure_ascii=False, indent=2)
        return Response(
            content=body, media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}.json"'},
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        writer.writerow([
            _iso(getattr(r, c)) if c in ("last_login", "last_active") else
            ("" if getattr(r, c) is None else getattr(r, c))
            for c in _CSV_COLUMNS
        ])
    return Response(
        content=buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
    )


@router.get("/instructor/transcript")
def instructor_transcript(
    student: str = Query(..., description="student email"),
    class_id: int = Query(...),
    format: str = Query(default="md", pattern="^(md|json)$"),
    viewer: Student = Depends(require_instructor),
    db: DBSession = Depends(get_db),
):
    """Download one student's full conversation for this class's course.

    Narrowed to the class's course on purpose: an instructor of one course
    downloads that course's conversation, not the student's work in another
    course with another instructor.
    """
    current = _class_or_403(db, viewer, class_id)
    target = _roster_student_or_403(db, viewer, current, student)

    rows = (
        db.query(Message)
        .filter(Message.student_id == target.id, Message.course == current.course)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .all()
    )
    feedback = {
        f.message_id: f
        for f in db.query(MessageFeedback).filter_by(student_id=target.id).all()
    }
    if format == "json":
        body = transcripts.render_json(target, rows, current.course, feedback)
        media, ext = "application/json", "json"
    else:
        body = transcripts.render_markdown(target, rows, current.course, feedback)
        media, ext = "text/markdown", "md"
    return Response(
        content=body, media_type=f"{media}; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{transcripts.export_filename(target.email, ext)}"'
        },
    )
