"""First-login welcome page: pick a course, see what the tutor does, start.

This is the first screen a new student sees after logging in (the /chat gate
sends anyone without a StudentPath here). It replaces the track picker in that
role: a student who knows nothing about the tutor should choose their course in
one tap and read four lines about how the tutor works, not pick between
"Foundations First" and "Exam Prep". The track picker stays available as an
optional page.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from tutor.templating import templates
from sqlalchemy.orm import Session as DBSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from tutor.agent.navigator import current_course, default_track_id
from tutor.agent.state import ensure_student_path
from tutor.auth.deps import get_current_student, get_db, session_student
from tutor.db.guard import best_effort
from tutor.nav import nav_context
from tutor.db.models import Student
from tutor.courses import COURSE_LIST, COURSES, course_color
from tutor.routers.params import read_form

router = APIRouter(tags=["onboarding"])

# Student-facing course summaries, from courses.yaml (label, title, topics,
# note); keep each course's topics to one line there.
def _course_cards() -> list[dict[str, str]]:
    return [{"number": c.id, "title": c.label, "subtitle": c.title, "topics": c.topics,
             "note": c.note, "color": course_color(c.id)["strong"]} for c in COURSE_LIST]


def first_name(student: Student) -> str:
    """The first word of the student's display name, or "" when they have
    none. Magic-link sign-up stores the email's local part as the name, and
    "Hi jsmith3" read like a mistake (plan step 3.6, finding D24), so that
    counts as no name and the greeting is just "Hi."."""
    name = (student.name or "").strip()
    local = (student.email or "").split("@")[0].strip()
    if not name or name == local:   # exact: sign-up copies the local part as is
        return ""
    return name.split()[0]


def _render(request: Request, student: Student, db: DBSession, *, error: str = "",
            status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "welcome.html",
        {
            **nav_context(student, db),
            "first_name": first_name(student),
            "courses": _course_cards(),
            "current": current_course(student.id, db),
            "error": error,
        },
        status_code=status_code,
    )


def _session_student(request: Request, db: DBSession) -> Student | None:
    """The student of a live session cookie, or None (read-only; never
    raises). The help page is public: signed in, it shows the site header;
    signed out, a Log in link."""
    # A public page: a database hiccup reads as signed out, logged, in a
    # savepoint (plan step 5.6).
    return best_effort(db, lambda: session_student(db, request.cookies.get("session_token")),
                       None, "the help page's session check")


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, db: DBSession = Depends(get_db)):
    """Public help page (no login needed): what the tutor does, what it will
    not do, how to ask, where things are. Same text as the chat's ? modal
    (templates/_help_content.html), so the flyer and the login page can link
    to it."""
    student = _session_student(request, db)
    context = nav_context(student, db) if student is not None else {}
    return templates.TemplateResponse(
        request, "help.html", {**context, "logged_in": student is not None}
    )


@router.get("/welcome", response_class=HTMLResponse)
def welcome_page(
    request: Request,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    return _render(request, student, db)


@router.post("/welcome")
async def welcome_choose_course(
    request: Request,
    form: FormData = Depends(read_form),          # the body first, then the login check (R1)
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Make the chosen course current (creating its default-track path on the
    first pick) and go to chat."""
    course = str(form.get("course", "")).strip()

    def choose():
        if course not in COURSES:
            return _render(
                request, student, db,
                error="Pick one of the courses to continue.", status_code=400,
            )
        ensure_student_path(db, student.id, course, default_track_id(course))
        db.commit()
        return RedirectResponse("/chat", status_code=303)

    # The database work runs in the threadpool, off the event loop (plan
    # step 2.6, C1).
    return await run_in_threadpool(choose)
