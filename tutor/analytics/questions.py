"""Questions: the time between a student's questions
(section 3.8).

For consecutive questions by one student in one course: the gap since the
previous question, the think time since the previous reply finished (the
browser's ``reply_done`` when it was recorded, else the reply's saved
time), and the wait from the question to its reply. Think time falls in a
bucket: a follow-up, reading, away but inside the visit, or a new visit.
Abandoned turns are questions the browser sent whose reply it never saw
finish.
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

from tutor.analytics.base import Store, Thresholds, Window, median

BUCKETS = ("follow_up", "reading", "away", "new_visit")


@dataclass
class QuestionGap:
    student_id: int
    course: Optional[str]
    ts: datetime.datetime
    message_id: int
    lecture_num: Optional[int] = None
    gap_s: Optional[float] = None          # since the previous question in the course
    think_s: Optional[float] = None        # since the previous reply finished
    reply_wait_s: Optional[float] = None   # from this question to its reply
    bucket: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "student_id": self.student_id, "course": self.course, "asked": self.ts, "lecture": self.lecture_num,
            "gap_s": _r(self.gap_s), "think_s": _r(self.think_s), "reply_wait_s": _r(self.reply_wait_s),
            "bucket": self.bucket,
        }


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 1)


def bucket_for(think_s: Optional[float], thresholds: Thresholds) -> Optional[str]:
    if think_s is None:
        return None
    if think_s < thresholds.follow_up_s:
        return "follow_up"
    if think_s < thresholds.reading_max_min * 60:
        return "reading"
    if think_s < thresholds.idle_gap_min * 60:
        return "away"
    return "new_visit"


def question_gaps(store: Store, student_ids: Sequence[int], window: Window, thresholds: Thresholds,
                  course: Optional[str] = None) -> dict[int, list[QuestionGap]]:
    """Each student's questions with their gaps, think times, and buckets."""
    done_at: dict[int, datetime.datetime] = {}
    for sig in store.events(student_ids, window, ["reply_done"]):
        if sig.message_id:
            done_at[int(sig.message_id)] = sig.ts
    out: dict[int, list[QuestionGap]] = {int(s): [] for s in student_ids}
    per_student: dict[int, list] = {}
    for sig in store.messages(student_ids, window, course):
        per_student.setdefault(sig.student_id, []).append(sig)
    for sid, msgs in per_student.items():
        last_question: dict[Optional[str], datetime.datetime] = {}
        last_reply_done: dict[Optional[str], datetime.datetime] = {}
        pending: dict[Optional[str], QuestionGap] = {}
        for sig in msgs:
            key = sig.course
            if sig.source == "question":
                gap = QuestionGap(student_id=sid, course=sig.course, ts=sig.ts, message_id=int(sig.message_id or 0),
                                  lecture_num=sig.lecture_num)
                if key in last_question:
                    gap.gap_s = (sig.ts - last_question[key]).total_seconds()
                if key in last_reply_done:
                    gap.think_s = max(0.0, (sig.ts - last_reply_done[key]).total_seconds())
                    gap.bucket = bucket_for(gap.think_s, thresholds)
                last_question[key] = sig.ts
                pending[key] = gap
                out[sid].append(gap)
            else:
                finished = done_at.get(int(sig.message_id or 0), sig.ts)
                last_reply_done[key] = finished
                if key in pending:
                    pending[key].reply_wait_s = max(0.0, (sig.ts - pending[key].ts).total_seconds())
                    del pending[key]
    return out


def abandoned_turns(store: Store, student_ids: Sequence[int], window: Window) -> dict[int, dict]:
    """Per student: questions the browser sent, how their replies ended,
    and how many never saw a reply finish before the next question."""
    out: dict[int, dict] = {int(s): {"sent": 0, "abandoned": 0, "outcomes": Counter()} for s in student_ids}
    open_question: dict[int, bool] = {}
    for sig in store.events(student_ids, window, ["question_sent", "reply_done", "page_leave"]):
        stats = out.setdefault(sig.student_id, {"sent": 0, "abandoned": 0, "outcomes": Counter()})
        if sig.name == "question_sent":
            if open_question.get(sig.student_id):
                stats["abandoned"] += 1
            stats["sent"] += 1
            open_question[sig.student_id] = True
        elif sig.name == "reply_done":
            stats["outcomes"][str(sig.detail.get("outcome") or "done")] += 1
            open_question[sig.student_id] = False
    for sid, is_open in open_question.items():
        if is_open:
            out[sid]["abandoned"] += 1
    return out


def summarize_questions(gaps: Sequence[QuestionGap], abandoned: Optional[dict] = None) -> dict:
    """Counts, medians, and the bucket shares for one student or a class."""
    thinks = [g.think_s for g in gaps if g.think_s is not None]
    buckets = Counter(g.bucket for g in gaps if g.bucket)
    n_bucketed = sum(buckets.values()) or 1
    sent = (abandoned or {}).get("sent", 0)
    summary = {
        "questions": len(gaps),
        "median_gap_s": _r(median([g.gap_s for g in gaps if g.gap_s is not None])),
        "median_think_s": _r(median(thinks)),
        "median_reply_wait_s": _r(median([g.reply_wait_s for g in gaps if g.reply_wait_s is not None])),
        "follow_up_rate": round(buckets["follow_up"] / n_bucketed, 2) if buckets else None,
        "reading_rate": round(buckets["reading"] / n_bucketed, 2) if buckets else None,
        "away_rate": round(buckets["away"] / n_bucketed, 2) if buckets else None,
        "new_visit_rate": round(buckets["new_visit"] / n_bucketed, 2) if buckets else None,
        "abandoned_rate": round((abandoned or {}).get("abandoned", 0) / sent, 2) if sent else None,
    }
    return summary
