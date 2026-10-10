#!/usr/bin/env python3
"""Email the admin who hit a tutor quota (tutor/quotas.py), per period.

    python -m scripts.quota_report                     # the periods due today and switched on
    python -m scripts.quota_report --period weekly     # one period, whatever the settings say
    python -m scripts.quota_report --period daily --no-email

Read-only against the configured database (DATABASE_URL, so the workflow
points it at production). Which periods go out is an admin setting on the
Supervision tab (daily, weekly on Monday, monthly on the 1st, all in the class's
local time zone);
a period with no hits sends nothing. The gather and the rendering live in
tutor/quota_report.py; this file only does IO.
"""

from __future__ import annotations

import argparse
import os


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Quota roll-up email")
    p.add_argument("--period", choices=("daily", "weekly", "monthly"),
                   help="send this period now (default: the periods due today that are switched on)")
    p.add_argument("--email", help="recipient (default: REPORT_EMAIL or first ADMIN_EMAILS)")
    p.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:8000"),
                   help="public app URL for the link (default: $BASE_URL)")
    p.add_argument("--no-email", action="store_true", help="print only, do not send")
    args = p.parse_args(argv)

    from tutor import mail, quota_report
    from tutor.db.models import get_engine, get_session_factory
    from tutor.db.target import print_target

    engine = get_engine()
    print_target(engine)
    db = get_session_factory(engine)()
    try:
        periods = quota_report.due_periods(db, force=args.period)
        if not periods:
            print("quota_report: no roll-up is due today (or all are switched off).")
            return 0
        for period in periods:
            roll = quota_report.gather(db, period)
            print(f"quota_report: {period}, {roll.label}: {len(roll.students)} student(s), {roll.total_hits} hit(s)")
            if not roll.students:
                continue
            text = quota_report.render_markdown(roll, args.base_url)
            if args.no_email:
                print(text)
                continue
            mail.send_report(mail.report_recipient(args.email), roll.subject, text=text,
                             html=quota_report.render_html(roll, args.base_url), tag="quota_report")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
