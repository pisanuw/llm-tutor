"""Learning goals map page."""

from __future__ import annotations

from typing import Optional


from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from tutor.templating import templates
from sqlalchemy.orm import Session as DBSession

from tutor.agent.explorables import explorable_url
from tutor.agent.progress import get_student_progress
from tutor.nav import nav_context
from tutor.auth.deps import get_current_student, get_db
from tutor.db.models import GoalPrerequisite, LearningGoal, Student, StudentProgress

router = APIRouter(tags=["map"])


def _slides_url(goal) -> Optional[str]:
    from tutor import decks

    anchor = goal.slide_anchor_data() or {}
    try:
        lecture = int(anchor.get("lecture", goal.lecture_num))
    except (TypeError, ValueError):
        lecture = goal.lecture_num
    return decks.deck_url(goal.course, lecture)


@router.get("/map", response_class=HTMLResponse)
def map_page(
    request: Request,
    student: Student = Depends(get_current_student),
    db: DBSession = Depends(get_db),
):
    goals = db.query(LearningGoal).order_by(LearningGoal.course, LearningGoal.lecture_num).all()
    edges = db.query(GoalPrerequisite).all()
    progress = get_student_progress(student.id, db)

    # Build seen set: goals the student has encountered in chat (seen=True but not mastered/unlocked)
    seen_rows = (
        db.query(StudentProgress.goal_id)
        .filter_by(student_id=student.id, seen=True)
        .all()
    )
    seen_ids: set[str] = {r.goal_id for r in seen_rows}

    def _display_status(goal_id: str) -> str:
        s = progress.get(goal_id, "locked")
        if s in ("mastered", "unlocked"):
            return s
        if goal_id in seen_ids:
            return "seen"
        return "locked"

    nodes = [
        {
            "id": g.id,
            "title": g.title,
            "description": g.description,
            "course": g.course,
            "lecture": g.lecture_num,
            "status": _display_status(g.id),
            "slide_anchor": g.slide_anchor_data(),
            # The deck's URL only when it is on disk (review step 0.3, F3).
            "slides_url": _slides_url(g),
            "book_anchor": g.book_anchor_data(),
            "hook": g.hook or None,
            "explorable_url": explorable_url(g.id) if g.threshold else None,
        }
        for g in goals
    ]
    links = [{"source": e.from_goal_id, "target": e.to_goal_id} for e in edges]

    unlocked = mastered = seen = 0
    for n in nodes:
        if n["status"] == "unlocked":
            unlocked += 1
        elif n["status"] == "mastered":
            mastered += 1
        elif n["status"] == "seen":
            seen += 1

    return templates.TemplateResponse(
        request,
        "map.html",
        {
            **nav_context(student, db),
            # |tojson escapes <, >, &, and ' (plan step 5.4, finding C24).
            "graph": {"nodes": nodes, "links": links},
            "total_goals": len(nodes),
            "unlocked": unlocked,
            "mastered": mastered,
            "seen": seen,
        },
    )
