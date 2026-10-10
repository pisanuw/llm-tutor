"""The weekly instructor email: who used the tutor, who did not, what they learned.

Pure gather + render, like tutor/reporting.py and tutor/feedback_report.py; the
Resend call and the argument parsing live in scripts/instructor_report.py, and
the same functions back the in-app preview at ``/instructor/report``. The
numbers come from tutor/class_metrics.py, so the email and the dashboard can
never disagree.

The report leads with usage, not learning, because the question an instructor
cannot answer from their own classroom is "who is not using this at all?".
Named students appear under "needs attention" (never started, or gone quiet)
and under "most active"; everything else is counts. It is a teaching signal,
not a grade sheet, and the footer says so.

Who gets it: an instructor who has not switched it off on /instructor, for
each of their classes that is open, has its admin switch on, has students,
is inside its quarter dates today, and had any activity in the window (a
class nobody touched sends nothing; ``include_quiet`` restores the old
"nobody used it" email). With a local analytics store the email also
carries a day-by-day table and the class's daily chart page as an
attachment (``attach_daily``).

Since 2026-10-06 each class also reports the week's thumbs up and down on
the tutor's replies and lists the flagged ones with the student's reason and
a link to the reply (``FeedbackWeek``): the same rows the dashboard's
"Replies students flagged as wrong" section shows, capped at FLAGGED_LIMIT.
"""

from __future__ import annotations

import datetime
import html as _html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DBSession

from tutor import class_metrics, classes as classes_mod, engagement as engagement_mod
from tutor import feedback_report
from tutor.class_metrics import ClassSummary, StudentEngagement
from tutor.courses import IDENTITY, course_label
from tutor.db.models import CourseClass, Message, MessageFeedback, Student
from tutor.feedback_report import FlaggedReply
from tutor.utctime import REPORT_TZ

# Cap the named lists so one bad week does not produce a wall of 200 emails.
ATTENTION_LIMIT = 15
TOP_LIMIT = 3
FLAGGED_LIMIT = 10      # flagged replies listed per class; the rest are on the dashboard
# The day-by-day table in the email: a few columns that render anywhere.
DAILY_COLUMNS = (("active_students", "students"), ("questions", "questions"),
                 ("active_min", "on-screen min"), ("narration_min", "narration min"))


@dataclass
class FeedbackWeek:
    """The week's thumbs up and down on the class's replies (in the class's
    course, by its roster), and the flagged ones, newest first, with the
    student's reason."""

    up: int = 0
    down: int = 0
    flagged: list[FlaggedReply] = field(default_factory=list)

    @property
    def rated(self) -> int:
        return self.up + self.down

    def line(self) -> str:
        """The sentence after "Feedback on the tutor:"."""
        if not self.rated:
            return "no ratings this week"
        share = round(100 * self.down / self.rated)
        return f"👍 {self.up} · 👎 {self.down} ({share}% of rated replies were thumbs-down)"


@dataclass
class ClassWeek:
    """One class's week: the roll-up, the roster rows, and the named lists."""

    course_class: CourseClass
    summary: ClassSummary
    rows: list[StudentEngagement] = field(default_factory=list)
    confirmed_goals: list[tuple[str, int]] = field(default_factory=list)
    feedback: FeedbackWeek = field(default_factory=FeedbackWeek)
    # How the roster used the site, from the browser's activity events
    # (phase 4).
    activity: engagement_mod.ClassActivity = field(default_factory=engagement_mod.ClassActivity)
    # Day by day, from the local analytics store (attach_daily); None without one.
    daily: Optional[object] = None
    attachment: Optional[tuple[str, bytes]] = None
    # Where each student stands within the class this week, a percentile per
    # measure (review step 0.9); empty under five students.
    standings: list[class_metrics.Standing] = field(default_factory=list)

    @property
    def has_activity(self) -> bool:
        """Anyone on the roster did anything in the window."""
        return bool(self.summary.active or self.summary.messages or self.activity.events)

    def daily_rows(self) -> list[dict]:
        """One row per day with the DAILY_COLUMNS, for the email's table."""
        table = self.daily
        if table is None:
            return []
        out = []
        for i, day in enumerate(table.days):            # type: ignore[attr-defined]
            row: dict = {"day": day}
            for key, heading in DAILY_COLUMNS:
                m = table.metric(key)                   # type: ignore[attr-defined]
                value = sum((s.values[i] or 0) for s in m.series)
                row[heading] = int(value) if m.unit == "int" else round(value)
            out.append(row)
        return out

    def activity_lines(self) -> list[str]:
        """The two usage sentences, markdown-ready."""
        a = self.activity
        if not a.events:
            return []       # nothing recorded in the browser: say nothing rather than zeros
        visits = f"{a.total_visits} visit" + ("" if a.total_visits == 1 else "s")
        median = f" (median {a.median_active_min:g} min per student who came)" if a.median_active_min is not None else ""
        decks = f"{a.students_with_deck} student" + ("" if a.students_with_deck == 1 else "s") + " opened a lecture deck"
        listened = f"{a.lectures_listened} lecture" + ("" if a.lectures_listened == 1 else "s") + " listened through"
        return [f"**Time on the site:** {a.total_active_min:g} min over {visits}{median}",
                f"**Lectures:** {decks}; {listened}"]

    @property
    def never(self) -> list[StudentEngagement]:
        return [r for r in self.rows if r.status == class_metrics.STATUS_NEVER]

    @property
    def dormant(self) -> list[StudentEngagement]:
        return [r for r in self.rows if r.status == class_metrics.STATUS_DORMANT]

    @property
    def locked_out(self) -> list[StudentEngagement]:
        return [r for r in self.rows if r.status == class_metrics.STATUS_LOCKED_OUT]

    @property
    def signed_in(self) -> list[StudentEngagement]:
        return [r for r in self.rows if r.status == class_metrics.STATUS_SIGNED_IN]

    def not_using_line(self) -> str:
        """The "not using it" clause, only the non-zero parts."""
        s = self.summary
        parts = [(s.never, "never signed in"), (s.signed_in, "signed in and never asked"),
                 (s.dormant, "went quiet this week"), (s.locked_out, "could not sign in")]
        said = [f"{n} {what}" for n, what in parts if n]
        return ", ".join(said) if said else "nobody"

    def standings_table(self) -> list[list[str]]:
        """Header and rows for the percentile table, or ``[]``."""
        if not self.standings:
            return []
        header = ["student"] + [h for _k, h in class_metrics.PERCENTILE_METRICS]
        rows = [[_name(st) + (" (staff, unranked)" if st.is_staff else "")]
                + [st.cell(k) for k, _h in class_metrics.PERCENTILE_METRICS] for st in self.standings]
        return [header] + rows

    @property
    def most_active(self) -> list[StudentEngagement]:
        ranked = sorted(self.rows, key=lambda r: (-r.messages, -r.active_days, r.email))
        return [r for r in ranked if r.messages][:TOP_LIMIT]


def class_feedback(db: DBSession, course_class: CourseClass, roster: list[Student],
                   since: datetime.datetime, until: datetime.datetime) -> FeedbackWeek:
    """The roster's ratings on replies in the class's course whose last change
    fell in the window (a re-rating counts in the week it changed, as the
    dashboard counts them), and the thumbs-down among them as the dashboard's
    flagged list has them: newest first, a reply with no course counting for
    any class."""
    ids = [s.id for s in roster]
    if not ids:
        return FeedbackWeek()
    course = course_class.course
    counts: dict[int, int] = {int(rating): int(n) for rating, n in (
        db.query(MessageFeedback.rating, func.count(MessageFeedback.id))
        .join(Message, Message.id == MessageFeedback.message_id)
        .filter(MessageFeedback.student_id.in_(ids),
                or_(Message.course == course, Message.course.is_(None)),
                MessageFeedback.updated_at >= since, MessageFeedback.updated_at < until)
        .group_by(MessageFeedback.rating)
        .all()
    )}
    emails = {s.email for s in roster}
    flagged = [f for f in reversed(feedback_report.thumbs_down_in_window(db, since, until))
               if f.student_email in emails and (not f.course or f.course == course)]
    return FeedbackWeek(up=sum(n for r, n in counts.items() if r > 0),
                        down=sum(n for r, n in counts.items() if r < 0), flagged=flagged)


def today_local(now: Optional[datetime.datetime] = None) -> datetime.date:
    from tutor.utctime import to_local, utcnow

    return to_local(now or utcnow()).date()


def eligible_classes(db: DBSession, instructor: Student,
                     now: Optional[datetime.datetime] = None) -> list[CourseClass]:
    """This instructor's classes that should appear in the weekly email.

    Active (not archived), with the per-class ``weekly_report`` switch on,
    inside its quarter dates today (local time), and with at least one student —
    an empty roster has nothing to report and would only train the instructor
    to ignore the email.
    """
    today = today_local(now)
    out = []
    for c in classes_mod.classes_for_instructor(db, instructor.id):
        if not c.weekly_report:
            continue
        if not c.in_quarter(today):
            continue
        if not classes_mod.roster_ids(db, c.id):
            continue
        out.append(c)
    return out


def eligible_instructors(db: DBSession, now: Optional[datetime.datetime] = None) -> list[Student]:
    """Every instructor who has not opted out and has a reportable class."""
    from tutor.auth.access import list_instructors

    return [s for s in list_instructors(db)
            if not s.weekly_email_opt_out and eligible_classes(db, s, now)]


def gather(
    db: DBSession,
    instructor: Student,
    since: datetime.datetime,
    until: datetime.datetime,
    now: Optional[datetime.datetime] = None,
    include_quiet: bool = False,
) -> list[ClassWeek]:
    """Build one :class:`ClassWeek` per reportable class that had activity
    in the window (every reportable class with ``include_quiet``), largest
    class first."""
    weeks: list[ClassWeek] = []
    for c in eligible_classes(db, instructor, now):
        roster = classes_mod.class_roster(db, c.id)
        summary, rows = class_metrics.class_report(db, c, roster, since, until, now=now)
        activity = engagement_mod.class_activity(db, c.course, [s.id for s in roster], since, until,
                                                 staff_ids=[r.student_id for r in rows if r.is_staff])
        week = ClassWeek(
            course_class=c,
            summary=summary,
            rows=rows,
            confirmed_goals=class_metrics.confirmed_goal_titles(
                db, c.course, [s.id for s in roster], since, until
            ),
            activity=activity,
            feedback=class_feedback(db, c, roster, since, until),
            standings=class_metrics.standings(rows, activity.rows),
        )
        if include_quiet or week.has_activity:
            weeks.append(week)
    weeks.sort(key=lambda w: (-w.summary.students, w.summary.name))
    return weeks


def attach_daily(weeks: list[ClassWeek], store_path: Path, since: datetime.datetime,
                 until: datetime.datetime, tz: str = REPORT_TZ) -> None:
    """Fill each week's day-by-day table and chart-page attachment from the
    local analytics store (python -m tutor.analytics export), over the
    window's calendar days. The store is pseudonymous, so the page carries
    ids only; the roster comes from the class, not the store's enrollments."""
    from tutor.analytics import daily, daily_page
    from tutor.analytics.base import Store, Thresholds, Window

    store = Store(store_path)
    try:
        for w in weeks:
            ids = [r.student_id for r in w.rows]
            table = daily.daily_table(store, ids, Window(since, until), Thresholds(), ZoneInfo(tz))
            w.daily = table
            title = f"Daily use: {w.summary.label}"
            context = [f"Daily totals for {w.summary.label} ({len(ids)} students), "
                       f"{table.days[0]} to {table.days[-1]}, {tz} calendar days."]
            page = daily_page.render(table, title, context)
            safe = "".join(ch if ch.isalnum() else "-" for ch in w.summary.label).strip("-").lower()
            w.attachment = (f"tutor-daily-{safe}-{table.days[-1]}.html", page.encode("utf-8"))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def window_label(since: datetime.datetime, until: datetime.datetime) -> str:
    """Both dates in the class's time zone (plan step 4.4, finding B23: UTC
    dates named the wrong day for a reader in another time zone)."""
    from tutor.utctime import window_label as local_label

    return local_label(since, until, times=False)


def subject(weeks: list[ClassWeek], label: str) -> str:
    """Subject line that carries the headline number, so it reads in a list view."""
    students = sum(w.summary.students for w in weeks)
    active = sum(w.summary.active for w in weeks)
    if not weeks:
        return f"{IDENTITY.name} weekly: no classes to report ({label})"
    return f"{IDENTITY.name} weekly: {active} of {students} students used the tutor ({label})"


def _name(row: "StudentEngagement | class_metrics.Standing") -> str:
    return row.name or row.email


def _named_list(rows: list[StudentEngagement], limit: int = ATTENTION_LIMIT) -> str:
    shown = [f"{_name(r)} <{r.email}>" for r in rows[:limit]]
    extra = len(rows) - len(shown)
    return ", ".join(shown) + (f", and {extra} more" if extra > 0 else "")


def _dash(v) -> str:
    return "—" if v is None else str(v)


def _questions(n: int) -> str:
    return f"{n} question" + ("" if n == 1 else "s")


def transcript_link(base_url: str, class_id: int, email: str, message_id: int) -> str:
    """The instructor's on-screen transcript of one student, at one reply."""
    from urllib.parse import quote

    return (f"{base_url.rstrip('/')}/instructor/transcript/view?class_id={class_id}"
            f"&student={quote(email, safe='')}&msg={message_id}#msg-{message_id}")


def _flag_parts(w: ClassWeek, f: FlaggedReply, base_url: str) -> tuple[str, str, str, str]:
    """(who, when and lecture, the student's reason, the link) for one flagged reply."""
    from tutor.utctime import to_local

    names = {r.email: _name(r) for r in w.rows}
    who = f"{names.get(f.student_email, f.student_email)} <{f.student_email}>"
    when = to_local(f.flagged_at).strftime("%b %d") if f.flagged_at else ""
    if f.lecture_num:
        when += f", Lecture {f.lecture_num}"
    reason = f"“{' '.join(f.comment.split())}”" if f.comment else "no reason given"
    return who, when, reason, transcript_link(base_url, w.course_class.id, f.student_email, f.message_id)


def render_markdown(
    instructor: Student, weeks: list[ClassWeek], label: str, base_url: str
) -> str:
    """Plain-text body (also the in-app preview and the --no-email output)."""
    who = instructor.name.split()[0] if instructor.name else instructor.email
    lines = [f"# {IDENTITY.name} weekly report — {label}", "", f"Hi {who},", ""]
    if not weeks:
        lines += ["No classes with students are set to report this week.", ""]
        return "\n".join(lines)

    lines += [
        "Here is how your students used the tutor this week. This is engagement, "
        f"not a grade: {IDENTITY.name} is not used to grade students.",
        "",
    ]
    for w in weeks:
        s = w.summary
        lines += [f"## {s.label} — {course_label(s.course)}", ""]
        lines.append(
            f"- **Used the tutor:** {s.active} of {s.students} students"
            + (f" ({s.participation * 100:.0f}%)" if s.participation is not None else "")
        )
        lines.append(f"- **Questions asked:** {s.messages} (median {s.median_messages} per student)")
        lines.append(f"- **Not using it:** {w.not_using_line()}")
        if s.reading:
            lines.append(f"- **Reading only:** {s.reading} came to the site without asking anything")
        if s.top_lectures:
            lines.append(
                "- **Most asked about:** "
                + ", ".join(f"Lecture {lec} ({n})" for lec, n in s.top_lectures)
            )
        lines.append(
            f"- **Exercises graded:** {s.exercises}"
            + (f" (avg {s.avg_score}/4)" if s.avg_score is not None else "")
            + f" · mini-exams passed/failed: {s.exams_passed}/{s.exams_failed}"
        )
        lines.append(
            f"- **Mastery:** {s.goals_confirmed} goal(s) confirmed by spaced retrieval, "
            f"{s.goals_mastered} provisional (quiz tier)"
        )
        lines.append(f"- **Feedback on the tutor:** {w.feedback.line()}")
        if w.confirmed_goals:
            lines.append(
                "- **Confirmed this week:** "
                + ", ".join(f"{t} ({n})" for t, n in w.confirmed_goals)
            )
        lines += [f"- {line}" for line in w.activity_lines()]
        lines.append("")

        flagged = w.feedback.flagged
        if flagged:
            lines += [f"**Replies students flagged as wrong ({len(flagged)}):**", ""]
            for f in flagged[:FLAGGED_LIMIT]:
                who, when, reason, link = _flag_parts(w, f, base_url)
                lines.append(f"- {who}, {when}: {reason} — the reply: {f.reply_excerpt} — {link}")
            if len(flagged) > FLAGGED_LIMIT:
                lines.append(f"- and {len(flagged) - FLAGGED_LIMIT} more on the dashboard: "
                             f"{dashboard_link(base_url, w.course_class.id)}#flagged")
            lines.append("")

        if w.locked_out:
            lines += [f"**Could not sign in ({len(w.locked_out)}):** {_named_list(w.locked_out)}", ""]
        if w.never:
            lines += [f"**Never signed in ({len(w.never)}):** {_named_list(w.never)}", ""]
        if w.signed_in:
            lines += [f"**Signed in, never asked ({len(w.signed_in)}):** {_named_list(w.signed_in)}", ""]
        if w.dormant:
            lines += [f"**Went quiet ({len(w.dormant)}):** {_named_list(w.dormant)}", ""]
        if w.most_active:
            lines += [
                "**Most active:** "
                + ", ".join(f"{_name(r)} ({_questions(r.messages)})" for r in w.most_active),
                "",
            ]
        daily_rows = w.daily_rows()
        if daily_rows:
            headings = ["day"] + [h for _k, h in DAILY_COLUMNS]
            lines += ["**By day:**", "", "| " + " | ".join(headings) + " |",
                      "|" + "---|" * len(headings)]
            lines += ["| " + " | ".join(str(r[h]) for h in headings) + " |" for r in daily_rows]
            lines.append("")
        table = w.standings_table()
        if table:
            lines += ["**Where each student stands this week** (percentile within the class; "
                      "a rank, not a judgement):", "",
                      "| " + " | ".join(table[0]) + " |", "|" + "---|" * len(table[0])]
            lines += ["| " + " | ".join(r) + " |" for r in table[1:]]
            lines.append("")
        if w.attachment:
            lines += [f"Attached: {w.attachment[0]} (daily charts; open it in a browser).", ""]
        lines += [f"Open the class: {dashboard_link(base_url, w.course_class.id)}", ""]

    lines += [
        "---",
        "",
        f"You receive this because you teach a class in {IDENTITY.name}. Turn it off on your "
        f"instructor page ({base_url.rstrip('/')}/instructor, \"Weekly email\"); an admin can "
        "also silence one class (Admin → Classes).",
        "",
    ]
    return "\n".join(lines)


def dashboard_link(base_url: str, class_id: int) -> str:
    return f"{base_url.rstrip('/')}/instructor?class_id={class_id}"


def render_html(
    instructor: Student, weeks: list[ClassWeek], label: str, base_url: str
) -> str:
    """HTML body (email clients do not render Markdown)."""
    esc = _html.escape
    who = esc(instructor.name.split()[0] if instructor.name else instructor.email)
    font = "font-family:system-ui,-apple-system,Segoe UI,sans-serif"
    parts = [
        f"<div style='{font};max-width:680px'>",
        f"<h2 style='margin:0 0 4px'>{esc(IDENTITY.name)} weekly report</h2>",
        f"<p style='color:#666;margin:0 0 18px'>{esc(label)}</p>",
        f"<p>Hi {who},</p>",
    ]
    if not weeks:
        parts.append("<p>No classes with students are set to report this week.</p></div>")
        return "\n".join(parts)

    parts.append(
        "<p>Here is how your students used the tutor this week. This is engagement, "
        f"not a grade: {esc(IDENTITY.name)} is not used to grade students.</p>"
    )
    for w in weeks:
        s = w.summary
        pct = f" ({s.participation * 100:.0f}%)" if s.participation is not None else ""
        parts.append(
            f"<div style='border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin:16px 0'>"
            f"<h3 style='margin:0 0 2px'>{esc(s.label)}</h3>"
            f"<p style='color:#666;margin:0 0 10px;font-size:13px'>{esc(course_label(s.course))}</p>"
            f"<p style='margin:0 0 6px;font-size:22px;font-weight:700'>"
            f"{s.active} of {s.students}<span style='font-size:13px;font-weight:400;color:#666'>"
            f" students used the tutor{esc(pct)}</span></p>"
            "<ul style='margin:8px 0 0;padding-left:18px;font-size:14px;line-height:1.55'>"
            f"<li><b>Questions asked:</b> {s.messages} (median {s.median_messages} per student)</li>"
            f"<li><b>Not using it:</b> {esc(w.not_using_line())}</li>"
        )
        if s.reading:
            parts.append(f"<li><b>Reading only:</b> {s.reading} came to the site without asking anything</li>")
        if s.top_lectures:
            parts.append(
                "<li><b>Most asked about:</b> "
                + esc(", ".join(f"Lecture {lec} ({n})" for lec, n in s.top_lectures))
                + "</li>"
            )
        parts.append(
            f"<li><b>Exercises graded:</b> {s.exercises}"
            + (f" (avg {_dash(s.avg_score)}/4)" if s.avg_score is not None else "")
            + f" · mini-exams {s.exams_passed}/{s.exams_failed} passed/failed</li>"
            f"<li><b>Mastery:</b> {s.goals_confirmed} confirmed by spaced retrieval, "
            f"{s.goals_mastered} provisional</li>"
        )
        parts.append(f"<li><b>Feedback on the tutor:</b> {esc(w.feedback.line())}</li>")
        if w.confirmed_goals:
            parts.append(
                "<li><b>Confirmed this week:</b> "
                + esc(", ".join(f"{t} ({n})" for t, n in w.confirmed_goals))
                + "</li>"
            )
        for line in w.activity_lines():
            head, _sep, rest = line.partition(":** ")
            parts.append(f"<li><b>{esc(head.lstrip('*'))}:</b> {esc(rest)}</li>")
        parts.append("</ul>")

        flagged = w.feedback.flagged
        if flagged:
            parts.append(
                f"<p style='margin:10px 0 2px;font-size:14px'><b style='color:#b91c1c'>"
                f"Replies students flagged as wrong ({len(flagged)}):</b></p>"
                "<ul style='margin:0;padding-left:18px;font-size:13px;line-height:1.5'>"
            )
            for f in flagged[:FLAGGED_LIMIT]:
                who, when, reason, link = _flag_parts(w, f, base_url)
                parts.append(
                    f"<li style='margin:0 0 6px'>{esc(who)}, {esc(when)}: <i>{esc(reason)}</i><br>"
                    f"<span style='color:#666'>{esc(f.reply_excerpt)}</span> "
                    f"<a href='{esc(link)}'>open</a></li>"
                )
            if len(flagged) > FLAGGED_LIMIT:
                more = esc(dashboard_link(base_url, w.course_class.id)) + "#flagged"
                parts.append(f"<li>and {len(flagged) - FLAGGED_LIMIT} more "
                             f"<a href='{more}'>on the dashboard</a></li>")
            parts.append("</ul>")

        for title, rows, color in (
            ("Could not sign in", w.locked_out, "#b91c1c"),
            ("Never signed in", w.never, "#b91c1c"),
            ("Signed in, never asked", w.signed_in, "#b45309"),
            ("Went quiet", w.dormant, "#b45309"),
        ):
            if rows:
                parts.append(
                    f"<p style='margin:10px 0 0;font-size:14px'>"
                    f"<b style='color:{color}'>{title} ({len(rows)}):</b> "
                    f"{esc(_named_list(rows))}</p>"
                )
        if w.most_active:
            parts.append(
                "<p style='margin:10px 0 0;font-size:14px'><b style='color:#15803d'>Most active:</b> "
                + esc(", ".join(f"{_name(r)} ({_questions(r.messages)})" for r in w.most_active))
                + "</p>"
            )
        daily_rows = w.daily_rows()
        if daily_rows:
            headings = ["day"] + [h for _k, h in DAILY_COLUMNS]
            parts.append(
                "<table style='border-collapse:collapse;font-size:13px;margin:12px 0 0'>"
                "<thead><tr>" + "".join(
                    f"<th style='text-align:{'left' if h == 'day' else 'right'};padding:2px 8px;"
                    f"border-bottom:1px solid #e5e7eb;color:#666;font-weight:500'>{esc(h)}</th>"
                    for h in headings) + "</tr></thead><tbody>")
            for r in daily_rows:
                parts.append("<tr>" + "".join(
                    f"<td style='text-align:{'left' if h == 'day' else 'right'};padding:2px 8px'>{esc(str(r[h]))}</td>"
                    for h in headings) + "</tr>")
            parts.append("</tbody></table>")
        table = w.standings_table()
        if table:
            parts.append(
                "<p style='margin:12px 0 2px;font-size:14px'><b>Where each student stands this week</b> "
                "<span style='color:#666'>(percentile within the class; a rank, not a judgement)</span></p>"
                "<table style='border-collapse:collapse;font-size:13px'>"
                "<thead><tr>" + "".join(
                    f"<th style='text-align:{'left' if i == 0 else 'right'};padding:2px 8px;"
                    f"border-bottom:1px solid #e5e7eb;color:#666;font-weight:500'>{esc(h)}</th>"
                    for i, h in enumerate(table[0])) + "</tr></thead><tbody>")
            for cells in table[1:]:
                parts.append("<tr>" + "".join(
                    f"<td style='text-align:{'left' if i == 0 else 'right'};padding:2px 8px'>{esc(c)}</td>"
                    for i, c in enumerate(cells)) + "</tr>")
            parts.append("</tbody></table>")
        if w.attachment:
            parts.append(f"<p style='margin:10px 0 0;font-size:13px;color:#666'>Attached: "
                         f"<b>{esc(w.attachment[0])}</b> — daily charts; open it in a browser.</p>")
        link = dashboard_link(base_url, w.course_class.id)
        parts.append(
            f"<p style='margin:14px 0 0'><a href='{esc(link)}' "
            "style='background:#1d4ed8;color:#fff;text-decoration:none;padding:8px 14px;"
            "border-radius:8px;font-size:14px;display:inline-block'>Open this class</a></p>"
            "</div>"
        )

    me = esc(base_url.rstrip("/") + "/instructor")
    parts.append(
        "<p style='color:#666;font-size:12px;margin-top:18px'>You receive this because you "
        f"teach a class in {esc(IDENTITY.name)}. Turn it off on <a href='{me}'>your instructor page</a> "
        "(\"Weekly email\"); an admin can also silence one class (Admin → Classes).</p></div>"
    )
    return "\n".join(parts)
