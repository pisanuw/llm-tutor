"""Learning metrics for the daily report (pedagogy item 13).

"Measure learning, not usage." Turn counts and cost say students used the
tutor; these three say whether they learned:

* Mastery velocity: goals reaching status=mastered in the window, normalized
  per active student per 7 days.
* 2-week retention: review outcomes (GradedEvent source="review") recorded at
  least RETENTION_DAYS after the goal was first mastered. Retention is the
  fraction of those reviews with success >= RETAINED_THRESHOLD; the mean
  success is reported alongside. Reviews earlier than 14 days after mastery
  measure short-term recall and are excluded on purpose.
* Calibration error: from score_predictions (item 11): mean absolute error,
  signed bias (positive = students expect more than they score), and the
  exact-hit rate.

Pure computation over ORM rows; the daily report renders the result. Every
metric carries its n so small-sample noise is visible in the report rather
than hidden behind a percentage.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

RETENTION_DAYS = 14
# Matches state.RETRIEVAL_SUCCESS: a shaky review (HARD, 0.6) is not
# "retained" (plan step 1.10, finding B11).
RETAINED_THRESHOLD = 0.7


@dataclass
class LearningMetrics:
    since: datetime.datetime
    until: datetime.datetime
    active_students: int = 0
    # Mastery velocity (item 9 tiers): confirmed = spaced evidence, the real
    # signal; mastered = quiz/progression tier, reported as provisional.
    goals_confirmed: int = 0
    confirmed_per_student_week: Optional[float] = None
    cumulative_confirmed: int = 0
    goals_mastered: int = 0
    cumulative_mastered: int = 0
    # Retention
    retention_reviews: int = 0
    retention_rate: Optional[float] = None
    retention_mean_success: Optional[float] = None
    # Calibration
    predictions: int = 0
    calibration_mae: Optional[float] = None
    calibration_bias: Optional[float] = None
    calibration_exact_rate: Optional[float] = None
    notes: list[str] = field(default_factory=list)


def compute_learning_metrics(
    db: Session, since: datetime.datetime, until: datetime.datetime
) -> LearningMetrics:
    from tutor.db.models import GradedEvent, Message, ScorePrediction, StudentProgress

    m = LearningMetrics(since=since, until=until)

    # Active students: anyone who sent a message in the window.
    active_ids = {
        sid for (sid,) in db.query(Message.student_id)
        .filter(Message.created_at >= since, Message.created_at < until,
                Message.role == "user")
        .distinct()
    }
    m.active_students = len(active_ids)

    # --- Mastery velocity -------------------------------------------------
    # Two tiers from StudentProgress (pedagogy item 9): confirmed_at is set
    # only when successful retrievals span distinct days; mastered_at is the
    # quiz tier.
    m.goals_confirmed = (
        db.query(StudentProgress)
        .filter(StudentProgress.confirmed_at.isnot(None),
                StudentProgress.confirmed_at >= since,
                StudentProgress.confirmed_at < until)
        .count()
    )
    m.cumulative_confirmed = (
        db.query(StudentProgress).filter(StudentProgress.confirmed_at.isnot(None)).count()
    )
    m.goals_mastered = (
        db.query(StudentProgress)
        .filter(StudentProgress.mastered_at.isnot(None),
                StudentProgress.mastered_at >= since,
                StudentProgress.mastered_at < until)
        .count()
    )
    m.cumulative_mastered = (
        db.query(StudentProgress).filter(StudentProgress.mastered_at.isnot(None)).count()
    )
    window_days = max((until - since).total_seconds() / 86400.0, 1e-9)
    if m.active_students:
        m.confirmed_per_student_week = (
            m.goals_confirmed / m.active_students * (7.0 / window_days)
        )

    # --- 2-week retention ---------------------------------------------------
    # First mastery per (student, goal) = StudentProgress.mastered_at; a review
    # counts toward retention only when it happened >= RETENTION_DAYS after.
    first_mastered: dict[tuple[int, str], datetime.datetime] = {}
    for sid, gid, mastered_at in (
        db.query(StudentProgress.student_id, StudentProgress.goal_id, StudentProgress.mastered_at)
        .filter(StudentProgress.mastered_at.isnot(None))
        .all()
    ):
        if mastered_at is not None:
            first_mastered[(sid, gid)] = mastered_at

    reviews = (
        db.query(GradedEvent)
        .filter(GradedEvent.source == "review",
                GradedEvent.ts >= since, GradedEvent.ts < until)
        .all()
    )
    delta = datetime.timedelta(days=RETENTION_DAYS)
    counted = [
        r for r in reviews
        if (r.student_id, r.goal_id) in first_mastered
        and r.ts >= first_mastered[(r.student_id, r.goal_id)] + delta
    ]
    m.retention_reviews = len(counted)
    if counted:
        m.retention_rate = sum(1 for r in counted if r.success >= RETAINED_THRESHOLD) / len(counted)
        m.retention_mean_success = sum(r.success for r in counted) / len(counted)
    elif reviews:
        m.notes.append(
            f"{len(reviews)} review(s) in window, none yet {RETENTION_DAYS}+ days after mastery"
        )

    # --- Calibration ----------------------------------------------------------
    preds = (
        db.query(ScorePrediction)
        .filter(ScorePrediction.ts >= since, ScorePrediction.ts < until)
        .all()
    )
    m.predictions = len(preds)
    if preds:
        errs = [p.predicted - p.actual for p in preds]
        m.calibration_mae = sum(abs(e) for e in errs) / len(errs)
        m.calibration_bias = sum(errs) / len(errs)
        m.calibration_exact_rate = sum(1 for e in errs if e == 0) / len(errs)

    return m


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def render_learning_markdown(m: LearningMetrics) -> str:
    """Digest section. Leads the report: this is what the tutor is for."""
    lines = ["## Learning signals", ""]
    if (m.active_students == 0 and m.goals_mastered == 0 and m.goals_confirmed == 0
            and m.predictions == 0 and m.retention_reviews == 0):
        lines.append("_No learning signals in this window yet._")
        lines.append("")
        return "\n".join(lines)

    vel = ("n/a" if m.confirmed_per_student_week is None
           else f"{m.confirmed_per_student_week:.2f} goals/student/week")
    lines.append(
        f"- **Mastery velocity (confirmed by spaced retrieval):** {m.goals_confirmed} this window "
        f"({vel}); {m.cumulative_confirmed} confirmed all-time"
    )
    lines.append(
        f"- **Provisional mastery (quiz tier, awaiting a spaced review):** "
        f"{m.goals_mastered} this window; {m.cumulative_mastered - m.cumulative_confirmed} awaiting all-time"
    )
    if m.retention_reviews:
        lines.append(
            f"- **{RETENTION_DAYS}-day retention:** {_pct(m.retention_rate)} retained "
            f"(mean success {m.retention_mean_success:.2f}, n={m.retention_reviews} reviews)"
        )
    else:
        lines.append(f"- **{RETENTION_DAYS}-day retention:** n/a (no reviews {RETENTION_DAYS}+ days after mastery yet)")
    if m.predictions:
        bias = m.calibration_bias or 0.0
        direction = "over-confident" if bias > 0.05 else "under-confident" if bias < -0.05 else "well centered"
        lines.append(
            f"- **Calibration:** MAE {m.calibration_mae:.2f} on a 0-4 scale, bias "
            f"{bias:+.2f} ({direction}), exact {_pct(m.calibration_exact_rate)}, n={m.predictions}"
        )
    else:
        lines.append("- **Calibration:** n/a (no score predictions this window)")
    for note in m.notes:
        lines.append(f"- _{note}_")
    lines.append("")
    return "\n".join(lines)
