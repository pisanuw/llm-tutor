"""Path layer: track picker, next-goals card, PathEvent telemetry (Session 15, S25)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from tutor.templating import templates
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from tutor.agent.navigator import current_course, default_track_id, get_student_track, next_goals
from tutor.agent.state import (
    PATH_MODALITIES, PATH_SOURCES, ensure_student_path, record_path_event, set_student_path,
)
from tutor.auth.deps import get_current_student, get_db
from tutor.nav import nav_context
from tutor.courses import COURSES, DEFAULT_COURSE, require_course
from tutor.routers.params import int_query
from tutor.db.models import LearningGoal, StudentProgress, Track, Student

router = APIRouter(tags=["path"])



def _resolve_track(student_id: int, course: str, db: DBSession) -> str:
    """Return the student's active track for a course, or the course default."""
    return get_student_track(student_id, course, db) or default_track_id(course)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/path/next")
def path_next(
    course: str = DEFAULT_COURSE,
    n: int = Depends(int_query("n", 3)),
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Return the student's top-n recommended next goals in their current track.

    Response: {track_id, goals: [{id, title, hook, slide_anchor, book_anchor, status}]}
    """
    course = require_course(course)
    n = min(n, 50)
    track_id = _resolve_track(student.id, course, db)
    goal_ids = next_goals(student.id, course, db, track_id=track_id, n=n)

    progress_map: dict[str, str] = {}
    if goal_ids:
        rows = (
            db.query(StudentProgress.goal_id, StudentProgress.status)
            .filter(
                StudentProgress.student_id == student.id,
                StudentProgress.goal_id.in_(goal_ids),
            )
            .all()
        )
        progress_map = {r[0]: r[1] for r in rows}

    goals_out = []
    for gid in goal_ids:
        g = db.get(LearningGoal, gid)
        if g is None:
            continue
        goals_out.append({
            "id": g.id,
            "title": g.title,
            "hook": g.hook or "",
            "lecture": g.lecture_num,
            "course": g.course,
            "slide_anchor": g.slide_anchor_data(),
            "book_anchor": g.book_anchor_data(),
            "status": progress_map.get(gid, "locked"),
        })

    return JSONResponse({"track_id": track_id, "goals": goals_out})


@router.get("/path/current")
def path_current(
    course: str = DEFAULT_COURSE,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Return the student's current track for a course (id + display title) so
    the chat header can show which track they're on."""
    course = require_course(course)
    track_id = _resolve_track(student.id, course, db)
    track = db.get(Track, track_id)
    return JSONResponse(
        {"track_id": track_id, "title": track.title if track else track_id}
    )


class TrackSetRequest(BaseModel):
    course: str
    track_id: str


@router.post("/path/track")
def path_set_track(
    body: TrackSetRequest,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Set the student's active track for a course and record a PathEvent."""
    require_course(body.course)
    track = db.get(Track, body.track_id)
    if track is None or track.course != body.course:
        raise HTTPException(status_code=404, detail="Track not found")

    set_student_path(db, student.id, body.course, body.track_id)
    record_path_event(
        db,
        student_id=student.id,
        course=body.course,
        modality="track_set",
        goal_id=None,
        source="student",
    )
    db.commit()
    return {"ok": True, "track_id": body.track_id}


class CourseSetRequest(BaseModel):
    course: str


@router.post("/path/course")
def path_set_course(
    body: CourseSetRequest,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Remember the course the student is working in (header course switch).

    Creates the course's default-track path on first use, otherwise touches
    the existing one; navigator.current_course reads the most recent path, so
    the next plain /chat visit opens on this course.
    """
    require_course(body.course)
    row, created = ensure_student_path(
        db, student.id, body.course, default_track_id(body.course)
    )
    db.commit()
    return {"ok": True, "course": body.course, "track_id": row.track_id, "created": created}


class PathEventRequest(BaseModel):
    course: str
    goal_id: Optional[str] = None
    modality: str
    source: str = "student"


@router.post("/path/event")
def path_record_event(
    body: PathEventRequest,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    """Record a PathEvent (goal chosen + modality selected by the student)."""
    require_course(body.course)
    if body.modality not in PATH_MODALITIES:
        raise HTTPException(status_code=422, detail=f"Unknown modality: {body.modality[:32]!r}")
    if body.source not in PATH_SOURCES:
        raise HTTPException(status_code=422, detail=f"Unknown source: {body.source[:32]!r}")
    # An empty id is no goal: stored as "" it broke the goals foreign key on
    # Postgres, a 500 (the Stage 4 review).
    goal_id = body.goal_id or None
    if goal_id:
        g = db.get(LearningGoal, goal_id)
        if g is None:
            raise HTTPException(status_code=404, detail="Goal not found")

    record_path_event(
        db,
        student_id=student.id,
        course=body.course,
        modality=body.modality,
        goal_id=goal_id,
        source=body.source,
    )
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Track picker HTML page
# ---------------------------------------------------------------------------

@router.get("/track-picker", response_class=HTMLResponse)
def track_picker_page(
    request: Request,
    course: Optional[str] = None,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):

    # Default to the course the student is working in, not always the first one.
    if course not in COURSES:
        course = current_course(student.id, db) or DEFAULT_COURSE
    current_track = _resolve_track(student.id, course, db)
    tracks = (
        db.query(Track)
        .filter_by(course=course)
        .order_by(Track.id)
        .all()
    )
    tracks_data = [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "goal_count": len(t.goal_ids()),
            "active": t.id == current_track,
        }
        for t in tracks
    ]

    return templates.TemplateResponse(
        request,
        "track_picker.html",
        {
            **nav_context(student, db, course),
            "tracks": tracks_data,
            "current_track": current_track,
        },
    )
