"""Cohort misconception mining (pedagogy item 12).

Input: per-student :class:`Misconception` notes, the actor's distilled record
of a specific wrong model ("thinks the Moon's phases come from Earth's shadow
falling on it"). Per goal, once enough DISTINCT students have such notes
in the window, the supervisor-tier model clusters them into 1-4 cohort-level
misconceptions, each with a one-sentence description and a quiz distractor
that embodies it. Results are upserted as ``candidate`` rows in
``goal_misconceptions``; only rows an instructor has marked ``approved``
reach prompts (envelope note + exam grader). Auto-mined text never shapes
teaching unreviewed.

Cost controls: the model is called only for goals at/above MIN_STUDENTS, at
most MAX_GOALS_PER_RUN goals per run, on the supervisor tier; ``--dry-run``
gathers and reports with zero API calls; with no API key the job reports and
exits 0. With no students yet the weekly job is a no-op.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import anthropic
from sqlalchemy.orm import Session

from tutor.agent.models import SUPERVISOR
from tutor.config import get_config
from tutor.db.models import GoalMisconception, LearningGoal, Misconception
from tutor.utctime import utcnow
from tutor.courses import course_context

logger = logging.getLogger("tutor.misconceptions")

MIN_STUDENTS = 4         # distinct students with a note on the goal before mining
MAX_GOALS_PER_RUN = 12   # cost cap per weekly run
MAX_NOTES_PER_GOAL = 40  # prompt size cap
MAX_ITEMS_PER_GOAL = 4

_PROMPT = (
    "You are curating a misconception library for "
    + course_context().replace("{", "{{").replace("}", "}}")
    + ". Below are short notes, each recording ONE "
    "student's wrong mental model about the learning goal. Cluster them into "
    "the distinct underlying misconceptions (1 to {max_items}). For each, give: "
    "description (one sentence, the wrong belief in the student's terms), "
    "distractor (a plausible wrong quiz answer or wrong code fragment that "
    "embodies it, one line), and count (how many notes support it). Output "
    "ONLY a JSON array of objects with keys description, distractor, count."
)


@dataclass
class GoalEvidence:
    goal_id: str
    title: str
    notes: list[str] = field(default_factory=list)
    students: set[int] = field(default_factory=set)


@dataclass
class RunReport:
    since: datetime.datetime
    until: datetime.datetime
    goals_seen: int = 0
    goals_eligible: int = 0
    goals_mined: int = 0
    candidates_new: int = 0
    candidates_updated: int = 0
    dry_run: bool = False
    lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gather
# ---------------------------------------------------------------------------

def gather(db: Session, since: datetime.datetime, until: datetime.datetime) -> dict[str, GoalEvidence]:
    """Per-goal notes from per-student Misconception rows created in the window."""
    titles = {g.id: g.title for g in db.query(LearningGoal).all()}
    out: dict[str, GoalEvidence] = {}
    rows = (
        db.query(Misconception)
        .filter(Misconception.created_at >= since, Misconception.created_at < until)
        .order_by(Misconception.created_at.asc())
        .all()
    )
    for m in rows:
        desc = (m.description or "").strip()
        if not desc:
            continue
        ev = out.setdefault(m.goal_id, GoalEvidence(goal_id=m.goal_id, title=titles.get(m.goal_id, m.goal_id)))
        if len(ev.notes) < MAX_NOTES_PER_GOAL:
            ev.notes.append(desc[:300])
        ev.students.add(m.student_id)
    return out


def eligible(evidence: Iterable[GoalEvidence]) -> list[GoalEvidence]:
    ranked = [e for e in evidence if len(e.students) >= MIN_STUDENTS]
    ranked.sort(key=lambda e: (-len(e.students), e.goal_id))
    return ranked[:MAX_GOALS_PER_RUN]


# ---------------------------------------------------------------------------
# Synthesize (the only API call in this module)
# ---------------------------------------------------------------------------

def _client() -> anthropic.Anthropic:
    from tutor.agent.clients import supervisor_client

    # Offline weekly job with a larger synthesis prompt: a longer read bound.
    return supervisor_client(api_key=get_config()["anthropic_api_key"], timeout=60.0)


def _parse_items(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    items: list[dict[str, Any]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        desc = str(d.get("description", "")).strip()
        if not desc:
            continue
        try:
            count = int(d.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        items.append({
            "description": desc[:300],
            "distractor": str(d.get("distractor", "")).strip()[:300],
            "count": max(1, count),
        })
    return items[:MAX_ITEMS_PER_GOAL]


def synthesize(ev: GoalEvidence, client: Optional[Any] = None) -> list[dict[str, Any]]:
    """One supervisor-tier call; [] on any failure (never raises)."""
    client = client or _client()
    body = "\n".join(f"- {n}" for n in ev.notes)
    try:
        resp = client.messages.create(
            model=SUPERVISOR.id,
            max_tokens=1200,
            messages=[{
                "role": "user",
                "content": (
                    _PROMPT.format(max_items=MAX_ITEMS_PER_GOAL)
                    + f"\n\nGoal: {ev.title}\n\nNotes ({len(ev.notes)} from {len(ev.students)} students):\n{body}"
                ),
            }],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return _parse_items(text)
    except Exception as exc:  # noqa: BLE001 - offline job reports, never crashes
        logger.error("misconception synthesis failed for %s: %s: %s", ev.goal_id, type(exc).__name__, exc)
        return []


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def upsert_candidates(
    db: Session, ev: GoalEvidence, items: list[dict[str, Any]], run_label: str
) -> tuple[int, int]:
    """Insert new candidates; refresh evidence on near-duplicates. Never touches
    the status of an existing row (an instructor's approve/reject stands)."""
    existing = db.query(GoalMisconception).filter_by(goal_id=ev.goal_id).all()
    by_norm = {_norm(r.description): r for r in existing}
    new = updated = 0
    now = utcnow()
    examples = json.dumps(ev.notes[:3])
    for it in items:
        key = _norm(it["description"])
        row = by_norm.get(key)
        if row is None:
            db.add(GoalMisconception(
                goal_id=ev.goal_id, description=it["description"], distractor=it["distractor"],
                evidence_count=it["count"], examples_json=examples, status="candidate",
                run_label=run_label, created_at=now, updated_at=now,
            ))
            new += 1
        else:
            row.evidence_count = (row.evidence_count or 0) + it["count"]
            if it["distractor"] and not row.distractor:
                row.distractor = it["distractor"]
            row.examples_json = examples
            row.run_label = run_label
            row.updated_at = now
            updated += 1
    db.flush()   # the caller commits (plan step 1.11)
    return new, updated


# ---------------------------------------------------------------------------
# Orchestrate
# ---------------------------------------------------------------------------

def run(
    db: Session, since: datetime.datetime, until: datetime.datetime,
    *, dry_run: bool = False, client: Optional[Any] = None, run_label: str = "",
) -> RunReport:
    rep = RunReport(since=since, until=until, dry_run=dry_run)
    evidence = gather(db, since, until)
    rep.goals_seen = len(evidence)
    todo = eligible(evidence.values())
    rep.goals_eligible = len(todo)
    for ev in todo:
        rep.lines.append(f"{ev.goal_id}: {len(ev.students)} students, {len(ev.notes)} notes")
    if dry_run or not todo:
        return rep
    label = run_label or until.strftime("%Y-%m-%d")
    for ev in todo:
        items = synthesize(ev, client=client)
        rep.goals_mined += 1
        if not items:
            rep.lines.append(f"{ev.goal_id}: synthesis returned nothing")
            continue
        n, u = upsert_candidates(db, ev, items, label)
        rep.candidates_new += n
        rep.candidates_updated += u
    return rep


# ---------------------------------------------------------------------------
# Consumption (prompts) + curation
# ---------------------------------------------------------------------------

def approved_for_goals(db: Session, goal_ids: set[str], limit: int = 4) -> list[tuple[str, str]]:
    """(goal_id, description) for approved library rows on the given goals,
    most-evidenced first. Pure read; safe to call per turn."""
    if not goal_ids:
        return []
    rows = (
        db.query(GoalMisconception)
        .filter(GoalMisconception.goal_id.in_(goal_ids), GoalMisconception.status == "approved")
        .order_by(GoalMisconception.evidence_count.desc(), GoalMisconception.id.asc())
        .limit(limit)
        .all()
    )
    return [(r.goal_id, r.description) for r in rows]


def set_status(db: Session, row_id: int, status: str) -> bool:
    if status not in ("candidate", "approved", "rejected"):
        raise ValueError(f"bad status {status!r}")
    row = db.get(GoalMisconception, row_id)
    if row is None:
        return False
    row.status = status
    row.updated_at = utcnow()
    db.flush()   # the caller commits (plan step 1.11)
    return True
