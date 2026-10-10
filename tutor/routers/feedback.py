"""One link for a flagged reply, whoever opens it (review step 0.4, F17,
F20): ``GET /feedback/{feedback_id}`` sends an admin to the admin
transcript, a class instructor of that student to their transcript view,
and a visitor to the login page with the whole destination kept; each lands
on the reply, highlighted, with the window of messages around it. The
daily email, the Today panel and the weekly email all link here, so there
is one link shape and it works for the reader's role."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DBSession

from tutor.auth.deps import get_db, session_student
from tutor.db.models import Message, MessageFeedback, Student

router = APIRouter()


def destination(db: DBSession, feedback_id: int | str, viewer: Optional[Student], admin: bool) -> Optional[str]:
    """Where this reader opens the flagged reply, or None when they may not."""
    from tutor import classes as classes_mod
    from tutor.auth.access import is_instructor

    from tutor.routers.params import bounded_int

    # A hand-edited id past 2^31 is "no such feedback", not a 500 (R4), and
    # so is one that is not a number at all, not FastAPI's 422 JSON (R13).
    fid = bounded_int(feedback_id)
    fb = db.get(MessageFeedback, fid) if fid else None
    if fb is None:
        raise HTTPException(status_code=404, detail="No such feedback")
    msg = db.get(Message, fb.message_id)
    owner = db.get(Student, fb.student_id)
    if msg is None or owner is None:
        raise HTTPException(status_code=404, detail="No such feedback")
    email = quote(owner.email, safe="")
    if admin:
        return f"/admin?tab=conversations&student={email}&msg={msg.id}#msg-{msg.id}"
    if viewer is not None and is_instructor(db, viewer.email):
        for course_class in classes_mod.classes_for_instructor(db, viewer.id):
            if msg.course and msg.course != course_class.course:
                continue
            if owner.id in classes_mod.roster_ids(db, course_class.id):
                return (f"/instructor/transcript/view?class_id={course_class.id}&student={email}"
                        f"&msg={msg.id}#msg-{msg.id}")
    return None


@router.get("/feedback/{feedback_id}")
def open_feedback(
    request: Request,
    feedback_id: str,
    session_token: Optional[str] = Cookie(default=None),
    admin_session: Optional[str] = Cookie(default=None),
    db: DBSession = Depends(get_db),
):
    """A plain ``def``: FastAPI runs it in the threadpool (plan step 2.6)."""
    from tutor.auth.access import is_admin
    from tutor.routers.admin import _admin_session_ok

    viewer = session_student(db, session_token)
    admin = _admin_session_ok(admin_session) or (viewer is not None and is_admin(viewer.email))
    if viewer is None and not admin:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/auth/login?next={quote(target, safe='')}", status_code=303)
    where = destination(db, feedback_id, viewer, admin)
    if where is None:
        raise HTTPException(status_code=403, detail="Not your student")
    return RedirectResponse(where, status_code=303)
