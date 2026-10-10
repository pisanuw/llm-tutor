#!/usr/bin/env python3
"""Daily per-user activity report -> markdown files + an emailed digest.

Read-only against the configured DB (honours ``DATABASE_URL``, so CI can point it
at prod). Writes one markdown file per active student plus ``digest.md`` under
``<out>/<date>/``, and emails the digest to ``REPORT_EMAIL`` (or the first
``ADMIN_EMAILS``) when a Resend key is set. The aggregation lives in
``tutor/reporting.py`` (pure, tested); this file only does IO.

Usage:
    python -m scripts.daily_report                      # last 24h, write + email
    python -m scripts.daily_report --hours 24 --no-email
    python -m scripts.daily_report --date 2026-07-17 --out reports
"""

from __future__ import annotations

import argparse
import datetime
import html as _html
import os
from pathlib import Path
from typing import Optional


def _window(args) -> tuple[datetime.datetime, datetime.datetime, str, str]:
    """``(since, until, label, folder)``. ``--date`` is a calendar day in the
    class's time zone (``tutor.timezone`` in courses.yaml); the label names
    both endpoints in local time, and the output folder is the local date the
    window starts on (plan step 4.4, finding B23: the UTC date named the wrong
    day)."""
    from tutor.utctime import report_window, to_local, window_label

    since, until = report_window(args.date, args.hours)
    return since, until, window_label(since, until), args.date or f"{to_local(since):%Y-%m-%d}"


def _email_digest(to: Optional[str], label: str, digest: str, n_active: int) -> None:
    """Email the digest (tutor.mail), or print it when email is not configured.
    The files are already written, so a failed send loses nothing."""
    from tutor import mail
    from tutor.courses import IDENTITY

    mail.send_report(
        mail.report_recipient(to), f"{IDENTITY.name} daily report: {label} ({n_active} active)",
        text=digest,
        html=("<pre style='font-family:ui-monospace,monospace;font-size:13px;"
              f"white-space:pre-wrap'>{_html.escape(digest)}</pre>"),
        tag="daily_report",
    )


def _email_quiet(to: Optional[str], label: str) -> None:
    """The one-line note for a day with nothing in it: the job ran."""
    from tutor import mail
    from tutor.courses import IDENTITY

    text = f"Quiet day: no student activity and no learning signals in the window {label}."
    mail.send_report(
        mail.report_recipient(to), f"{IDENTITY.name} daily report: {label} (quiet day)",
        text=text, html=f"<p>{_html.escape(text)}</p>", tag="daily_report",
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Daily per-user activity report")
    p.add_argument("--date", help="calendar day in local time (YYYY-MM-DD); default is a rolling window")
    p.add_argument("--hours", type=int, default=24, help="rolling window size when --date is absent")
    p.add_argument("--out", default="reports", help="output directory root (default: reports/)")
    p.add_argument("--email", help="digest recipient (default: REPORT_EMAIL or first ADMIN_EMAILS)")
    p.add_argument("--no-email", action="store_true", help="write files only, do not send")
    p.add_argument("--always-email", action="store_true",
                   help="send even on a quiet day (no activity, no learning signals)")
    args = p.parse_args(argv)

    from tutor.db.models import (
        MasteryAttempt, Message, Student, TurnEvent, get_engine, get_session_factory,
    )
    from tutor.logging_config import slow_turn_ms
    from tutor import login_digest, request_errors
    from tutor.reporting import (
        build_user_dailies, render_digest_markdown, render_errors_markdown, render_user_markdown,
        safe_filename,
    )

    since, until, label, folder = _window(args)
    engine = get_engine()
    try:
        db_desc = engine.url.render_as_string(hide_password=True)
    except Exception:
        db_desc = str(engine.url)
    print(f"daily_report: window {since:%Y-%m-%d %H:%M}..{until:%Y-%m-%d %H:%M} UTC  db={db_desc}")

    db = get_session_factory(engine)()
    try:
        students = db.query(Student).all()
        messages = (db.query(Message)
                    .filter(Message.created_at >= since, Message.created_at < until).all())
        turn_events = (db.query(TurnEvent)
                       .filter(TurnEvent.ts >= since, TurnEvent.ts < until).all())
        attempts = (db.query(MasteryAttempt)
                    .filter(MasteryAttempt.completed_at.isnot(None),
                            MasteryAttempt.completed_at >= since,
                            MasteryAttempt.completed_at < until).all())
        # Learning signals: mastery velocity, 2-week retention,
        # calibration error. Never let a metrics bug kill the usage report.
        metrics = None
        try:
            from tutor.learning_metrics import compute_learning_metrics, render_learning_markdown
            metrics = compute_learning_metrics(db, since, until)
            learning_md = render_learning_markdown(metrics)
        except Exception as exc:  # pragma: no cover - defensive
            learning_md = f"## Learning signals\n\n_metrics unavailable: {exc}_\n"
        # Dead links and error pages (review step 0.1): a day with any is not quiet.
        try:
            errors = request_errors.summary(db, since, until)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"daily_report: request errors unavailable: {exc}")
            errors = None
        # Sign-in problems (review step 0.6): likewise.
        try:
            logins = login_digest.summary(db, since, until)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"daily_report: login summary unavailable: {exc}")
            logins = None
    finally:
        db.close()

    reports = build_user_dailies(students, messages, turn_events, attempts, slow_ms=slow_turn_ms())

    out_dir = Path(args.out) / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    for u in reports:
        (out_dir / f"{safe_filename(u.email)}.md").write_text(render_user_markdown(u, label))
    digest = render_digest_markdown(reports, label, learning_md=learning_md,
                                    base_url=os.getenv("BASE_URL", "").strip())
    errors_md = render_errors_markdown(errors)
    if errors_md:
        digest = digest.rstrip("\n") + "\n\n" + errors_md
    logins_md = login_digest.render_markdown(logins)
    if logins_md:
        digest = digest.rstrip("\n") + "\n\n" + logins_md
    (out_dir / "digest.md").write_text(digest)
    print(f"daily_report: wrote {len(reports)} user file(s) + digest.md to {out_dir}")

    if args.no_email:
        print("daily_report: --no-email set, skipping send\n")
        print(digest)
        return 0
    if is_quiet_day(reports, metrics, errors, logins) and not args.always_email:
        # No students, no turns, no learning signals: an email of zeros is
        # noise, but silence is ambiguous (a crashed run looks the same), so
        # one line goes out instead (student-facing review, decision 10).
        # Files are still written.
        print(f"daily_report: quiet day ({label}), nothing to report; sending the one-line note "
              "(use --always-email for the full digest)")
        _email_quiet(args.email, label)
        return 0
    _email_digest(args.email, label, digest, len(reports))
    return 0


def is_quiet_day(reports, metrics, errors=None, logins=None) -> bool:
    """True when the window has no student activity, no learning signals, no
    error a student's browser hit and no sign-in problem. Errors and slow
    turns inside a turn belong to a student's report, so an empty
    ``reports`` already implies none of those either."""
    if reports:
        return False
    if errors is not None and not errors.empty:
        return False
    if logins is not None and not logins.empty:
        return False
    if metrics is None:  # metrics failed: send, so the failure note is seen
        return False
    return (
        metrics.goals_confirmed == 0 and metrics.goals_mastered == 0
        and metrics.predictions == 0 and metrics.retention_reviews == 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
