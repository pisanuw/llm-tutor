"""Spaced-review loop (Engram integration, Phase 1).

A dedicated, interleaved, cross-topic free-recall session — the retrieval-practice
habit loop the tutor lacked (Engram integration report, section 5). Goals
whose FSRS due date has arrived are surfaced as short free-recall probes drawn
from the existing mastery-exam question bank; each answer is graded by the same
fail-closed, DATA-framed grader as the exams, mapped to an FSRS rating, and fed
back through the single-writer state module (``record_review_rating``) to advance
the schedule.

Everything the endpoints need is computed by the pure helpers here (due queue,
interleaving, session sizing, amnesty, minute estimate) so the scheduling logic
is unit-testable without HTTP. Writes go through ``state`` only (P3); the grade
endpoint re-derives the probe rubric server-side from the bank (never trusts a
client-supplied rubric), consistent with the "trusted state only" discipline.
"""

from __future__ import annotations

import random
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from tutor.templating import templates
from sqlalchemy.orm import Session as DBSession

from tutor.agent import mastery as mastery_mod
from tutor.agent import spaced_review, state
from tutor.auth.deps import get_current_student, get_db
from tutor.courses import DEFAULT_COURSE, require_course
from tutor.db.models import LearningGoal, MasteryAttempt, Student, StudentProgress
from tutor.utctime import to_local

router = APIRouter(tags=["review"])

# Session sizing. Mirrors Engram's Sprint/Standard/Deep framing (§5); values are
# item caps, and the minute estimate is derived so "3 items ≈ 2 minutes".
SESSION_CAPS: dict[str, int] = {"sprint": 7, "standard": 20, "deep": 40}
DEFAULT_MODE = "standard"
MINUTES_PER_ITEM = 0.7

# Pre-reveal confidence bands (Engram P2): Certain / Pretty sure / Half unsure /
# Guessing. Anything else (incl. a skip) stores NULL — confidence is never inferred.
VALID_CONFIDENCE = frozenset({90, 70, 50, 25})
# At or above this band, a wrong answer is a miscalibration worth flagging.
HIGH_CONFIDENCE = 70


def _clean_confidence(raw) -> Optional[int]:
    """Return a valid confidence band or None (never inferred)."""
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val if val in VALID_CONFIDENCE else None


# ---------------------------------------------------------------------------
# Pure-ish scheduling core (reads the DB, no writes, no LLM)
# ---------------------------------------------------------------------------

def _minutes_estimate(count: int) -> int:
    """Whole-minute estimate for a session of ``count`` items (min 1 if any)."""
    if count <= 0:
        return 0
    return max(1, round(count * MINUTES_PER_ITEM))


def due_rows(student_id: int, course: str, db: DBSession, now=None) -> list[StudentProgress]:
    """StudentProgress rows for ``course`` whose FSRS due date has arrived.

    Only goals that have a probe available in the exam bank are returned (a due
    item with no question can't be practised). Ordered by due date, oldest first.
    """
    rows = state.review_due_rows(db, student_id, course, now)   # the one review clock (B20)
    return [r for r in rows if mastery_mod._has_exam_questions(r.goal_id)]


def _interleave_by_lecture(rows: list[StudentProgress], titles: dict[str, tuple[str, int]]) -> list[StudentProgress]:
    """Reorder due rows so consecutive items avoid the same lecture where possible.

    Interleaving across topics is the whole point of a review session (Engram §5);
    a due-date sort alone would clump a lecture together. Stable within a lecture
    (keeps the oldest-due order), round-robin across lectures.
    """
    buckets: dict[int, list[StudentProgress]] = {}
    order: list[int] = []
    for r in rows:
        lec = titles.get(r.goal_id, ("", 0))[1]
        if lec not in buckets:
            buckets[lec] = []
            order.append(lec)
        buckets[lec].append(r)
    out: list[StudentProgress] = []
    while any(buckets[lec] for lec in order):
        for lec in order:
            if buckets[lec]:
                out.append(buckets[lec].pop(0))
    return out


def build_session(
    student_id: int,
    course: str,
    db: DBSession,
    mode: str = DEFAULT_MODE,
    now=None,
) -> dict:
    """Assemble one review session: due count, capped/interleaved items, amnesty.

    Amnesty (Engram §5): when the backlog exceeds the session cap, we surface a
    calm capped catch-up rather than dumping every due item — no guilt framing.
    Each item carries one probe sampled from the goal's bank (probe ``id`` only;
    the grade endpoint re-derives the rubric server-side).
    """
    cap = SESSION_CAPS.get(mode, SESSION_CAPS[DEFAULT_MODE])
    rows = due_rows(student_id, course, db, now=now)
    due_count = len(rows)

    # Titles + lecture for interleaving and display.
    ids = [r.goal_id for r in rows]
    titles: dict[str, tuple[str, int]] = {}
    if ids:
        for g in db.query(LearningGoal).filter(LearningGoal.id.in_(ids)).all():
            titles[g.id] = (g.title, g.lecture_num)

    ordered = _interleave_by_lecture(rows, titles)[:cap]

    items: list[dict] = []
    for idx, r in enumerate(ordered):
        pool = mastery_mod._question_pool(r.goal_id)
        if not pool:
            continue
        # Odd items on goals with approved distractors become error-detection
        # probes (misconception library, pedagogy item 12); the rest sample the bank.
        probe = probe_for_item(r.goal_id, idx, db, lambda gid: random.choice(pool))
        if probe is None:
            continue
        title, lec = titles.get(r.goal_id, (r.goal_id, 0))
        items.append({
            "goal_id": r.goal_id,
            "title": title,
            "lecture_num": lec,
            "probe_id": probe["id"],
            "question": probe["question"],
        })

    amnesty = due_count > cap
    return {
        "due_count": due_count,
        "shown": len(items),
        "amnesty": amnesty,
        "minutes": _minutes_estimate(len(items)),
        "mode": mode if mode in SESSION_CAPS else DEFAULT_MODE,
        "items": items,
    }


def nudge(student_id: int, course: str, db: DBSession, now=None) -> dict:
    """Session-start due-count nudge. Silent (message empty) when nothing is due."""
    count = len(due_rows(student_id, course, db, now=now))
    if count == 0:
        return {"due_count": 0, "minutes": 0, "message": ""}
    minutes = _minutes_estimate(min(count, SESSION_CAPS[DEFAULT_MODE]))
    return {
        "due_count": count,
        "minutes": minutes,
        "message": f"{count} item{'s' if count != 1 else ''} due for review, about {minutes} minute{'s' if minutes != 1 else ''}.",
    }


MISCONCEPTION_PROBE_PREFIX = "mc-"


def _misconception_probe(row) -> dict:
    """An error-detection probe built from an approved library entry
    (pedagogy item 12 follow-up): the student is shown the distractor and
    asked what is wrong with it. Rubric stays server-side like every probe."""
    return {
        "id": f"{MISCONCEPTION_PROBE_PREFIX}{row.id}",
        "question": (
            "A common mistake on this topic is: "
            f"\u201c{row.distractor}\u201d. What is wrong with it, and what is the correct idea?"
        ),
        "rubric": (
            f"Full credit: identifies the flaw (the underlying misconception is: {row.description}) "
            "and states the correct idea. Partial: spots that it is wrong without saying why."
        ),
    }


def _approved_distractor_rows(goal_id: str, db: DBSession) -> list:
    from tutor.db.models import GoalMisconception
    return (
        db.query(GoalMisconception)
        .filter(GoalMisconception.goal_id == goal_id,
                GoalMisconception.status == "approved",
                GoalMisconception.distractor != "")
        .order_by(GoalMisconception.evidence_count.desc(), GoalMisconception.id.asc())
        .all()
    )


def probe_for_item(goal_id: str, index: int, db: DBSession, sample_bank) -> Optional[dict]:
    """Pick this item's probe. Odd-indexed items on a goal with an approved
    distractor get an error-detection probe (rotating through the library);
    everything else samples the bank. Deterministic, so a session is stable."""
    if index % 2 == 1:
        rows = _approved_distractor_rows(goal_id, db)
        if rows:
            return _misconception_probe(rows[(index // 2) % len(rows)])
    return sample_bank(goal_id)


def _lookup_probe(goal_id: str, probe_id: str, db: Optional[DBSession] = None) -> Optional[dict]:
    """Re-derive a probe (question + rubric) by id. Server-side only. Bank probes
    come from the mastery pool; misconception probes (mc-<row id>) from the
    approved library, so a rejected/reverted row can no longer be graded."""
    if probe_id.startswith(MISCONCEPTION_PROBE_PREFIX) and db is not None:
        from tutor.db.models import GoalMisconception
        from tutor.routers.params import bounded_int

        # mc-<id past 2^31> was "integer out of range" on Postgres (R4).
        rid = bounded_int(probe_id[len(MISCONCEPTION_PROBE_PREFIX):])
        if rid is None:
            return None
        row = db.get(GoalMisconception, rid)
        if row is None or row.goal_id != goal_id or row.status != "approved" or not row.distractor:
            return None
        return _misconception_probe(row)
    for q in mastery_mod._question_pool(goal_id):
        if q["id"] == probe_id:
            return q
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    course: Optional[str] = None,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    from tutor.nav import nav_context
    from tutor.routers.chat import resolve_course

    # As /chat does: a course that is not one opens the student's current
    # course, not the 422 error page (R15).
    course = resolve_course(course, student.id, db)

    return templates.TemplateResponse(request, "review.html", nav_context(student, db, course))


@router.get("/review/session")
def review_session(
    course: str = DEFAULT_COURSE,
    mode: str = DEFAULT_MODE,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    return JSONResponse(build_session(student.id, require_course(course), db, mode=mode))


@router.get("/review/nudge")
def review_nudge(
    course: str = DEFAULT_COURSE,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    return JSONResponse(nudge(student.id, require_course(course), db))


def _checked_lately(db: DBSession, student_id: int, goal_id: str, minutes: int = 60) -> bool:
    """A graded review of this goal in the last ``minutes``."""
    import datetime
    import json

    from tutor.db.models import TurnEvent
    from tutor.utctime import utcnow

    rows = (
        db.query(TurnEvent.goal_ids_json, TurnEvent.verdicts_json)
        .filter(TurnEvent.student_id == student_id, TurnEvent.kind == "review",
                TurnEvent.ts >= utcnow() - datetime.timedelta(minutes=minutes))
        .order_by(TurnEvent.id.desc())
        .limit(50)
        .all()
    )
    for goals, verdicts in rows:
        try:
            if goal_id in (json.loads(goals or "[]") or []) and "score" in (json.loads(verdicts or "{}") or {}):
                return True
        except (ValueError, TypeError):
            continue
    return False


@router.post("/review/grade")
def review_grade(
    goal_id: str = Form(...),
    probe_id: str = Form(...),
    answer: str = Form(...),
    confidence: Optional[str] = Form(None),        # a band, or anything else as none
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Grade one free-recall answer, advance the FSRS schedule, return the verdict.

    Fail closed: a grader error leaves the item's schedule untouched (no rating
    recorded) and asks the student to retry, mirroring the exam grader. All state
    writes go through state.record_review_rating (single writer), which records
    evidence but never moves the goal's status. The pre-reveal ``confidence``
    band (Engram P2), collected before this call, is stored on the graded turn
    event for calibration reporting; a high-confidence miss is flagged.

    Guards (plan step 1.6, findings B2 and C3): the goal must be due for this
    student. Every item a session serves is due, and a grade moves the due date
    at least a day out, so an answer cannot be graded twice or on a goal the
    student has not reached. A goal under a pending mastery exam is refused (its
    questions come from the same bank). The daily review cap and the monthly
    spend cap apply, and the grader's tokens and cost go on the review TurnEvent.

    A plain ``def``: FastAPI runs it in the threadpool, so its database work
    and the blocking grader call stay off the event loop (plan step 2.6, C1).
    """
    # One grade at a time per student and goal: a retry while the first is
    # still with the grader (a dropped connection re-enables Check) graded
    # the answer twice, billed twice and moved the schedule twice (the Stage
    # 3 review). In process memory, like turn._INFLIGHT (one instance).
    key = (student.id, goal_id)
    with _GRADING_LOCK:
        if key in _GRADING:
            return JSONResponse(
                {"error": "in_progress",
                 "feedback": "Your answer to this one is still being checked. Give it a moment, then try again."},
                status_code=409,
            )
        _GRADING.add(key)
    try:
        return _grade_answer(goal_id, probe_id, answer, confidence, student, db)
    finally:
        with _GRADING_LOCK:
            _GRADING.discard(key)


_GRADING: set[tuple[int, str]] = set()
_GRADING_LOCK = threading.Lock()


def _grade_answer(goal_id: str, probe_id: str, answer: str, confidence: Optional[str],
                  student: Student, db: DBSession) -> JSONResponse:
    from tutor.agent import usage as usage_mod

    conf = _clean_confidence(confidence)
    answer = answer.replace("\x00", "")          # Postgres cannot store NUL (R8)

    goal = db.get(LearningGoal, goal_id)
    probe = _lookup_probe(goal_id, probe_id, db)
    if goal is None or probe is None:
        return JSONResponse(
            {"error": "unknown probe",
             "feedback": "This question changed since the page loaded. Skip it, or reload the page for the current one."},
            status_code=404)

    progress = (
        db.query(StudentProgress)
        .filter_by(student_id=student.id, goal_id=goal_id)
        .first()
    )
    if progress is None or not state.review_is_due(progress.fsrs_due):
        if progress is not None and _checked_lately(db, student.id, goal_id):
            # The grade went through but its answer never reached the page
            # (the signal dropped): the retry used to say "not due", which
            # read as an error (R27).
            when = to_local(progress.fsrs_due).date().isoformat() if progress.fsrs_due else None
            return JSONResponse(
                {"error": "already_checked", "next_due": when,
                 "feedback": "Your answer to this one was already checked"
                             + (f"; it comes back for review on {when}." if when else ".")},
                status_code=409,
            )
        return JSONResponse(
            {"error": "not_due", "feedback": "That item is not due for review right now."},
            status_code=409,
        )
    if (
        db.query(MasteryAttempt)
        .filter_by(student_id=student.id, goal_id=goal_id, status="pending")
        .first()
        is not None
    ):
        return JSONResponse(
            {"error": "exam_pending",
             "feedback": "This topic has a mastery check waiting in the chat. Finish that first."},
            status_code=409,
        )
    limit_message = usage_mod.over_review_caps(db, student.id)
    if limit_message:
        db.commit()                      # a quota hit is a ledger row (tutor/quotas.py)
        return JSONResponse({"error": "limit", "feedback": limit_message}, status_code=429)

    def _cost_fields(counts: dict) -> dict:
        return usage_mod.cost_fields(mastery_mod._GRADER_MODEL, counts)

    from tutor.agent.misconception_mining import approved_for_goals
    known = tuple(d for _, d in approved_for_goals(db, {goal_id}, limit=3))
    # The grader can take half a minute: the connection goes back to the pool
    # first (R1). The goal keeps the title and description the grader reads
    # once detached.
    db.expunge(goal)
    db.commit()
    try:
        result = mastery_mod.grade_probe(
            goal, probe["question"], probe["rubric"], answer, None, known,
        )
    except mastery_mod.GradingUnavailable as exc:
        # Fail closed: nothing is scheduled; the student can resend. A call
        # that ran but returned unusable output still costs: ledger it.
        state.record_turn_event(
            db,
            student_id=student.id,
            kind="review",
            goal_ids=[goal_id],
            verdicts={"grading_error": str(exc)[:200]},
            **_cost_fields(exc.usage),
        )
        db.commit()
        return JSONResponse(
            {"error": "grading_unavailable", "feedback": "My grading hiccupped — send that answer once more."},
            status_code=503,
        )

    score = int(result["score"])
    rating = spaced_review.rating_from_exam_score(score)
    state.record_review_rating(db, student.id, goal_id, rating, source="review")
    row = (
        db.query(StudentProgress)
        .filter_by(student_id=student.id, goal_id=goal_id)
        .first()
    )
    # The local date: the UTC one named the next day for an evening review.
    next_due = to_local(row.fsrs_due).date().isoformat() if row and row.fsrs_due else None

    # High-confidence miss (Engram P2): confident but wrong is a misconception
    # signal, not a knowledge gap — recorded on the turn event so the calibration
    # report can surface it.
    miscalibrated = conf is not None and conf >= HIGH_CONFIDENCE and score < 2

    # Misconception log (Engram P3): a confident-but-wrong answer logs a durable
    # misconception the tutor can address later; a correct answer resolves any
    # open one for this goal. Single writer (P3).
    if miscalibrated:
        state.record_misconception(db, student.id, goal_id, result.get("feedback", ""))
    elif score >= 2:
        state.resolve_misconceptions(db, student.id, goal_id)

    state.record_turn_event(
        db,
        student_id=student.id,
        kind="review",
        goal_ids=[goal_id],
        verdicts={"score": score, "rating": rating.name, "miscalibrated": miscalibrated},
        confidence=conf,
        **_cost_fields(result.get("usage") or {}),
    )
    db.commit()

    return JSONResponse({
        "score": score,
        "rating": rating.name,
        "feedback": result.get("feedback", ""),
        "next_due": next_due,
        "confidence": conf,
        "miscalibrated": miscalibrated,
        "mastery": round(row.mastery_score, 3) if row and row.mastery_score is not None else None,
    })
