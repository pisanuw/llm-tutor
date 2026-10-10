"""Email the instructor a thumbs-down report, only on days with thumbs-downs.

Runs after the daily digest in .github/workflows/daily-report.yml (same
DATABASE_URL / RESEND_API_KEY / REPORT_EMAIL secrets). Looks back ``--hours``
(default 24) or at one local ``--date`` (``tutor.timezone`` in courses.yaml); if no
reply was flagged, prints one line and sends nothing.

    python -m scripts.feedback_report                 # last 24h, email if any
    python -m scripts.feedback_report --date 2026-09-03 --no-email
"""
from __future__ import annotations

import argparse
import datetime
import os


def _window(args) -> tuple[datetime.datetime, datetime.datetime, str]:
    """``--date`` is a calendar day in the class's time zone; the label names
    both endpoints in local time (plan step 4.4, finding B23)."""
    from tutor.utctime import report_window, window_label

    since, until = report_window(args.date, args.hours)
    return since, until, window_label(since, until)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Daily thumbs-down feedback report")
    p.add_argument("--date", help="calendar day in local time (YYYY-MM-DD); default is a rolling window")
    p.add_argument("--hours", type=int, default=24, help="rolling window size when --date is absent")
    p.add_argument("--email", help="recipient (default: REPORT_EMAIL or first ADMIN_EMAILS)")
    p.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:8000"),
                   help="public app URL used for the conversation links (default: $BASE_URL)")
    p.add_argument("--no-email", action="store_true", help="print only, do not send")
    args = p.parse_args(argv)

    from tutor import mail
    from tutor.courses import IDENTITY
    from tutor.db.models import get_engine, get_session_factory
    from tutor.feedback_report import (
        render_report_html, render_report_markdown, thumbs_down_in_window,
    )

    since, until, label = _window(args)
    db = get_session_factory(get_engine())()
    try:
        flags = thumbs_down_in_window(db, since, until)
    finally:
        db.close()

    print(f"feedback_report: window {since:%Y-%m-%d %H:%M}..{until:%Y-%m-%d %H:%M} UTC, "
          f"{len(flags)} thumbs-down")
    if not flags:
        return 0

    text = render_report_markdown(flags, label, args.base_url)
    html = render_report_html(flags, label, args.base_url)
    if args.no_email:
        print(text)
        return 0
    n = len(flags)
    mail.send_report(mail.report_recipient(args.email),
                     f"{IDENTITY.name}: {n} thumbs-down repl{'y' if n == 1 else 'ies'} ({label})",
                     text=text, html=html, tag="feedback_report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
