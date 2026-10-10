"""The analysis commands of ``python -m tutor.analytics`` (phase 3 of
the analytics plan). ``add_commands`` registers them on
the parser's subcommands; ``export`` lives in ``__main__``.

    visits         --student S   one student's visits, with on-screen, deck and narration time
    lectures       --class C     coverage, dwell, narration per student and lecture; a class table
    questions      --student S | --class C   gaps, think time, buckets, follow-up and abandonment
    disengagement  --class C | --student S   where each visit's last turn left off, and what came next
    timeline       --student S --day D       one student's day, signal by signal
    student        S             the metric table for one student, with class percentiles
    class          C             the metric table for a roster, its summary, and the weekly trend
    compare        C1 C2 ...     classes side by side
    daily          [--class C]   one value per calendar day for every metric; --format html is a page of charts

Every command takes --db, a window (--days N, or --since/--until), --format
md|csv|json|html, and the thresholds as flags; each report prints the ones it
used. A student is an id, a pseudonym (S12), or an address in a store made
with --with-emails.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from typing import Optional

import webbrowser
from pathlib import Path
from zoneinfo import ZoneInfo

from tutor.analytics import cohort, daily, daily_page, lectures as lectures_mod, questions as questions_mod
from tutor.analytics import topics as topics_mod, visits as visits_mod
from tutor.analytics.base import Store, Thresholds, Window, window_days
from tutor.analytics.render import FORMATS, Report, render
from tutor.courses import COURSES, course_label

DEFAULT_STORE = "tutor-analytics.sqlite"


# ---------------------------------------------------------------------------
# Shared arguments
# ---------------------------------------------------------------------------

def _common(parser: argparse.ArgumentParser, *, student: bool = False, klass: bool = False,
            required_student: bool = False) -> None:
    parser.add_argument("--db", default=DEFAULT_STORE, help=f"the local store (default {DEFAULT_STORE})")
    parser.add_argument("--days", type=int, help="the last N days")
    parser.add_argument("--since", help="YYYY-MM-DD")
    parser.add_argument("--until", help="YYYY-MM-DD (exclusive)")
    parser.add_argument("--format", choices=FORMATS, default="md")
    if student:
        parser.add_argument("--student", required=required_student, help="id, pseudonym (S12), or address")
    if klass:
        parser.add_argument("--class", "--class-id", dest="class_id", type=int,
                            help="a class id (see `class` for the list)")
    t = Thresholds()
    parser.add_argument("--idle-gap", type=float, default=None, metavar="MIN", help=f"visit idle gap (default {t.idle_gap_min:g})")
    parser.add_argument("--follow-up", type=float, default=None, metavar="S", help=f"follow-up think time (default {t.follow_up_s:g})")
    parser.add_argument("--reading-max", type=float, default=None, metavar="MIN", help=f"reading up to (default {t.reading_max_min:g})")
    parser.add_argument("--read-slide", type=float, default=None, metavar="S", help=f"a slide read at (default {t.read_slide_s:g})")
    parser.add_argument("--coverage", type=float, default=None, metavar="PCT", help=f"a lecture covered at (default {t.coverage_pct:g})")
    parser.add_argument("--listened", type=float, default=None, metavar="PCT", help=f"a lecture listened at (default {t.listened_pct:g})")
    parser.add_argument("--left-after", type=float, default=None, metavar="MIN", help=f"left after (default {t.left_after_min:g})")
    parser.add_argument("--heartbeat", type=int, default=None, metavar="S", help=f"the pages' heartbeat (default {t.heartbeat_s})")


def _thresholds(args: argparse.Namespace) -> Thresholds:
    return Thresholds().with_overrides(
        idle_gap_min=args.idle_gap, follow_up_s=args.follow_up, reading_max_min=args.reading_max,
        read_slide_s=args.read_slide, coverage_pct=args.coverage, listened_pct=args.listened,
        left_after_min=args.left_after, heartbeat_s=args.heartbeat,
    )


def _window(args: argparse.Namespace) -> Window:
    return window_days(args.days, args.since, args.until)


def _context(store: Store, window: Window, thresholds: Thresholds) -> list[str]:
    exported = store.one("SELECT value FROM export_meta WHERE key = 'last_export'")
    lines = [f"Window: {window.label()}.", f"Thresholds: {thresholds.describe()}."]
    if exported:
        lines.append(f"Store: {store.path} (exported {str(exported['value']).strip(chr(34))[:19]}).")
    return lines


def _emit(report: Report, fmt: str) -> int:
    sys.stdout.write(render(report, fmt))
    return 0


def _students_for(store: Store, args: argparse.Namespace) -> tuple[list[int], str, Optional[str]]:
    """The students a command works on: one student, a class's roster, or
    everyone; with a title fragment and the course (for a class)."""
    if getattr(args, "student", None):
        sid = store.resolve_student(args.student)
        return [sid], store.student_label(sid), None
    if getattr(args, "class_id", None):
        return store.roster(args.class_id), store.class_label(args.class_id), store.class_course(args.class_id)
    return store.all_students(), "all students", None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_visits(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds, window = _thresholds(args), _window(args)
    ids, who, _course = _students_for(store, args)
    report = Report(f"Visits: {who}", _context(store, window, thresholds))
    by_student = visits_mod.visits_for(store, ids, window, thresholds)
    rows = []
    for sid in ids:
        for v in by_student.get(sid, []):
            rows.append({"student": store.student_label(sid), **v.as_dict()})
    for r in rows:
        r.pop("student_id", None)
    report.add("Visits", rows)
    totals = visits_mod.summarize_visits([v for sid in ids for v in by_student.get(sid, [])])
    report.add("Totals", [totals])
    return _emit(report, args.format)


def cmd_lectures(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds, window = _thresholds(args), _window(args)
    ids, who, course = _students_for(store, args)
    course = args.course or course
    uses = lectures_mod.lecture_use(store, ids, window, course=course, lecture=args.lecture)
    report = Report(f"Lectures: {who}" + (f", {course_label(course)}" if course else ""), _context(store, window, thresholds))
    labels = {sid: store.student_label(sid) for sid in ids}
    if getattr(args, "class_id", None) and course:
        numbers = sorted({n for (_s, c, n) in uses if c == course})
        if args.lecture:
            numbers = [args.lecture]
        report.add("By lecture", lectures_mod.class_lecture_table(uses, ids, course, thresholds, numbers))
        report.add("Roster x lecture (slides seen % / narration %)",
                   lectures_mod.student_lecture_matrix(uses, ids, course, labels, numbers))
    detail = []
    for (sid, _c, _n), u in sorted(uses.items()):
        row = u.as_dict(thresholds)
        row["student"] = labels[sid]
        row.pop("student_id", None)
        detail.append(row)
    report.add("Per student and lecture", detail)
    return _emit(report, args.format)


def cmd_questions(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds, window = _thresholds(args), _window(args)
    ids, who, course = _students_for(store, args)
    course = args.course or course
    gaps = questions_mod.question_gaps(store, ids, window, thresholds, course=course)
    abandoned = questions_mod.abandoned_turns(store, ids, window)
    report = Report(f"Questions: {who}", _context(store, window, thresholds))
    summary_rows = []
    for sid in ids:
        s = questions_mod.summarize_questions(gaps.get(sid, []), abandoned.get(sid))
        summary_rows.append({"student": store.student_label(sid), **s})
    report.add("Per student", summary_rows)
    everyone = [g for sid in ids for g in gaps.get(sid, [])]
    all_abandoned = {"sent": sum(a["sent"] for a in abandoned.values()),
                     "abandoned": sum(a["abandoned"] for a in abandoned.values())}
    report.add("All together", [questions_mod.summarize_questions(everyone, all_abandoned)])
    if len(ids) == 1:
        rows = [g.as_dict() for g in everyone]
        for r in rows:
            r.pop("student_id", None)
        report.add("Each question", rows)
    return _emit(report, args.format)


def cmd_disengagement(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds, window = _thresholds(args), _window(args)
    ids, who, _course = _students_for(store, args)
    points = topics_mod.disengagement_points(store, ids, window, thresholds)
    report = Report(f"Disengagement points: {who}", _context(store, window, thresholds))
    rows = []
    for sid in ids:
        for d in points.get(sid, []):
            row = d.as_dict()
            row = {"student": store.student_label(sid), **row}
            row.pop("student_id", None)
            rows.append(row)
    report.add("Each visit's last turn", rows)
    n = len(rows)
    left = sum(1 for r in rows if r["left"])
    after: dict[str, int] = {}
    for r in rows:
        after[r["after"]] = after.get(r["after"], 0) + 1
    report.add("Summary", [{"visits with a question": n, "left within the threshold": left,
                            "left share": round(left / n, 2) if n else None,
                            **{f"after: {k}": v for k, v in sorted(after.items())}}])
    turns = topics_mod.turns_labeled(store, ids, window)
    titles = store.goal_titles()
    sw = [{"student": store.student_label(sid), **s} for sid in ids
          for s in topics_mod.switches(turns.get(sid, []), thresholds, titles)]
    for s in sw:
        s.pop("student_id", None)
    report.add("Topic switches inside a visit", sw)
    return _emit(report, args.format)


def cmd_timeline(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds = _thresholds(args)
    sid = store.resolve_student(args.student)
    try:
        day = datetime.date.fromisoformat(args.day)
    except ValueError:
        print(f"--day wants YYYY-MM-DD, not {args.day!r}", file=sys.stderr)
        return 2
    report = Report(f"Timeline: {store.student_label(sid)} on {day.isoformat()}",
                    [f"Thresholds: {thresholds.describe()}."])
    report.add("Signals", topics_mod.timeline(store, sid, day, thresholds))
    return _emit(report, args.format)


def cmd_student(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds, window = _thresholds(args), _window(args)
    sid = store.resolve_student(args.student)
    data = cohort.student_report(store, sid, window, thresholds)
    report = Report(f"Student: {store.student_label(sid)}", _context(store, window, thresholds))
    row = data["row"].as_dict()
    report.add("Metrics", [{"metric": h, "value": row.get(k), "meaning": m} for k, h, m in cohort.METRICS],
               columns=["metric", "value", "meaning"])
    report.add("Percentile within each class", data["percentiles"])
    visits = visits_mod.visits_for(store, [sid], window, thresholds).get(sid, [])
    rows = [v.as_dict() for v in visits]
    for r in rows:
        r.pop("student_id", None)
    report.add("Visits", rows)
    return _emit(report, args.format)


def cmd_class(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds, window = _thresholds(args), _window(args)
    if args.class_id is None:
        rows = store.rows("SELECT c.id, c.name, c.term, c.course, c.archived, "
                          "(SELECT COUNT(*) FROM class_enrollments e WHERE e.class_id = c.id) AS enrolled "
                          "FROM classes c ORDER BY c.id")
        report = Report("Classes", [])
        report.add("Classes in the store", rows)
        return _emit(report, args.format)
    summary = cohort.class_summary(store, args.class_id, window, thresholds)
    report = Report(f"Class: {summary.label}", _context(store, window, thresholds))
    report.add("Summary", [{"enrolled": summary.n_enrolled, "active": summary.n_active,
                            "never active": ", ".join(summary.never_active) or "nobody"}])
    report.add("Metrics over the roster", summary.summary,
               columns=["metric", "n", "median", "q1", "q3", "mean", "meaning"])
    rows = [r.as_dict() for r in sorted(summary.rows, key=lambda r: -r.metrics["active_min"])]
    report.add("Each student", rows)
    report.add("By week", cohort.weekly_trend(store, summary.roster, window, thresholds))
    return _emit(report, args.format)


def cmd_daily(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds = _thresholds(args)
    window = window_days(None, args.since, args.until)       # --days is the span, not a cutoff
    tz = ZoneInfo(args.tz)
    if args.class_id:
        ids, scope = store.roster(args.class_id), store.class_label(args.class_id)
    elif args.instructors:
        ids = [int(r["id"]) for r in store.rows("SELECT id FROM students ORDER BY id")]
        scope = "everyone, instructors included"
    else:
        ids, scope = store.all_students(), "all students"
    table = daily.daily_table(store, ids, window, thresholds, tz, days=args.days or daily.DEFAULT_DAYS)
    exported = store.one("SELECT value FROM export_meta WHERE key = 'last_export'")
    context = [f"Daily totals for {scope} ({table.students} accounts), {table.days[0]} to {table.days[-1]}, "
               f"{table.tz} calendar days.", f"Thresholds: {thresholds.describe()}."]
    title = f"Daily use: {scope}"
    if args.format == "html":
        text = daily_page.render(table, title, context,
                                 str(exported["value"]).strip(chr(34))[:16].replace("T", " ") + "Z" if exported else "")
    else:
        report = Report(title, context + ([f"Store: {store.path}."]))
        report.add("Totals", table.totals())
        report.add("Per day", table.as_rows(), columns=table.columns())
        text = render(report, args.format)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
        if args.open:
            webbrowser.open(out.resolve().as_uri())
    else:
        sys.stdout.write(text)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    store = Store(args.db)
    thresholds, window = _thresholds(args), _window(args)
    data = cohort.compare_classes(store, args.class_ids, window, thresholds)
    labels = ", ".join(s.label for s in data["summaries"])
    report = Report(f"Classes compared: {labels}", _context(store, window, thresholds))
    report.add("Side by side (median over the roster; q1..q3 beneath)", data["table"])
    for s in data["summaries"]:
        rows = [r.as_dict() for r in sorted(s.rows, key=lambda r: -r.metrics["active_min"])]
        report.add(f"{s.label}: each student", rows)
    return _emit(report, args.format)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def add_commands(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("visits", help="one student's visits (or a class's, or everyone's)")
    _common(p, student=True, klass=True)
    p.set_defaults(func=cmd_visits)

    p = sub.add_parser("lectures", help="slides seen, dwell, narration per student and lecture")
    _common(p, student=True, klass=True)
    p.add_argument("--course", choices=tuple(COURSES))
    p.add_argument("--lecture", type=int)
    p.set_defaults(func=cmd_lectures)

    p = sub.add_parser("questions", help="the time between questions")
    _common(p, student=True, klass=True)
    p.add_argument("--course", choices=tuple(COURSES))
    p.set_defaults(func=cmd_questions)

    p = sub.add_parser("disengagement", help="where each visit's last turn left off")
    _common(p, student=True, klass=True)
    p.set_defaults(func=cmd_disengagement)

    p = sub.add_parser("timeline", help="one student's day, signal by signal")
    _common(p, student=True, required_student=True)
    p.add_argument("--day", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("student", help="one student's metrics and class percentiles")
    _common(p)
    p.add_argument("student", help="id, pseudonym (S12), or address")
    p.set_defaults(func=cmd_student)

    p = sub.add_parser("class", help="a class's metric table, summary, and weekly trend (no id: list classes)")
    _common(p)
    p.add_argument("class_id", nargs="?", type=int)
    p.set_defaults(func=cmd_class)

    p = sub.add_parser("daily", help="one value per calendar day for every metric (html: a page of charts)")
    _common(p, klass=True)
    p.add_argument("--tz", default=daily.DEFAULT_TZ, help=f"the calendar the days follow (default {daily.DEFAULT_TZ})")
    p.add_argument("--instructors", action="store_true", help="count instructor accounts too")
    p.add_argument("--out", help="write to this file instead of stdout")
    p.add_argument("--open", action="store_true", help="with --out: open the file in the browser")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("compare", help="classes side by side")
    _common(p)
    p.add_argument("class_ids", nargs="+", type=int)
    p.set_defaults(func=cmd_compare)
