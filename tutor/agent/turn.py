"""The supervised turn pipeline: the ONLY way a student message reaches the actor.

Every entry point (the SSE chat endpoint, the JSON API, future surfaces) calls
:func:`run_turn`. The pipeline stages, in order: spend caps -> active-exam
intercept -> pre-classifier -> conduct short-circuit -> envelope assembly ->
prerequisite gate -> one streaming actor call with a bounded tool loop ->
supervision (detector always, judge on risky turns) -> persistence + telemetry.
Each stage lives in :mod:`tutor.agent.turn_stages`; this module only
sequences them (plan step 5.2, finding A15).

Public API
----------
run_turn(...)   -- async generator yielding JSON-serialisable event dicts:
                   {"type": "token", "text": ...}
                   {"type": "slide", "url": ..., "title": ..., "num": N}
                   {"type": "eval",  "score": ..., "correct": ...}
                   {"type": "error", "message": ...}
                   {"type": "section", "full_text": ...}
                   {"type": "done",  "full_text": ..., "message_id": N}
                   {"type": "busy",  "message": ...}
                   {"type": "exam",  "goal": ..., "not_now": "Not now"}
                   A turn ends with exactly one "done". An "exam" comes
                   before a prerequisite check's text, so the chat can
                   offer a button that sends ``not_now``. A "section" closes
                   an earlier saved reply in the same turn (an exam result
                   before the question it held back, a deferral before the
                   answer); "busy" is the whole stream when another turn of
                   the same student is still running.
friendly_error(exc) -- user-facing message for an agent failure.
CRASH_NOTE          -- what history keeps when a turn crashes.

Persistence is two-stage (plan step 2.5): the student's message and a
provisional "chat" TurnEvent are committed before the actor runs; the reply
and the finished event are committed before "done"; learning-state
bookkeeping commits on its own and cannot take the reply down with it.

A test (tests/test_single_actor.py) asserts mechanically that no other module
constructs an actor client, imports a turn stage, or reimplements the loop,
so the supervision cannot be bypassed by a new endpoint (audit item S3).
"""

from __future__ import annotations

import time
import uuid
from typing import AsyncGenerator, Optional

from sqlalchemy.orm import Session as DBSession

from tutor import logging_config
from tutor.agent import usage as usage_mod
from tutor.agent.turn_stages import (
    actor_loop, cache_warm, cli_turn, context, exam_intercept, persist, preflight, supervision,
)
from tutor.agent.turn_stages.dbstep import db_step
from tutor.agent.turn_stages.errors import CRASH_NOTE, friendly_error
from tutor.agent.turn_stages.request import TurnRequest
from tutor.config import get_config
from tutor.db.models import Student
from tutor.courses import DEFAULT_COURSE

__all__ = ["run_turn", "warm_prompt_cache", "friendly_error", "CRASH_NOTE", "BUSY_MESSAGE"]

# Per-student in-flight turns (plan step 2.2, findings A6 and C22). A second
# message while a reply is still running is refused with a friendly "busy"
# event instead of racing the first: two concurrent turns used to pass the
# daily cap together (it counts committed events) and could grade one exam
# twice. With at most one turn in flight per student, the cap's count is exact.
# Process memory: the single-instance note in CLAUDE.md applies.
_INFLIGHT: dict[int, tuple[float, str]] = {}
# A claim older than this is treated as abandoned (a consumer that never
# closed its generator); no real turn runs this long.
INFLIGHT_STALE_SECONDS = 600
BUSY_MESSAGE = (
    "One question at a time: I'm still answering your last message. "
    "Send this again once that reply finishes."
)


def warm_prompt_cache(student_id: int) -> None:
    """Warm the actor's prompt cache for a student who just opened the chat
    (turn_stages/cache_warm.py). Synchronous: the chat page runs it as a
    background task, after the page is sent, on its own database session."""
    from tutor.db.models import app_engine, get_session_factory

    if not cache_warm.due():
        return
    db = get_session_factory(app_engine())()
    try:
        cache_warm.warm(db, student_id)
    finally:
        db.close()


def _claim_turn(student_id: int) -> Optional[str]:
    """Register a turn for the student; None if one is already running."""
    now = time.monotonic()
    held = _INFLIGHT.get(student_id)
    if held is not None and now - held[0] < INFLIGHT_STALE_SECONDS:
        return None
    token = uuid.uuid4().hex
    _INFLIGHT[student_id] = (now, token)
    return token


def _release_turn(student_id: int, token: str) -> None:
    held = _INFLIGHT.get(student_id)
    if held is not None and held[1] == token:
        del _INFLIGHT[student_id]


async def run_turn(
    student: Student,
    db: DBSession,
    user_message: str,
    lecture_num: Optional[int] = None,
    slide_num: Optional[int] = None,
    course: str = DEFAULT_COURSE,
    turn_id: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Run one supervised turn (see :func:`_run_turn_body`), one at a time
    per student: a turn that starts while another is running yields a single
    ``{"type": "busy", "message": ...}`` event and does nothing else.

    ``turn_id`` is the id the caller already told the client (the chat
    stream's resume handle); it names the turn in the activity stream, on
    its ``turn_events`` row, and in the browser's activity events
    (step 1.2). A caller with no
    client gets a fresh one."""
    # Read the id once: in the finally below, a failed turn can leave the
    # session unusable, and reading student.id there would try a reload,
    # raise, and leak the claim (the student would read "busy" until the
    # claim went stale).
    sid = student.id
    token = _claim_turn(sid)
    if token is None:
        yield {"type": "busy", "message": BUSY_MESSAGE}
        return
    # Every database step of the turn commits on its own (db_step), so the
    # turn's objects stay loaded across commits: otherwise the next attribute
    # read on the event loop would be a blocking reload that also reopens a
    # transaction and holds a pooled connection through the next LLM call.
    expire_on_commit = db.expire_on_commit
    db.expire_on_commit = False
    body = _run_turn_body(student, db, user_message, lecture_num, slide_num, course, turn_id)
    try:
        async for event in body:
            yield event
    finally:
        try:
            await body.aclose()
        finally:
            db.expire_on_commit = expire_on_commit
            _release_turn(sid, token)


async def _run_turn_body(
    student: Student,
    db: DBSession,
    user_message: str,
    lecture_num: Optional[int] = None,
    slide_num: Optional[int] = None,
    course: str = DEFAULT_COURSE,
    turn_id: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Run one supervised turn; persist messages and yield event dicts.

    Each step is a stage in :mod:`tutor.agent.turn_stages`; a stage that
    yields "done" has ended the turn.
    0. Spend caps (per-student daily, global monthly): a friendly notice, no agent.
    1. A pending mastery exam intercepts the message (exam_intercept):
       ANSWER grades (fail-closed; a pass replays the original gated
       question), CLARIFY rephrases wording without consuming the attempt,
       DEFER pauses the exam and falls through to the normal flow.
    2. Context (context), and the local no-key path (cli_turn).
    3. Preflight (preflight): classify (conduct short-circuits here), the
       envelope, the prerequisite gate (gated content starts a mastery exam
       instead), and the strategist's move.
    4. The actor (actor_loop): one streaming call with a bounded native tool
       loop; a retrieved slide can widen an empty scope but never fires a gate.
    5. Supervision (supervision): detector always, judge on risky turns.
    6. Persistence and telemetry (persist), then "done", then the summary.
    """
    req = TurnRequest(student=student, user_message=user_message, lecture_num=lecture_num,
                      slide_num=slide_num, course=course)
    if turn_id:
        req.turn_id = turn_id
    # A turn_start with no matching terminal event (chat_turn, gate,
    # slow_turn) is a hang ("thinking forever"); turn_start is verbose-tier
    # so it is off by default.
    logging_config.activity("turn_start", level="verbose", turn_id=req.turn_id,
                            student_id=req.sid, course=course, lecture_num=lecture_num)

    # 0. Spend caps: when hit, stream a friendly in-voice notice instead of
    #    running the agent; the turn is not recorded as an exchange, and the
    #    question is not saved. ``limited`` tells the page so: it puts the
    #    question back in the box and gives the notice no speaker or thumbs
    #    (R21). A page from before the
    #    flag shows the notice as it always did.
    limit_msg = await db_step(db, usage_mod.over_caps, db, req.sid)
    if limit_msg:
        yield {"type": "token", "text": limit_msg}
        yield {"type": "done", "full_text": limit_msg, "limited": True}
        return

    # The student's message is committed first, like every database step of
    # the turn (plan steps 2.5 and 2.6).
    req.gate_trigger_id = await persist.save_user_message(db, req)

    # 1. Active exam intercept.
    finished = False
    async for event in exam_intercept.handle_active_exam(db, req):
        finished = finished or event["type"] == "done"
        yield event
    if finished:
        return

    # 2. Context, and the local no-key path.
    per_request_ctx, messages = await context.read(db, req)
    if get_config()["use_claude_cli"]:
        async for event in cli_turn.run(db, req, per_request_ctx, messages):
            yield event
        return

    # 3. Preflight. The classifier and the judge never touch the session:
    #    they report their usage here and the turn writes the ledger rows
    #    (plan step 2.6, A19).
    supervisor_calls: list = []
    classification, strategist_facts = await preflight.classify(db, req, supervisor_calls)
    conduct = await preflight.conduct_reply(db, req, classification)
    if conduct:
        conduct_msg, conduct_id = conduct
        yield {"type": "token", "text": conduct_msg}
        yield {"type": "done", "full_text": conduct_msg, "message_id": conduct_id}
        return
    code_adjacent = preflight.is_code_adjacent(classification)
    bundle = await preflight.assemble_envelope(db, req, classification, code_adjacent)
    gate_events = await preflight.start_gate(db, req, bundle)
    if gate_events:
        for event in gate_events:
            yield event
        return
    await preflight.consult_strategist(db, req, bundle, classification, strategist_facts)

    # Stage 1 persistence (plan step 2.5, A13): the provisional chat event is
    # committed before the actor runs, so the turn is on the ledger and the
    # daily cap even if a later step fails.
    provisional_event = await persist.record_provisional_event(db, req, bundle, classification)
    req.mark("preflight_done")

    # 4. The actor.
    run = actor_loop.prepare(bundle, classification, code_adjacent, per_request_ctx, messages)
    async for event in actor_loop.run_rounds(db, req, run):
        yield event

    # 5. Supervision.
    reply = supervision.Reply()
    async for event in supervision.supervise(db, req, run, reply, supervisor_calls):
        yield event
    for event in supervision.settle(run, reply):
        yield event

    # 6. Stage 2 persistence: the reply and its ledger rows first, then the
    #    learning state on its own (a failure there is logged, not raised).
    reply_id = await persist.save_reply(db, req, run, reply, provisional_event)
    await persist.record_learning(db, req, run, reply)
    persist.log_turn(req, run, reply)
    # "done" goes out only after the reply is durable: a consumer that stops
    # reading at done (the JSON API, a closed tab) can no longer drop the turn,
    # its telemetry, or its cost, and the reply row exists before the client
    # can press speak or thumbs on it (A1).
    # The reply's row id rides along so the browser's events can name it.
    yield {"type": "done", "full_text": reply.full_text, "message_id": reply_id}
    # Fold older messages into the rolling summary (fail-soft, S16).
    await persist.summarize(db, req)
