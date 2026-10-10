"""The error page's one control: the Tell-the-instructor button (review
step 0.1, decisions 2 and 13). A plain form POST, so it works with no
script and at 390px; the row and the email are
tutor.request_errors.report's."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session as DBSession

from tutor import request_errors
from tutor.auth.deps import get_current_student, get_db
from tutor.db.models import Student
from tutor.nav import nav_context
from tutor.templating import templates

router = APIRouter()


@router.get("/errors/report")
def report_page_again():
    """The thanks page answers a POST: reloading it from the address bar or
    returning to it with Back asks with a GET, which was a 405 error page
    (R17). The report is in; go on."""
    return RedirectResponse("/", status_code=303)


@router.post("/errors/report", response_class=HTMLResponse)
def report_problem(
    request: Request,
    ref: str = Form(""),
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """A plain ``def``: FastAPI runs it in the threadpool (plan step 2.6)."""
    row = request_errors.report(db, student, ref, request.headers.get("user-agent"))
    return templates.TemplateResponse(
        request, "error.html",
        {**nav_context(student, db), "reported": True, "ref": row.detail if row else "",
         "heading": "Thanks", "message": "", "status": None, "kind": request_errors.REPORT},
    )
