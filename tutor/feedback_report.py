"""Daily thumbs-down report: which tutor replies students flagged, and why.

Pure query + render helpers used by ``scripts/feedback_report.py`` (the
scheduled emailer). Kept free of Resend/argparse so the rendering is unit-
testable. Each flagged reply links straight to the admin Conversations tab,
anchored on the message (``#msg-<id>``), so the instructor lands on the
highlighted bubble instead of scrolling a transcript.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session as DBSession

from tutor.courses import IDENTITY
from tutor.db.models import Message, MessageFeedback, Student


@dataclass
class FlaggedReply:
    feedback_id: int
    message_id: int
    student_email: str
    course: Optional[str]
    lecture_num: Optional[int]
    comment: Optional[str]
    flagged_at: Optional[datetime.datetime]
    reply_excerpt: str
    question_excerpt: str
    # Flagged before the window; its comment (or rating) changed inside it.
    updated_later: bool = False
    # When the instructor marked it seen on Admin → Today (review step 0.4).
    seen_at: Optional[datetime.datetime] = None


def _excerpt(text: str, n: int = 240) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= n else flat[: n - 1].rstrip() + "…"


def thumbs_down_in_window(db: DBSession, since: datetime.datetime,
                          until: datetime.datetime) -> list[FlaggedReply]:
    """Every thumbs-down given in [since, until), plus older ones changed in
    it (a comment added the next day), marked ``updated_later``. Selecting by
    the latest change used to report such a flag again as if it were new
    (plan step 4.4, finding B23)."""
    rows = (
        db.query(MessageFeedback, Message, Student)
        .join(Message, Message.id == MessageFeedback.message_id)
        .join(Student, Student.id == MessageFeedback.student_id)
        .filter(MessageFeedback.rating < 0, or_(
            and_(MessageFeedback.created_at >= since, MessageFeedback.created_at < until),
            and_(MessageFeedback.created_at < since,
                 MessageFeedback.updated_at >= since, MessageFeedback.updated_at < until),
        ))
        .order_by(MessageFeedback.created_at.asc())
        .all()
    )
    out: list[FlaggedReply] = []
    for fb, msg, student in rows:
        # The student turn that prompted this reply: the nearest earlier user
        # message from the same student (same course when known).
        q = (db.query(Message)
             .filter(Message.student_id == msg.student_id, Message.role == "user",
                     Message.id < msg.id))
        if msg.course:
            q = q.filter(Message.course == msg.course)
        prev = q.order_by(Message.id.desc()).first()
        out.append(FlaggedReply(
            feedback_id=fb.id,
            message_id=msg.id,
            student_email=student.email,
            course=msg.course,
            lecture_num=msg.lecture_num,
            comment=fb.comment,
            flagged_at=fb.created_at,
            reply_excerpt=_excerpt(msg.content),
            question_excerpt=_excerpt(prev.content) if prev else "",
            updated_later=bool(fb.created_at and fb.created_at < since),
            seen_at=getattr(fb, "seen_at", None),
        ))
    return out


def conversation_link(base_url: str, feedback_id: int) -> str:
    """The one link for a flag (review step 0.4, F17 and F20): GET
    /feedback/{id} sends an admin to the admin transcript and a class
    instructor to their transcript view, each opened around the reply and
    highlighted, and a visitor to the login page with the destination kept."""
    return f"{base_url.rstrip('/')}/feedback/{feedback_id}"


def _fmt_ts(ts: Optional[datetime.datetime]) -> str:
    """Local time, like the subject's window label (the body said UTC, F24)."""
    from tutor.utctime import to_local

    return to_local(ts).strftime("%Y-%m-%d %H:%M %Z") if ts else ""


def render_report_markdown(flags: list[FlaggedReply], label: str, base_url: str) -> str:
    """Plain-text/Markdown body for the email. Empty list -> empty string."""
    if not flags:
        return ""
    n = len(flags)
    lines = [
        f"# {IDENTITY.name} thumbs-down report: {label}",
        "",
        f"{n} tutor repl{'y' if n == 1 else 'ies'} flagged by "
        f"{len({f.student_email for f in flags})} student(s). "
        "Each link opens the flagged reply in its conversation, for an admin or the class instructor.",
        "",
    ]
    for i, f in enumerate(flags, 1):
        where = " · ".join(p for p in [
            f.course or "", f"L{f.lecture_num:02d}" if f.lecture_num else "", _fmt_ts(f.flagged_at)
        ] if p)
        lines += [
            f"## {i}. {f.student_email}" + (f"  ({where})" if where else "")
            + ("  [comment added; flagged earlier]" if f.updated_later else ""),
            "",
            f"Link: {conversation_link(base_url, f.feedback_id)}",
            "",
            "What was wrong: " + (f.comment if f.comment else "(no reason given)"),
            "",
        ]
        if f.question_excerpt:
            lines += [f"Student asked: {f.question_excerpt}", ""]
        lines += [f"Tutor replied: {f.reply_excerpt}", ""]
    return "\n".join(lines)


def render_report_html(flags: list[FlaggedReply], label: str, base_url: str) -> str:
    """HTML body with clickable links (email clients do not render Markdown)."""
    import html as _html

    if not flags:
        return ""
    n = len(flags)
    parts = [
        f"<h2 style='font-family:sans-serif'>{_html.escape(IDENTITY.name)} thumbs-down report: "
        f"{_html.escape(label)}</h2>",
        f"<p style='font-family:sans-serif'>{n} tutor repl{'y' if n == 1 else 'ies'} flagged by "
        f"{len({f.student_email for f in flags})} student(s). "
        "Each link opens the flagged reply in its conversation, for an admin or the class instructor.</p>",
    ]
    for i, f in enumerate(flags, 1):
        link = conversation_link(base_url, f.feedback_id)
        where = " · ".join(p for p in [
            f.course or "", f"L{f.lecture_num:02d}" if f.lecture_num else "", _fmt_ts(f.flagged_at)
        ] if p)
        parts.append(
            "<div style='font-family:sans-serif;border-left:3px solid #f87171;"
            "padding:6px 12px;margin:14px 0'>"
            f"<p style='margin:0 0 6px'><b>{i}. <a href='{_html.escape(link)}'>"
            f"{_html.escape(f.student_email)}</a></b>"
            + (f" <span style='color:#666'>({_html.escape(where)})</span>" if where else "")
            + (" <span style='color:#92400e'>[comment added; flagged earlier]</span>"
               if f.updated_later else "")
            + "</p>"
            "<p style='margin:0 0 6px'><b>What was wrong:</b> "
            + (_html.escape(f.comment) if f.comment else "<i>(no reason given)</i>")
            + "</p>"
            + (f"<p style='margin:0 0 6px;color:#444'><b>Student asked:</b> "
               f"{_html.escape(f.question_excerpt)}</p>" if f.question_excerpt else "")
            + f"<p style='margin:0;color:#444'><b>Tutor replied:</b> {_html.escape(f.reply_excerpt)}</p>"
            "</div>"
        )
    return "\n".join(parts)
