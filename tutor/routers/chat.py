"""Chat UI and streaming agent endpoint."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from tutor.templating import templates
from sqlalchemy.orm import Session as DBSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from tutor import decks, streambuf
from tutor.auth.access import is_admin, is_instructor
from tutor.auth.deps import get_current_student, get_db
from tutor.courses import COURSES, DEFAULT_COURSE, optional_course, require_course
from tutor.db.models import Message, MessageFeedback, Student, TurnEvent
from tutor.routers.params import int_query, read_form
from tutor.utctime import utcnow

router = APIRouter(tags=["chat"])

# Per-deploy token appended to slide-deck iframe URLs so a re-seeded deck is
# fetched fresh instead of from a stale browser/CDN cache entry. Changes every
# deploy: the git commit on Render, else the process start time locally.
SLIDE_VERSION = (os.getenv("RENDER_GIT_COMMIT", "")[:8] or str(int(time.time())))

def resolve_course(requested: Optional[str], student_id: int, db: DBSession) -> str:
    """The course a page should open on: an explicit valid ``?course=``, else
    the student's current course (most recently chosen), else the first course
    in courses.yaml."""
    if requested is not None and requested in COURSES:
        return requested
    from tutor.agent.navigator import current_course

    return current_course(student_id, db) or DEFAULT_COURSE


def _voice_configured() -> bool:
    """Both ElevenLabs settings are present (else /chat/tts answers 503)."""
    from tutor.config import get_config

    cfg = get_config()
    return bool(cfg["elevenlabs_api_key"] and cfg["elevenlabs_voice_id"])


@router.get("/chat", response_class=HTMLResponse)
def chat_page(
    request: Request,
    background_tasks: BackgroundTasks,
    course: Optional[str] = None,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    # First-login gate: a student who has never chosen a course (no StudentPath
    # for any course) goes to /welcome, which explains the tutor and creates the
    # default-track path for the course they pick. Once any path exists they
    # come straight here; the track picker is optional from then on.
    from tutor.db.models import StudentPath

    has_track = (
        db.query(StudentPath.id).filter_by(student_id=student.id).first() is not None
    )
    if not has_track:
        return RedirectResponse("/welcome", status_code=303)

    from tutor.agent.lecture_plan import all_lecture_titles
    from tutor.agent.turn import warm_prompt_cache
    from tutor.routers.onboarding import first_name

    # The student is about to ask something: write the tutor's prompt cache
    # now, after the page is sent, so the first reply does not wait on it
    # (B1). Throttled; a no-op while warm.
    background_tasks.add_task(warm_prompt_cache, student.id)

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "student": student,
            "is_admin": is_admin(student.email),
            "is_instructor": is_instructor(db, student.email),
            "slide_version": SLIDE_VERSION,
            # The speak button shows only when a voice is configured (plan
            # step 3.5, finding D16); /chat/tts answers 503 without one.
            "tts_enabled": _voice_configured(),
            "max_message_chars": MAX_MESSAGE_CHARS,
            "course": resolve_course(course, student.id, db),
            "first_name": first_name(student),
            # Lecture titles for the header dropdown, keyed by course, for
            # |tojson: it escapes <, >, &, and ' as JSON unicode escapes, so a
            # title can never close the script tag or open another (plan
            # step 5.4, finding C24).
            "lecture_titles": all_lecture_titles(),
            # Which lectures have a deck on disk, so the picker can say "no
            # slides yet" and a reply's link to a missing deck is caught
            # (review step 0.3).
            "decks": decks.available_decks(),
        },
    )


@router.get("/chat/starters")
def chat_starters(
    course: Optional[str] = None,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Starter-prompt chips for a student who does not know what to ask:
    a personalized teach/practice pair for their next goal plus a few curated
    course questions (see tutor/agent/starters.py)."""
    from tutor.agent.starters import starter_prompts

    course = resolve_course(optional_course(course), student.id, db)
    return JSONResponse({"course": course, "starters": starter_prompts(student.id, course, db)})


@router.get("/chat/history")
def chat_history(
    course: str = DEFAULT_COURSE,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Return this student's recent messages for a course so the chat survives
    a page reload. Ordered oldest-first; capped to the last 100 messages."""
    course = require_course(course)
    rows = (
        db.query(Message)
        .filter(Message.student_id == student.id, Message.course == course)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(100)
        .all()
    )
    rows.reverse()
    # The id lets the page's activity events name a reply the student plays
    # or rates after a reload, and the rating pre-selects the thumb they
    # already gave (review step 0.4).
    ratings = {
        f.message_id: f.rating for f in
        db.query(MessageFeedback).filter(MessageFeedback.message_id.in_([r.id for r in rows] or [0])).all()
    }
    return JSONResponse(
        [{"id": r.id, "role": r.role, "content": r.content, "lecture_num": r.lecture_num,
          "rating": ratings.get(r.id)}
         for r in rows]
    )


@router.post("/chat/teachme")
async def chat_teachme(
    request: Request,
    form: FormData = Depends(read_form),          # the body first, then the login check (R1)
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Serve a pre-generated "Teach me" opener as a persisted Q/A turn.

    Zero API call: returns a curated opener for the goal at the student's track
    and requested depth (see tutor/agent/teachme.py). On a cache miss returns
    ``{"cached": false}`` so the client falls back to a normal live turn. On a
    hit, inserts the ``Explain: <title>`` user message + opener assistant message
    (once — a refresh re-serving the same opener is a no-op) so follow-up live
    turns keep the conversation context.
    """
    goal_id = str(form.get("goal", "")).strip()
    course = require_course(form.get("course") or DEFAULT_COURSE)
    depth = (str(form.get("depth", "")).strip() or None)
    lecture_num = _int_or_none(form.get("lecture_num"))
    again = form.get("again") == "1"            # a press, not a reload (chat.js)
    if not goal_id:
        return JSONResponse({"cached": False})
    # The database work runs in the threadpool, off the event loop (plan
    # step 2.6, C1).
    return await run_in_threadpool(
        _serve_teachme, db, student.id, goal_id, course, depth, lecture_num, again,
    )


# The longest message the tutor takes: about 500 lines of code, a few cents of
# input (R22 and section 4). The page checks it before sending; history clips
# older pastes (agent/memory.py).
MAX_MESSAGE_CHARS = 30_000


def too_long_sentence(chars: int) -> str:
    return (f"That is {chars:,} characters, and the tutor takes up to {MAX_MESSAGE_CHARS:,} at once "
            "(about 500 lines of code). Send the part your question is about, or split it into two messages.")


# How far back a served opener counts as already in the conversation (R18).
TEACHME_REPEAT_WINDOW = 20
TEACHME_REPEAT_AGE = datetime.timedelta(hours=2)


def _serve_teachme(db: DBSession, student_id: int, goal_id: str, course: str,
                   depth: Optional[str], lecture_num: Optional[int], again: bool = False) -> JSONResponse:
    from tutor.agent.navigator import get_student_track
    from tutor.agent.teachme import get_opener

    track_id = get_student_track(student_id, course, db)
    result = get_opener(goal_id, course=course, track_id=track_id, depth=depth)
    if not result:
        return JSONResponse({"cached": False})

    opener = result["opener"]
    # Idempotent: an opener that is the latest reply (a refresh, a double
    # tap) is not inserted again. Nor, unless the page says the student
    # pressed Teach me again (``again``), is one of the last few in the past
    # two hours: a reload after follow-ups appended it below them every time
    # (R18), and a page from before the flag reloads with goal= still in its
    # address.
    recent = (
        db.query(Message.content, Message.created_at)
        .filter_by(student_id=student_id, course=course, role="assistant")
        .order_by(Message.id.desc())
        .limit(TEACHME_REPEAT_WINDOW)
        .all()
    )
    since = utcnow() - TEACHME_REPEAT_AGE
    repeated = bool(recent) and (recent[0][0] == opener or (not again and any(
        content == opener and created is not None and created >= since for content, created in recent)))
    if not repeated:
        title = result.get("title") or goal_id
        db.add(Message(student_id=student_id, role="user",
                       content=f"Explain: {title}", course=course, lecture_num=lecture_num))
        db.add(Message(student_id=student_id, role="assistant",
                       content=opener, course=course, lecture_num=lecture_num))
        db.commit()

    return JSONResponse({"cached": True, "depth": result["depth"], "track": result["track"]})


@router.post("/chat/message")
async def chat_message(
    request: Request,
    form: FormData = Depends(read_form),          # the body first, then the login check (R1)
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Stream agent response as JSON SSE events.

    Event types emitted (newline-delimited JSON wrapped in SSE format):
      {"type": "turn",   "id": "..."}            resume handle, first
      {"type": "token",  "text": "..."}
      {"type": "slide",  "url": "...", "title": "...", "num": N}
      {"type": "eval",   "score": N, "correct": bool}
      {"type": "prediction", "predicted": N, "actual": N}
      {"type": "section", "full_text": "..."}    closes a saved reply mid-turn
      {"type": "error",  "message": "..."}
      {"type": "done",   "full_text": "...", "message_id": N}   exactly one, last
      {"type": "busy",   "message": "..."}       another turn is running
    """
    # NUL goes before the emptiness check: Postgres cannot store it, and a
    # message of only NUL would otherwise be saved as an empty turn (R8).
    # A browser's form encoding sends every line break as CRLF: counted as
    # sent, a paste the page let through at 29,999 characters measured
    # 30,498 here (the Stage 3 review). Stored as typed, too.
    user_msg  = str(form.get("message", "")).replace("\x00", "").replace("\r\n", "\n").strip()
    course    = require_course(form.get("course") or DEFAULT_COURSE)
    lecture_num = _int_or_none(form.get("lecture_num"))
    slide_num   = _int_or_none(form.get("slide_num"))

    if not user_msg:
        return HTMLResponse("", status_code=204)
    if len(user_msg) > MAX_MESSAGE_CHARS:
        # The page refuses it first; this is for a page from before the
        # limit, or a client that skips it (R22).
        return JSONResponse({"error": too_long_sentence(len(user_msg))}, status_code=413)

    # Predict-your-score calibration (pedagogy item 11): an optional 0-4
    # prediction rides the form. It is recorded server-side against the eval
    # result and NEVER enters the model pipeline: the grader must stay blind
    # to what the student expects.
    predicted: Optional[int] = None
    raw_pred = form.get("predicted_score")
    try:
        if raw_pred is not None and str(raw_pred).strip() != "":
            v = int(str(raw_pred).strip())
            if 0 <= v <= 4:
                predicted = v
    except (TypeError, ValueError):
        predicted = None

    student_id = student.id
    # Hand the request's pooled connection back before the reply streams
    # (R1): FastAPI closes this session only after the whole response is
    # sent, so every open reply held one of production's seven connections. The turn runs on its own session.
    await run_in_threadpool(db.commit)

    # Spend caps, classification, supervision, and telemetry all live inside
    # the turn pipeline; this endpoint only adapts events to SSE.
    from tutor.agent.turn import run_turn  # deferred import

    # Capability item 6: the agent runs in a background task writing
    # to a per-turn buffer (tutor/streambuf.py); this response only reads the
    # buffer. A client disconnect cancels the reader, not the turn: the reply
    # still completes and persists, and the client resumes via
    # GET /chat/message/{turn_id}?offset=N. The task owns its own DB session,
    # since the request session is torn down when this response ends.
    turn_id = uuid.uuid4().hex
    buf = streambuf.create(turn_id, student_id=student_id, course=course)

    async def _produce():
        db2 = _turn_session()
        # run_turn keeps loaded objects through its commits; so does loading
        # the student here, which then gives its connection back (R1).
        db2.expire_on_commit = False

        def _load_student() -> Optional[Student]:
            st = db2.get(Student, student_id)
            db2.commit()
            return st

        # The task's own database calls run in the threadpool, like the
        # turn's (plan step 2.6, C1). Each runs while the turn is paused at
        # an event, so the session is never used by two threads at once.
        def _record_prediction(predicted: int, actual: int, correct: bool) -> None:
            from tutor.agent import state as _state
            try:
                _state.record_score_prediction(
                    db2,
                    student_id=student_id,
                    course=course,
                    predicted=predicted,
                    actual=actual,
                    correct=correct,
                    context=user_msg,
                )
                db2.commit()
            except Exception:
                db2.rollback()
                logging.getLogger("tutor").warning(
                    "turn %s: could not record the score prediction", turn_id, exc_info=True)

        def _record_interrupted() -> None:
            from tutor.agent import state

            db2.rollback()
            state.close_interrupted_turn(db2, turn_id)
            db2.commit()

        def _record_crash(exc: Exception) -> None:
            from tutor.agent import state
            from tutor.agent.turn import CRASH_NOTE
            # P6: failed turns must be observable. Roll back whatever the
            # broken turn left dirty, then ledger the error itself: on the
            # turn's provisional chat row when stage 1 wrote one (so no turn
            # stays in_progress and the digest counts one failure), else as
            # an error row carrying the turn id (review step 0.8, F49).
            db2.rollback()
            provisional = (
                db2.query(TurnEvent)
                .filter(TurnEvent.turn_id == turn_id, TurnEvent.kind == "chat")
                .order_by(TurnEvent.id.desc())
                .first()
            )
            if provisional is not None:
                # Only what the crash knows: finalize_turn_event would reset
                # the intent, goals, envelope, rung and model stage 1 wrote.
                state.record_turn_crash(db2, provisional, str(exc))
            else:
                state.record_turn_event(
                    db2,
                    student_id=student_id,
                    kind="error",
                    verdicts={"error": str(exc)[:300]},
                    turn_id=turn_id,
                )
            # The student's message may already be saved (plan step 2.5
            # commits it before the tutor runs): pair it with a note so the
            # history does not end on an unanswered message.
            last = (
                db2.query(Message.role)
                .filter(Message.student_id == student_id, Message.course == course)
                .order_by(Message.id.desc())
                .first()
            )
            if last is not None and last[0] == "user":
                db2.add(Message(
                    student_id=student_id, role="assistant", course=course,
                    lecture_num=lecture_num, slide_num=slide_num,
                    content=CRASH_NOTE,
                ))
            db2.commit()

        try:
            st2 = await run_in_threadpool(_load_student)
            if st2 is None:  # pragma: no cover - deleted mid-request
                await buf.append({"type": "error", "message": "Please log in again."})
                return
            calibrated = False
            async for event in run_turn(
                student=st2,
                db=db2,
                user_message=user_msg,
                lecture_num=lecture_num,
                slide_num=slide_num,
                course=course,
                turn_id=turn_id,
            ):
                if isinstance(event, dict) and event.get("type") == "busy":
                    # The reply this one waits on, so the page can show it
                    # coming instead of only "busy" (R30).
                    other = streambuf.running_for(student_id, course=course, exclude=turn_id)
                    if other is not None:
                        event = {**event, "turn_id": other.turn_id}
                await buf.append(event)
                # Calibration join: record prediction vs the FIRST eval of the
                # turn, then tell the client so it can show the comparison
                # under the score badge. Never allowed to break the turn.
                if (predicted is not None and not calibrated
                        and isinstance(event, dict) and event.get("type") == "eval"):
                    calibrated = True
                    actual = int(event.get("score") or 0)
                    await run_in_threadpool(_record_prediction, predicted, actual,
                                            bool(event.get("correct", False)))
                    await buf.append({
                        "type": "prediction",
                        "predicted": predicted,
                        "actual": actual,
                    })
        except asyncio.CancelledError:
            # The process is going (a deploy restarts the site): the reply
            # is lost, so the ledger row and the history say so instead of
            # the turn staying in progress and the question unanswered (R31).
            # Blocking calls on purpose: the loop is shutting down.
            try:
                _record_interrupted()
            except Exception:
                logging.getLogger("tutor").warning("turn %s: could not record the interruption", turn_id,
                                                   exc_info=True)
            raise
        except Exception as exc:
            from tutor.agent.turn import friendly_error
            try:
                await run_in_threadpool(_record_crash, exc)
            except Exception:
                # Never mask the original failure with a telemetry failure,
                # but do not hide this one either (plan step 5.6).
                await run_in_threadpool(db2.rollback)
                logging.getLogger("tutor").warning(
                    "turn %s: could not record the crash", turn_id, exc_info=True)
            await buf.append({"type": "error", "message": friendly_error(exc, failure="crash")})
        finally:
            await buf.finish()
            db2.close()

    buf.task = asyncio.create_task(_produce())

    async def event_stream():
        # The synthetic first event tells the client its resume handle; it is
        # not part of the buffer, so buffer offsets count agent events only.
        yield f"data: {json.dumps({'type': 'turn', 'id': turn_id})}\n\n"
        async for event in streambuf.stream(turn_id, start=0):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/chat/running")
def chat_running(
    course: Optional[str] = None,
    student: Student = Depends(get_current_student),
):
    """The student's reply still being written in this course, if any: a
    page opened or reloaded mid-reply reattaches to it, where it used to show
    the question with no reply and no sign one was coming (R30)."""
    buf = streambuf.running_for(student.id, course=course if course in COURSES else DEFAULT_COURSE)
    return JSONResponse({"turn_id": buf.turn_id if buf is not None else None})


@router.get("/chat/quota")
def chat_quota(
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """The student's dollar quotas for the page's indicator (tutor/quotas.py):
    each window's share used and when it resets, a note from 80 percent, and
    the blocking message when one is used up. The class-wide caps join them
    (R25): a used-up daily turn limit or monthly budget blocks the same way,
    and the turns left show from 80 percent of the limit."""
    from tutor import quotas
    from tutor.agent import usage as usage_mod

    q = quotas.indicator(db, student.id, email=student.email)
    caps = usage_mod.cap_indicator(db, student.id)
    if caps["blocked"]:
        # The same limit the turn's notice names: the one that lifts last.
        q["blocked"] = q["note"] = caps["blocked"]
    elif caps["note"] and not q.get("note"):
        q["note"] = caps["note"]
    return JSONResponse(q)


@router.post("/chat/transcribe-photo")
def chat_transcribe_photo(
    image: UploadFile = File(...),
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Photo -> code text (capability item 7). Sync endpoint on purpose:
    FastAPI runs it in the threadpool, so the blocking vision call cannot
    stall the event loop. The result fills the code box client-side for the
    student to review; nothing is graded here."""
    from tutor.agent import vision

    data = image.file.read(vision.MAX_IMAGE_BYTES + 1)
    if len(data) > vision.MAX_IMAGE_BYTES:
        return JSONResponse(
            {"ok": False, "error": "Photo too large (6 MB max). Retake at lower resolution."},
            status_code=413,
        )
    media_type = vision.sniff(data)
    if media_type is None:
        return JSONResponse(
            {"ok": False, "error": "Unsupported image type. Use a JPEG or PNG photo."},
            status_code=415,
        )
    from tutor.agent import state, usage as usage_mod
    from tutor.agent.models import SUPERVISOR

    limit_msg = usage_mod.over_spend_caps(db, student.id)
    if limit_msg:
        db.commit()
        return JSONResponse({"ok": False, "error": limit_msg}, status_code=429)
    if not vision.allow(student.id):
        from tutor import quotas

        frees = vision.frees_at(student.id)
        when = f" The next one opens {quotas.when_phrase('rolling', frees)}." if frees else ""
        return JSONResponse(
            {"ok": False, "error": f"That is today's photos used up.{when} Type or paste the code instead."},
            status_code=429,
        )
    # The vision call can take most of a minute: the connection goes back
    # first (R1).
    db.commit()
    result = vision.transcribe_image(data, media_type)
    counts = result.pop("usage", None)
    if counts is None and not result.get("ok"):
        vision.release(student.id)       # never reached the model: the slot goes back
    if isinstance(counts, dict) and counts:
        # The call is ledgered like every other model call, so the quotas
        # and the class budget count it (R26).
        try:
            state.record_turn_event(db, student_id=student.id, kind="vision",
                                    **usage_mod.cost_fields(SUPERVISOR.id, counts))
            db.commit()
        except Exception:
            db.rollback()
            logging.getLogger("tutor").warning("photo: could not ledger the vision call", exc_info=True)
    return JSONResponse(result, status_code=200 if result.get("ok") else 422)


def _turn_session() -> DBSession:
    """A DB session owned by the background turn task (patchable in tests).
    The request-scoped session cannot be used: FastAPI closes it when the
    response finishes, which can precede the turn finishing after a client
    disconnect."""
    from tutor.db.models import app_engine, get_session_factory

    return get_session_factory(app_engine())()


@router.get("/chat/message/{turn_id}")
async def chat_message_resume(
    turn_id: str,
    offset: int = Depends(int_query("offset", 0, lo=0)),
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Resume a turn's event stream from ``offset`` after a dropped
    connection. 404 for unknown/expired turns or another student's turn (the
    client then falls back to the error bubble + history reload)."""
    buf = streambuf.get(turn_id)
    if buf is None or buf.student_id != student.id:
        return JSONResponse({"error": "unknown turn"}, status_code=404)
    # The session the login check used goes back to the pool before the
    # stream, which can run for minutes (R1).
    await run_in_threadpool(db.commit)

    async def event_stream():
        async for event in streambuf.stream(turn_id, start=offset):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _recent_assistant_message(db: DBSession, student_id: int, text: str) -> Optional[Message]:
    """The newest of this student's last 20 assistant messages whose content
    equals ``text`` (whitespace-normalized), or None.

    The client never learns message ids (the SSE events carry none; ``done``
    fires only after the reply row is committed), so both the voice and the
    feedback endpoints identify a reply by its text and accept only text WE
    generated.
    """
    def norm(s: str) -> str:
        return " ".join(s.split())

    target = norm(text)
    if not target:
        return None
    rows = (
        db.query(Message)
        .filter_by(student_id=student_id, role="assistant")
        .order_by(Message.id.desc())
        .limit(20)
        .all()
    )
    for m in rows:
        if norm(m.content) == target:
            return m
    return None


def _is_recent_assistant_text(db: DBSession, student_id: int, text: str) -> bool:
    """True iff ``text`` is (or contains nothing beyond) a reply WE generated.

    The voice endpoint must only ever speak server-generated replies: free
    text in the instructor's cloned voice is an impersonation vector
    (audit item S7). Compares against this student's recent assistant
    messages, whitespace-normalized.
    """
    return _recent_assistant_message(db, student_id, text) is not None


FEEDBACK_COMMENT_MAX = 2000


@router.post("/chat/feedback")
async def chat_feedback(
    request: Request,
    form: FormData = Depends(read_form),          # the body first, then the login check (R1)
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Record a thumbs up/down on a tutor reply, with an optional reason.

    Form fields: ``message_id`` (the reply's id, which the stream's done event
    and /chat/history carry; review step 0.4, F14: by text only the newest
    20 replies could be rated while the page shows 100), ``text`` (the reply,
    the fallback when the page has no id, matched the way /chat/tts does),
    ``rating`` (``1`` or ``-1``), ``comment`` (optional; the answer to "what
    was wrong?", asked after a thumbs down). One row per message: a second
    click overwrites the rating, and a later comment on the same reply fills
    in the row created by the click. 404 when neither names one of this
    student's replies.
    """
    text = str(form.get("text", "")).replace("\x00", "").strip()
    message_id = _int_or_none(form.get("message_id"))       # a hand-edited id is no id (R4)
    try:
        rating = int(str(form.get("rating", "")).strip())
    except ValueError:
        rating = 0
    if rating not in (1, -1):
        raise HTTPException(status_code=422, detail="rating must be 1 or -1")
    comment = str(form.get("comment", "") or "").replace("\x00", "").strip()[:FEEDBACK_COMMENT_MAX] or None
    # The database work runs in the threadpool, off the event loop (plan
    # step 2.6, C1).
    return await run_in_threadpool(_save_feedback, db, student.id, text, rating, comment, message_id)


def _own_assistant_message(db: DBSession, student_id: int, message_id: Optional[int]) -> Optional[Message]:
    """The student's own reply by id, or None."""
    if not message_id:
        return None
    msg = db.get(Message, message_id)
    if msg is None or msg.student_id != student_id or msg.role != "assistant":
        return None
    return msg


def _save_feedback(db: DBSession, student_id: int, text: str, rating: int,
                   comment: Optional[str], message_id: Optional[int] = None) -> JSONResponse:
    msg = _own_assistant_message(db, student_id, message_id) or _recent_assistant_message(db, student_id, text)
    if msg is None:
        raise HTTPException(status_code=404, detail="Text is not a recent tutor reply")

    from sqlalchemy.exc import IntegrityError

    from tutor.agent import state

    message_id = msg.id
    # Two clicks on a reply nobody rated yet both insert its row; the second
    # loses the race on message_feedback's unique message_id. It rolls back
    # and runs again, and then finds the row and updates it
    # (R9).
    for attempt in (1, 2):
        try:
            fb = state.record_message_feedback(
                db, message_id=message_id, student_id=student_id, rating=rating, comment=comment,
            )
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
    return JSONResponse({"ok": True, "message_id": message_id, "rating": fb.rating,
                         "has_comment": bool(fb.comment)})


@router.post("/chat/tts")
async def chat_tts(
    request: Request,
    form: FormData = Depends(read_form),          # the body first, then the login check (R1)
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Speak an assistant reply as MP3 in the instructor's voice.

    Auth-gated, and restricted to the student's own saved replies, so the
    cloned voice can never be made to say arbitrary student-supplied words:
    a reply named by ``message_id`` is spoken from its stored text, whatever
    its age (an older reply in the history failed the text match, R29), and
    otherwise the text must match one of the 20 most recent. Returns 503
    when voice is not configured, 403 for unrecognized text.
    """
    text = str(form.get("text", "")).strip()
    message_id = _int_or_none(form.get("message_id"))
    # The lookups run in the threadpool, off the event loop (plan step 2.6).
    saved = (await run_in_threadpool(_own_assistant_message, db, student.id, message_id)
             if message_id is not None else None)
    if saved is not None and (saved.content or "").strip():
        text = saved.content.strip()
    elif not text:
        return Response(status_code=204)
    elif not await run_in_threadpool(_is_recent_assistant_text, db, student.id, text):
        raise HTTPException(status_code=403, detail="Text is not a recent tutor reply")
    # Synthesis can take most of a minute: the connection goes back first (R1).
    await run_in_threadpool(db.commit)

    from tutor.agent.tts import synthesize  # deferred import

    # synthesize() makes a blocking HTTP call to ElevenLabs on a cache miss;
    # run it off the event loop so one slow synthesis cannot stall every other
    # concurrent request sharing this worker.
    loop = asyncio.get_running_loop()
    try:
        audio = await loop.run_in_executor(None, synthesize, text)
    except httpx.HTTPError as exc:
        # ElevenLabs refused (quota, a bad voice id) or could not be reached:
        # the documented 503, not a 500 that emails the operator (plan step
        # 6.2, finding F11). The reply text stays out of the log.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logging.getLogger("tutor").warning("tts: ElevenLabs failed (%s%s)", type(exc).__name__,
                                           f" {status}" if status else "")
        raise HTTPException(status_code=503, detail="Voice unavailable") from exc
    if audio is None:
        raise HTTPException(status_code=503, detail="Voice not configured")
    return Response(content=audio, media_type="audio/mpeg")


@router.get("/chat/progress", response_class=HTMLResponse)
def student_progress(
    request: Request,
    course: Optional[str] = None,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """The progress panel for the chat's sidebar, rendered from
    ``_progress.html`` (plan step 5.4, C23).

    ``course`` selects which course the counts, the review nudge, the "What's
    next" card, and the track link follow; it defaults to the student's
    current course.
    """
    from sqlalchemy import func
    from tutor.db.models import ExerciseAttempt, LearningGoal, StudentProgress, TopicAccess
    from tutor.routers.review import due_rows

    course = resolve_course(optional_course(course), student.id, db)

    # Messages and goals count this course only: the panel is headed with it
    # (plan step 3.6, finding D25: both spanned every course).
    msg_count = (
        db.query(func.count(Message.id))
        .filter(Message.student_id == student.id, Message.course == course)
        .scalar()
    )
    ex_rows = db.query(ExerciseAttempt).filter_by(student_id=student.id).order_by(
        ExerciseAttempt.created_at.desc()
    ).limit(5).all()
    topic_rows = (
        db.query(TopicAccess.topic_tag, TopicAccess.lecture_num)
        .filter_by(student_id=student.id)
        .order_by(TopicAccess.accessed_at.desc())
        .limit(8)
        .all()
    )
    scores = [e.score for e in ex_rows if e.score is not None]
    avg_score = round(sum(scores) / len(scores), 1) if scores else None

    total_goals = db.query(LearningGoal).filter(LearningGoal.course == course).count()

    def _count(status: str) -> int:
        return (
            db.query(StudentProgress)
            .join(LearningGoal, LearningGoal.id == StudentProgress.goal_id)
            .filter(StudentProgress.student_id == student.id, StudentProgress.status == status,
                    LearningGoal.course == course)
            .count()
        )

    unlocked = _count("unlocked")
    mastered = _count("mastered")
    # Spaced-review due count (Engram P1), for this course with the review
    # page's own query (plan step 3.1, finding D3).
    review_due = len(due_rows(student.id, course, db))
    seen: set[str] = set()
    topics = []
    for tag, lec in topic_rows:
        if tag[:30] not in seen:
            seen.add(tag[:30])
            topics.append((tag, lec))
    return templates.TemplateResponse(request, "_progress.html", {
        "course": course,
        # A student with no activity yet gets an orientation line instead of
        # a wall of zeros; the What's-next card still shows where to begin.
        "is_new": not msg_count and not ex_rows and not unlocked and not mastered,
        "total_goals": total_goals,
        "mastered": mastered,
        "unlocked": unlocked,
        "pct": round((unlocked + mastered) / total_goals * 100) if total_goals else 0,
        "review_due": review_due,
        "review_minutes": max(1, round(min(review_due, 20) * 0.7)),
        "msg_count": msg_count,
        "avg_score": avg_score,
        "topics": topics,
        "exercises": [{"exercise_id": e.exercise_id, "score": int(e.score or 0), "passed": e.passed}
                      for e in ex_rows],
        "next_goals": _whats_next(student.id, course, db),
    })


def _whats_next(student_id: int, course: str, db: DBSession) -> list[dict]:
    """The top three frontier goals for the "What's next" card, with their
    links; empty when the navigator fails (it is best-effort in the sidebar)."""
    import json as _json

    from tutor.agent.explorables import explorable_url
    from tutor.agent.navigator import default_track_id, get_student_track, next_goals
    from tutor.db.guard import best_effort
    from tutor.db.models import LearningGoal

    def _cards() -> list[dict]:
        track_id = get_student_track(student_id, course, db) or default_track_id(course)
        cards = []
        for gid in next_goals(student_id, course, db, track_id=track_id, n=3):
            g = db.get(LearningGoal, gid)
            if g is None:
                continue
            chat_url = (f"/chat?topic={urllib.parse.quote(g.title, safe='')}"
                        f"&course={urllib.parse.quote(g.course, safe='')}"
                        f"&lecture_num={urllib.parse.quote(str(g.lecture_num), safe='')}")
            slides_url = None
            if g.slide_anchor:
                # Only a deck that is on disk (review step 0.3, F3): a missing
                # one used to open a new tab on a 404.
                from tutor import decks

                try:
                    lec = int(_json.loads(g.slide_anchor).get("lecture", g.lecture_num))
                except (TypeError, ValueError):
                    lec = g.lecture_num
                slides_url = decks.deck_url(g.course, lec)
            cards.append({
                "id": gid, "title": g.title, "course": g.course, "chat_url": chat_url,
                "slides_url": slides_url,
                "explore_url": explorable_url(gid) if getattr(g, "threshold", False) else None,
            })
        return cards

    # Best-effort in the sidebar, in a savepoint (plan step 5.6).
    return best_effort(db, _cards, [], "the What's next card")


def _int_or_none(val) -> Optional[int]:
    """A lecture or slide number from a form, or None when it is not a
    positive whole number Postgres can hold (R4)."""
    from tutor.routers.params import bounded_int

    return bounded_int(val)
