"""Email each instructor a weekly report on how their students are using the tutor.

Read-only against the configured DB (honours ``DATABASE_URL``). One email per
instructor who has not switched it off, covering every class they own that is
active, has students, has its weekly_report switch on, is inside its quarter
dates today, and had any activity in the window. The aggregation and
rendering live in ``tutor/instructor_report.py`` (pure, tested); this file
only does IO.

The admin (``REPORT_EMAIL``, else the first ``ADMIN_EMAILS``) gets a blind
copy of every email sent, unless ``--no-admin-copy``. A class nobody touched
sends nothing (``--quiet-too`` restores the old "nobody used it" email). With ``--daily-store`` (the local analytics store
from ``python -m tutor.analytics export``) each class also gets a day-by-day
table in the body and its daily chart page as an attachment.
.github/workflows/instructor-report.yml sends the plain report weekly; for the
day-by-day table, run the export and then this script with ``--daily-store``
from a scheduled job (cron, launchd) on the machine that keeps the store.

Usage:
    python -m scripts.instructor_report                      # last 7 days, send
    python -m scripts.instructor_report --days 7 --no-email  # print instead
    python -m scripts.instructor_report --instructor instructor@example.edu --no-email
    python -m scripts.instructor_report --to instructor@example.edu   # send all to one box
    python -m scripts.instructor_report --daily-store ~/tutor-data/tutor.sqlite
"""

from __future__ import annotations

import argparse
import datetime
import os
from typing import Optional


def _window(days: int) -> tuple[datetime.datetime, datetime.datetime]:
    from tutor.utctime import report_window

    return report_window(None, days * 24)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Weekly per-instructor status report")
    p.add_argument("--days", type=int, default=7, help="look-back window (default 7)")
    p.add_argument("--instructor", help="only this instructor's email")
    p.add_argument("--to", help="send every report to this address instead (testing)")
    p.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:8000"),
                   help="public app URL used for the dashboard links (default: $BASE_URL)")
    p.add_argument("--no-email", action="store_true", help="print the reports, send nothing")
    p.add_argument("--daily-store", help="the local analytics store: adds the day-by-day table and the chart page")
    p.add_argument("--tz", default=None,
                   help="the calendar the day-by-day table follows (default: tutor.timezone in courses.yaml)")
    p.add_argument("--quiet-too", action="store_true", help="also email classes with no activity in the window")
    p.add_argument("--no-admin-copy", action="store_true",
                   help="no blind copy to the admin (REPORT_EMAIL, else the first ADMIN_EMAILS)")
    args = p.parse_args(argv)
    if not args.tz:
        from tutor.courses import IDENTITY

        args.tz = IDENTITY.timezone

    from tutor import instructor_report as report
    from tutor import mail
    from tutor.db.models import get_engine, get_session_factory

    since, until = _window(max(1, args.days))
    label = report.window_label(since, until)

    engine = get_engine()
    db = get_session_factory(engine)()
    sent = skipped = 0
    try:
        instructors = report.eligible_instructors(db)
        if args.instructor:
            want = args.instructor.strip().lower()
            instructors = [i for i in instructors if i.email.lower() == want]
            if not instructors:
                print(f"instructor_report: {want} has no reportable classes")
                return 0
        admin_copy = None if args.no_admin_copy else mail.report_recipient()
        print(f"instructor_report: window {since:%Y-%m-%d}..{until:%Y-%m-%d}, "
              f"{len(instructors)} instructor(s)" + (f", copies to {admin_copy}" if admin_copy else ""))

        for instructor in instructors:
            weeks = report.gather(db, instructor, since, until, include_quiet=args.quiet_too)
            if not weeks:
                skipped += 1
                continue
            if args.daily_store:
                from pathlib import Path

                report.attach_daily(weeks, Path(args.daily_store).expanduser(), since, until, args.tz)
            text = report.render_markdown(instructor, weeks, label, args.base_url)
            html = report.render_html(instructor, weeks, label, args.base_url)
            subject = report.subject(weeks, label)
            if args.no_email:
                print(f"\n=== {instructor.email} — {subject}\n")
                print(text)
                continue
            recipient: Optional[str] = args.to or instructor.email
            attachments = [w.attachment for w in weeks if w.attachment]
            if mail.send_report(recipient, subject, text=text, html=html, tag="instructor_report",
                                attachments=attachments, bcc=admin_copy):
                sent += 1
    finally:
        db.close()

    if not args.no_email:
        print(f"instructor_report: sent {sent}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
