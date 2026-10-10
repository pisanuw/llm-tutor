"""Trajectory state: what the recent ledger says about how learning is going.

Pure in the policy-core sense (P2/P5): zero LLM calls, zero student prose.
Everything here is computed from trusted rows the student cannot write
directly — TurnEvents (kind, classified intent, rung, exam verdicts) and
GradedEvents (exam/exercise outcomes). The output is a frozen dataclass and
is the pedagogy strategist's ONLY window into history (Session 9): the
strategist never reads the transcript, it reads this.

Signals per goal, over the session window:
- turns spent and the rungs used (is the student needing more help or less?)
- the student's move mix: attempts vs questions vs passive turns
- repeated_error: several turns on the same goal with no graded success and
  no easing of the help ceiling (the "stuck" signal)
- minutes since the student last attempted anything (any goal)
- exam passes/fails and recent graded scores
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor.db.models import GradedEvent, TurnEvent
from tutor.utctime import utcnow

# The "session" is a deterministic sliding window, not a login session:
# recent enough to describe the current sitting, long enough to span a break.
SESSION_WINDOW_HOURS = 6
MAX_EVENTS = 100

# A goal is flagged repeated_error after this many turns with no graded
# success and no drop in the help ceiling.
STUCK_TURNS = 3
SUCCESS_BAR = 0.75

# Intents counted as each move kind (anything else is passive presence).
_ATTEMPT_INTENTS = {"ATTEMPT"}
_QUESTION_INTENTS = {"QUESTION"}


@dataclass(frozen=True)
class GoalTrajectory:
    goal_id: str
    turns: int                        # chat turns touching this goal in window
    rungs_used: tuple[int, ...]       # rung ceiling of those turns, oldest first
    attempts: int
    questions: int
    passive: int
    repeated_error: bool              # stuck: turns, no success, no rung relief
    exam_passes: int
    exam_fails: int
    graded_scores: tuple[float, ...]  # GradedEvent successes in window, oldest first


@dataclass(frozen=True)
class Trajectory:
    session_turns: int                # chat turns in the window (all goals)
    attempts: int
    questions: int
    passive: int
    minutes_since_last_attempt: Optional[float]   # None: never attempted
    goals: tuple[GoalTrajectory, ...]

    def get(self, goal_id: str) -> Optional[GoalTrajectory]:
        for g in self.goals:
            if g.goal_id == goal_id:
                return g
        return None


def _goal_ids(event: TurnEvent) -> list[str]:
    if not event.goal_ids_json:
        return []
    try:
        ids = json.loads(event.goal_ids_json)
        return [g for g in ids if isinstance(g, str)] if isinstance(ids, list) else []
    except Exception:
        return []


def _exam_passed(event: TurnEvent) -> Optional[bool]:
    if not event.verdicts_json:
        return None
    try:
        v = json.loads(event.verdicts_json)
        return bool(v["passed"]) if isinstance(v, dict) and "passed" in v else None
    except Exception:
        return None


def compute_trajectory(
    db: DBSession,
    student_id: int,
    now: Optional[datetime.datetime] = None,
) -> Trajectory:
    """Compute the trajectory from the ledger. Reads only trusted rows."""
    now = now or utcnow()
    window_start = now - datetime.timedelta(hours=SESSION_WINDOW_HOURS)

    events = (
        db.query(TurnEvent)
        .filter(TurnEvent.student_id == student_id, TurnEvent.ts >= window_start)
        .order_by(TurnEvent.ts.desc(), TurnEvent.id.desc())
        .limit(MAX_EVENTS)
        .all()
    )
    events.reverse()   # oldest first

    graded = (
        db.query(GradedEvent)
        .filter(GradedEvent.student_id == student_id, GradedEvent.ts >= window_start)
        .order_by(GradedEvent.ts.asc(), GradedEvent.id.asc())
        .all()
    )
    scores_by_goal: dict[str, list[float]] = {}
    for g in graded:
        scores_by_goal.setdefault(g.goal_id, []).append(float(g.success))

    session_turns = attempts = questions = passive = 0
    per_goal_turns: dict[str, int] = {}
    per_goal_rungs: dict[str, list[int]] = {}
    per_goal_moves: dict[str, dict[str, int]] = {}
    per_goal_exams: dict[str, dict[str, int]] = {}
    goal_order: list[str] = []

    for ev in events:
        if ev.kind == "exam":
            passed = _exam_passed(ev)
            if passed is None:
                continue
            for gid in _goal_ids(ev):
                d = per_goal_exams.setdefault(gid, {"pass": 0, "fail": 0})
                d["pass" if passed else "fail"] += 1
                if gid not in goal_order:
                    goal_order.append(gid)
            continue
        if ev.kind != "chat":
            continue
        session_turns += 1
        if ev.intent in _ATTEMPT_INTENTS:
            bucket = "attempts"
        elif ev.intent in _QUESTION_INTENTS:
            bucket = "questions"
        else:
            bucket = "passive"
        if bucket == "attempts":
            attempts += 1
        elif bucket == "questions":
            questions += 1
        else:
            passive += 1
        for gid in _goal_ids(ev):
            per_goal_turns[gid] = per_goal_turns.get(gid, 0) + 1
            if ev.rung_used is not None:
                per_goal_rungs.setdefault(gid, []).append(int(ev.rung_used))
            per_goal_moves.setdefault(
                gid, {"attempts": 0, "questions": 0, "passive": 0}
            )[bucket] += 1
            if gid not in goal_order:
                goal_order.append(gid)

    # Minutes since the last ATTEMPT-intent chat turn (any age, not just the
    # window: "has this student ever tried?" matters to the strategist).
    last_attempt = (
        db.query(TurnEvent)
        .filter(
            TurnEvent.student_id == student_id,
            TurnEvent.kind == "chat",
            TurnEvent.intent.in_(sorted(_ATTEMPT_INTENTS)),
        )
        .order_by(TurnEvent.ts.desc(), TurnEvent.id.desc())
        .first()
    )
    minutes_since_attempt = (
        (now - last_attempt.ts).total_seconds() / 60.0 if last_attempt else None
    )

    goals: list[GoalTrajectory] = []
    for gid in goal_order:
        rungs = tuple(per_goal_rungs.get(gid, ()))
        moves = per_goal_moves.get(gid, {"attempts": 0, "questions": 0, "passive": 0})
        exams = per_goal_exams.get(gid, {"pass": 0, "fail": 0})
        scores = tuple(scores_by_goal.get(gid, ()))
        turns = per_goal_turns.get(gid, 0)

        no_success = not any(s >= SUCCESS_BAR for s in scores) and exams["pass"] == 0
        # Rung relief = the ceiling came DOWN over the window (the student
        # needed less help); without it, repeated turns mean stuck.
        no_relief = len(rungs) < 2 or rungs[-1] >= rungs[0]
        repeated_error = turns >= STUCK_TURNS and no_success and no_relief

        goals.append(GoalTrajectory(
            goal_id=gid,
            turns=turns,
            rungs_used=rungs,
            attempts=moves["attempts"],
            questions=moves["questions"],
            passive=moves["passive"],
            repeated_error=repeated_error,
            exam_passes=exams["pass"],
            exam_fails=exams["fail"],
            graded_scores=scores,
        ))

    return Trajectory(
        session_turns=session_turns,
        attempts=attempts,
        questions=questions,
        passive=passive,
        minutes_since_last_attempt=minutes_since_attempt,
        goals=tuple(goals),
    )
