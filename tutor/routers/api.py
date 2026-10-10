"""JSON API: the operators' chat endpoint and the browser's activity events.

``POST /api/chat`` runs the SAME supervised turn pipeline as the SSE chat
endpoint (caps, classifier, envelope, detector, judge, telemetry); it only
collects the streamed events into one JSON reply. There is no anonymous
endpoint: every path to the actor is authenticated and supervised
(audit item S3). It is for operators (admins) only; students use the
chat page. It reads the turn to the end rather than stopping at the first
``done`` event, so the persistence and telemetry that follow run for every
turn (finding A1 and decision 3).

Usage (with a logged-in session cookie):
  curl -s -X POST http://localhost:8000/api/chat \
       -b "session_token=..." \
       -H 'Content-Type: application/json' \
       -d '{"message": "explain moon phases", "course": "AST101"}' | python -m json.tool

``POST /api/events`` takes the batches ``track.js`` and ``deck-track.js``
post (step 1.3): each batch is cleaned against the vocabulary in
tutor/activity_events.py, capped per student, written in one statement off
the event loop, and mirrored to the ``tutor.activity`` stream at the
verbose tier. It answers 204 to anything a
signed-in student sends, valid or not: a browser must never retry a batch.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession
from starlette.concurrency import run_in_threadpool

from tutor import activity_events
from tutor.auth.access import is_admin
from tutor.auth.deps import get_db, get_current_student, session_id_for
from tutor.routers.params import read_body
from tutor.db.models import Student
from tutor.courses import DEFAULT_COURSE, require_course
from tutor.utctime import utcnow

router = APIRouter(prefix="/api", tags=["api"])
_log = logging.getLogger("tutor.activity_events")


class ChatRequest(BaseModel):
    message: str
    course: str = DEFAULT_COURSE
    lecture_num: Optional[int] = None


@router.post("/events", status_code=204)
async def record_events(
    request: Request,
    body: Optional[bytes] = Depends(read_body),   # the body first, then the login check (R1)
    student: Student = Depends(get_current_student),
    session_token: Optional[str] = Cookie(default=None),
    db: DBSession = Depends(get_db),
):
    """Store one batch of browser activity events for the signed-in student.

    The body is read here rather than through a model so an oversized one is
    refused before it is parsed. The database work (the session lookup and
    the bulk insert) runs in the threadpool; a failure there is logged and
    the batch is lost, never an error the page sees.
    """
    if body is None:
        # The tab closed or navigated while the batch was still arriving
        # (the leave beacon, most often): the batch is lost, which the
        # sequence numbers already show, and nothing went wrong on the server.
        return Response(status_code=204)
    if len(body) > activity_events.MAX_BODY_BYTES:
        return Response(status_code=413)
    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=400)
    rows = activity_events.clean_batch(payload, utcnow())
    if not rows:
        return None
    student_id = student.id
    if not activity_events.allow(student_id, len(rows)):
        _log.warning("activity events: student %s over %d rows a minute; batch of %d dropped",
                     student_id, activity_events.RATE_PER_MINUTE, len(rows))
        return None

    def _write() -> None:
        from tutor.agent import state

        login_session_id = session_id_for(db, session_token)
        state.record_activity_events(db, student_id, login_session_id, rows)
        db.commit()

    try:
        await run_in_threadpool(_write)
    except Exception:
        await run_in_threadpool(db.rollback)
        _log.warning("activity events: a batch of %d for student %s was not written",
                     len(rows), student_id, exc_info=True)
        return None
    activity_events.mirror(student_id, rows)
    return None


class ChatResponse(BaseModel):
    text: str
    slide_ref: Optional[dict] = None
    error: Optional[str] = None   # the friendly error, when the tutor call failed


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    if not is_admin(student.email):
        raise HTTPException(
            status_code=403,
            detail="The JSON chat API is for operators; students use the chat page.",
        )
    from tutor.agent.turn import run_turn  # deferred to keep startup fast

    course = require_course(req.course)

    # Read every event: a turn can carry more than one reply (an exam pass
    # replays the original question; a deferred exam acknowledges first), and
    # the work after the last "done" (the rolling summary) should still run.
    replies: list[str] = []
    slide_ref: Optional[dict] = None
    error: Optional[str] = None
    async for event in run_turn(
        student=student,
        db=db,
        user_message=req.message,
        lecture_num=req.lecture_num,
        course=course,
    ):
        kind = event.get("type")
        if kind in ("section", "done"):
            # A turn can hold two saved replies (plan step 2.5): a "section"
            # closes the first, "done" the last.
            if event.get("full_text"):
                replies.append(event["full_text"])
        elif kind == "slide":
            slide_ref = {
                "slide_url": event.get("url"),
                "lecture_title": event.get("title"),
                "slide_number": event.get("num"),
                "lecture_num": event.get("lecture_num"),
            }
        elif kind in ("error", "busy"):
            error = event.get("message", "")
    text = "\n\n".join(replies) or (error or "")
    return ChatResponse(text=text, slide_ref=slide_ref, error=error)
