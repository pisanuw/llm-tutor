#!/usr/bin/env python3
"""Who signed in, who could not, and how: the sign-in trail in a terminal.

    python -m scripts.login_report                   # the last 3 days, local time, oldest first
    python -m scripts.login_report --days 14 --failed
    python -m scripts.login_report --student ada     # rows whose address contains "ada"
    python -m scripts.login_report --since 2026-10-01 --all   # every step, the in-progress ones too
    python -m scripts.login_report --follow          # then keep printing new rows (Ctrl-C stops)

Read-only against the configured database: DATABASE_URL in .env, so production
(the target is printed on stderr first). The rows are the ``login_attempts``
table that Admin → Logins shows (tutor.auth.attempts): one per step of a
sign-in. By default only the steps that ended something are listed, a session
issued (``ok``) or a failure; the steps in between (a magic link sent, a Google
consent screen opened) are counted on one trailing line when nothing followed
them, and listed with ``--all``.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from typing import Iterable, Optional

from tutor.auth import attempts
from tutor.db.models import LoginAttempt
from tutor.utctime import local_day, to_local, utcnow

DEFAULT_DAYS = 3
STARTS = frozenset({attempts.STARTED, attempts.LINK_SENT, attempts.LINK_SHOWN})
TERMINAL = frozenset({attempts.OK}) | attempts.FAILED


def window(days: int, since: Optional[str]) -> tuple[datetime.datetime, datetime.datetime]:
    """The naive-UTC ``[since, until)``: a local calendar day on, or the last ``days``."""
    until = utcnow()
    if since:
        return local_day(since)[0], until
    return until - datetime.timedelta(days=days), until


def select_rows(db, since: Optional[datetime.datetime], needle: str = "",
                after_id: int = 0) -> list[LoginAttempt]:
    """The trail oldest first, from ``since`` or after row ``after_id``."""
    q = db.query(LoginAttempt)
    if since is not None:
        q = q.filter(LoginAttempt.created_at >= since)
    if after_id:
        q = q.filter(LoginAttempt.id > after_id)
    if needle:
        q = q.filter(LoginAttempt.email.ilike(f"%{needle}%"))
    return q.order_by(LoginAttempt.created_at.asc(), LoginAttempt.id.asc()).all()


def visible(rows: Iterable[LoginAttempt], *, failed_only: bool = False,
            everything: bool = False) -> list[LoginAttempt]:
    if failed_only:
        return [a for a in rows if a.outcome in attempts.FAILED]
    if everything:
        return list(rows)
    return [a for a in rows if a.outcome in TERMINAL]


def unfinished(rows: Iterable[LoginAttempt]) -> dict[str, int]:
    """Starts nothing followed: a magic link sent (or shown) with no later
    ``ok`` or failure for that address, a Google consent screen opened with no
    later Google result. Each result settles the oldest open start, since a
    Google start carries no address yet."""
    open_starts: dict[tuple[str, str], int] = {}
    for a in rows:
        key = (a.method, (a.email or "") if a.method == attempts.MAGIC else "")
        if a.outcome in STARTS:
            open_starts[key] = open_starts.get(key, 0) + 1
        elif a.outcome in TERMINAL and open_starts.get(key):
            open_starts[key] -= 1
    counts = {attempts.MAGIC: 0, attempts.GOOGLE: 0}
    for (method, _), n in open_starts.items():
        counts[method] = counts.get(method, 0) + n
    return counts


def _clip(value: Optional[str], width: int) -> str:
    text = (value or "").strip()
    if len(text) <= width:
        return text
    return text[:width - 3] + "..."


def format_row(a: LoginAttempt) -> str:
    when = to_local(a.created_at).strftime("%a %b %d %H:%M")
    return (f"{when}  {a.outcome:<16}{a.method:<8}{a.email or '?':<34}"
            f"{_clip(a.detail, 24):<26}{_clip(a.user_agent, 60)}").rstrip()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def render(rows: list[LoginAttempt], since: datetime.datetime, until: datetime.datetime, *,
           failed_only: bool = False, everything: bool = False, needle: str = "") -> str:
    """The report: the window, the outcome counts, one line per row, the open starts."""
    fmt = "%a %b %d %H:%M"
    scope = f" for addresses containing {needle!r}" if needle else ""
    lines = [f"Sign-ins {to_local(since).strftime(fmt)} to {to_local(until).strftime(fmt)}"
             f" local time{scope}"]
    if not rows:
        lines.append("Nothing happened.")
        return "\n".join(lines)
    counts: dict[str, int] = {}
    for a in rows:
        counts[a.outcome] = counts.get(a.outcome, 0) + 1
    order = [attempts.OK, *sorted(attempts.FAILED), *sorted(attempts.IN_PROGRESS)]
    lines.append("  ".join(f"{o} {counts[o]}" for o in order if o in counts))
    shown = visible(rows, failed_only=failed_only, everything=everything)
    lines.append("")
    lines.extend(format_row(a) for a in shown)
    if not shown:
        lines.append("(no failures)" if failed_only else "(no completed sign-ins or failures)")
    if not failed_only:
        pending = unfinished(rows)
        parts = []
        if pending[attempts.MAGIC]:
            parts.append(_plural(pending[attempts.MAGIC], "magic link"))
        if pending[attempts.GOOGLE]:
            parts.append(_plural(pending[attempts.GOOGLE], "Google sign-in"))
        if parts:
            lines.append("")
            lines.append(f"Started and not completed: {', '.join(parts)}.")
    return "\n".join(lines)


def follow(factory, last_id: int, needle: str, interval: float, *, failed_only: bool,
           everything: bool) -> None:
    """Print every new row as it lands, polling every ``interval`` seconds."""
    print(f"-- following; new rows every {interval:g} s, Ctrl-C to stop --", file=sys.stderr)
    started = utcnow()   # an empty window has no last id: then only rows from now on
    while True:
        time.sleep(interval)
        db = factory()
        try:
            rows = select_rows(db, None if last_id else started, needle, after_id=last_id)
        except Exception as exc:   # a dropped pooler connection: say so, try again next tick
            print(f"poll failed ({type(exc).__name__}: {exc}); retrying", file=sys.stderr)
            continue
        finally:
            db.close()
        for a in visible(rows, failed_only=failed_only, everything=everything):
            print(format_row(a), flush=True)
        if rows:
            last_id = max(a.id for a in rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="The sign-in trail (login_attempts) in a terminal")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"how far back (default {DEFAULT_DAYS})")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="from that local day on, instead of --days")
    p.add_argument("--student", default="", metavar="TEXT", help="only addresses containing TEXT")
    p.add_argument("--failed", action="store_true", help="only the failures")
    p.add_argument("--all", action="store_true", dest="everything",
                   help="every step, the in-progress ones (link sent, Google started) too")
    p.add_argument("--follow", action="store_true", help="after the report, keep printing new rows")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between polls with --follow")
    args = p.parse_args(argv)
    if args.interval <= 0:
        p.error("--interval must be positive")

    from tutor.db.models import get_engine, get_session_factory
    from tutor.db.target import print_target

    engine = get_engine()
    print_target(engine)
    factory = get_session_factory(engine)
    needle = args.student.strip().lower()
    since, until = window(args.days, args.since)
    db = factory()
    try:
        rows = select_rows(db, since, needle)
    finally:
        db.close()
    print(render(rows, since, until, failed_only=args.failed, everything=args.everything,
                 needle=needle), flush=True)
    if args.follow:
        try:
            follow(factory, max((a.id for a in rows), default=0), needle, args.interval,
                   failed_only=args.failed, everything=args.everything)
        except KeyboardInterrupt:
            print("", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
