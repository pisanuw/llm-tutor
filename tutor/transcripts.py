"""Render one student's tutor transcript as Markdown or JSON.

Extracted from the admin router so the admin Conversations export and the
instructor's per-student download produce byte-identical files from one
renderer. Pure formatting over ORM rows: the callers do the authorization
and the querying, this module only writes bytes.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Optional

from tutor.courses import IDENTITY
from tutor.db.models import Message, MessageFeedback, Student
from tutor.utctime import utcnow

ROLE_LABEL = {"user": "Student", "assistant": "Tutor"}


def fmt_ts(ts: Optional[datetime.datetime]) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC") if ts else ""


def tool_notes(raw: Optional[str]) -> Optional[list]:
    """Parse the persisted tool_summary JSON; None when absent or unreadable."""
    if not raw:
        return None
    try:
        notes = json.loads(raw)
    except Exception:
        return None
    return notes if isinstance(notes, list) else None


def export_filename(email: str, ext: str) -> str:
    local = email.split("@", 1)[0]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", local).strip("-") or "student"
    return f"tutor-conversation-{safe}-{utcnow().strftime('%Y%m%d')}.{ext}"


def render_markdown(student_obj: Student, rows: list[Message], course: Optional[str],
                    feedback: Optional[dict[int, MessageFeedback]] = None) -> str:
    feedback = feedback or {}
    courses = sorted({m.course for m in rows if m.course})
    lines = [
        f"# {IDENTITY.name} conversation: {student_obj.email}",
        "",
        f"Exported {fmt_ts(utcnow())} · {len(rows)} messages"
        + (f" · course {course}" if course else (" · courses " + ", ".join(courses) if courses else "")),
        "",
    ]
    for m in rows:
        meta = [ROLE_LABEL.get(m.role, m.role)]
        if m.course:
            meta.append(m.course)
        if m.lecture_num:
            meta.append(f"L{m.lecture_num:02d}" + (f" slide {m.slide_num}" if m.slide_num else ""))
        meta.append(fmt_ts(m.created_at))
        lines.append("---")
        lines.append("")
        lines.append("**" + " · ".join(p for p in meta if p) + "**")
        lines.append("")
        lines.append(m.content.rstrip())
        notes = tool_notes(m.tool_summary)
        if notes:
            bits = []
            for n in notes:
                bit = str(n.get("tool", ""))
                if n.get("query"):
                    bit += f" '{n['query']}'"
                if n.get("top_source"):
                    bit += f" -> {n['top_source']}"
                if n.get("slide"):
                    bit += f" (showed slide {n['slide']})"
                if bit:
                    bits.append(bit)
            if bits:
                lines.append("")
                lines.append("_[tools used: " + "; ".join(bits) + "]_")
        fb = feedback.get(m.id)
        if fb is not None:
            lines.append("")
            glyph = "👍" if fb.rating > 0 else "👎"
            lines.append(f"_[student feedback: {glyph}" + (f" {fb.comment}" if fb.comment else "") + "]_")
        lines.append("")
    return "\n".join(lines)


def render_json(student_obj: Student, rows: list[Message], course: Optional[str],
                feedback: Optional[dict[int, MessageFeedback]] = None) -> str:
    feedback = feedback or {}
    payload = {
        "student": student_obj.email,
        "name": student_obj.name,
        "course": course,
        "exported_at": utcnow().isoformat() + "Z",
        "message_count": len(rows),
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "course": m.course,
                "lecture_num": m.lecture_num,
                "slide_num": m.slide_num,
                "tools": tool_notes(m.tool_summary),
                "feedback": (
                    {"rating": feedback[m.id].rating, "comment": feedback[m.id].comment}
                    if m.id in feedback else None
                ),
                "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
            }
            for m in rows
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
