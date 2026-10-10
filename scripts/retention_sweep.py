"""Retention sweep: anonymize students past the one-year window.

    python -m scripts.retention_sweep                       # dry-run: who is eligible
    python -m scripts.retention_sweep --days 365 --confirm  # anonymize the inactive
    python -m scripts.retention_sweep --email ada@example.edu --confirm   # anonymize one now
    python -m scripts.retention_sweep --list                # last activity + anon status

Every confirmed run also prunes used and expired magic links, login attempts
older than ATTEMPTS_DAYS (the admin's sign-in trail, which carries an address
and a browser string), and, only when ACTIVITY_EVENTS_DAYS is set, raw browser
activity events older than that many days.

De-identifies via state.anonymize_student_data: strips PII + free text and keeps
de-identified telemetry under a pseudonym (docs/retention-policy.md). Eligibility
is "no activity for more than --days days" (default 365), measured from the
latest of last_seen / last_login / newest message / newest turn event. Idempotent
and dry-run by default. The counterpart to scripts/delete_student_data.py
(deletion on request); run it manually until it is scheduled.

The target database (from DATABASE_URL, else local SQLite) is printed on every
run. Anonymizing against a non-SQLite (production) database additionally requires
--allow-prod (or TUTOR_ALLOW_PROD_WRITES=1), so a --confirm meant for a local
copy cannot silently rewrite prod via .env.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from tutor.utctime import utcnow

ATTEMPTS_DAYS = 90   # how long the login_attempts trail is kept


def _last_activity(db, student, Message, TurnEvent):
    """The student's most recent activity timestamp, or None. A student who
    only reads the slides (activity events, no message) is active too
    (activity analytics, step 1.9)."""
    from tutor.db.models import ActivityEvent

    times = [student.last_seen, student.last_login]
    m = (db.query(Message.created_at).filter_by(student_id=student.id)
         .order_by(Message.created_at.desc()).first())
    if m:
        times.append(m[0])
    t = (db.query(TurnEvent.ts).filter_by(student_id=student.id)
         .order_by(TurnEvent.ts.desc()).first())
    if t:
        times.append(t[0])
    a = (db.query(ActivityEvent.ts).filter_by(student_id=student.id)
         .order_by(ActivityEvent.ts.desc()).first())
    if a:
        times.append(a[0])
    times = [x for x in times if x is not None]
    return max(times) if times else None


def prune_activity_events(db, now, days, confirm):
    """Delete (or count, on a dry run) raw activity events older than
    ``days``; ``days`` None or 0 keeps them all (they age out with the
    one-year anonymization). Returns the number affected."""
    from tutor.db.models import ActivityEvent

    if not days:
        return 0
    old = db.query(ActivityEvent).filter(ActivityEvent.ts < now - datetime.timedelta(days=days))
    if not confirm:
        return old.count()
    n = old.delete(synchronize_session=False)
    db.commit()
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=365,
                        help="anonymize students inactive for more than this many days")
    parser.add_argument("--email", help="anonymize one student by email, regardless of age")
    parser.add_argument("--confirm", action="store_true", help="apply (else dry-run)")
    parser.add_argument("--list", action="store_true", help="list students + last activity + anon status")
    parser.add_argument("--allow-prod", action="store_true",
                        help="permit anonymization against a non-SQLite (production) database")
    args = parser.parse_args()

    from tutor.db.target import prepare_schema, print_target, require_prod_ok
    from tutor.agent import state
    from sqlalchemy import or_

    from tutor.db.models import (
        LoginAttempt, MagicLink, Message, RequestError, Student, TurnEvent, get_engine,
        get_session_factory,
    )

    engine = get_engine()
    target = print_target(engine)
    # Gate the destructive path before we connect: a --confirm meant for a local
    # SQLite copy must not silently rewrite prod when .env sets DATABASE_URL.
    if args.confirm:
        require_prod_ok(target, allow_prod=args.allow_prod, action="anonymize student data")
    prepare_schema(engine, target)
    db = get_session_factory(engine)()
    try:
        now = utcnow()
        cutoff = now - datetime.timedelta(days=args.days)

        # Used and expired magic links hold an email and nothing else of use
        # (plan step 2.9, finding C21): pruned on every confirmed run.
        if not args.list:
            stale = db.query(MagicLink).filter(
                or_(MagicLink.used == True, MagicLink.expires_at < now))  # noqa: E712
            if args.confirm:
                print(f"Pruned {stale.delete(synchronize_session=False)} used or expired magic link(s).")
                db.commit()
            else:
                print(f"DRY RUN: {stale.count()} used or expired magic link(s) would be pruned.")
            old_attempts = db.query(LoginAttempt).filter(
                LoginAttempt.created_at < now - datetime.timedelta(days=ATTEMPTS_DAYS))
            if args.confirm:
                print(f"Pruned {old_attempts.delete(synchronize_session=False)} login attempt(s) "
                      f"older than {ATTEMPTS_DAYS} days.")
                db.commit()
            else:
                print(f"DRY RUN: {old_attempts.count()} login attempt(s) older than "
                      f"{ATTEMPTS_DAYS} days would be pruned.")
            # Error pages students hit (tutor/request_errors.py): the same window.
            old_errors = db.query(RequestError).filter(
                RequestError.ts < now - datetime.timedelta(days=ATTEMPTS_DAYS))
            if args.confirm:
                print(f"Pruned {old_errors.delete(synchronize_session=False)} request error(s) "
                      f"older than {ATTEMPTS_DAYS} days.")
                db.commit()
            else:
                print(f"DRY RUN: {old_errors.count()} request error(s) older than "
                      f"{ATTEMPTS_DAYS} days would be pruned.")
            # Raw activity events, only when ACTIVITY_EVENTS_DAYS is set
            # (activity analytics, decision 8: unset by default). Read the
            # way tutor.config reads it, without get_config(): that requires
            # the app's API key and secret, which the scheduled sweep has not.
            from tutor.config import _int_env

            event_days = _int_env("ACTIVITY_EVENTS_DAYS", 0, floor=0) or None
            if event_days:
                n_events = prune_activity_events(db, now, event_days, args.confirm)
                verb = "Pruned" if args.confirm else "DRY RUN:"
                tail = "." if args.confirm else " would be pruned."
                print(f"{verb} {n_events} activity event(s) older than {event_days} days{tail}")

        if args.list:
            for s in db.query(Student).order_by(Student.email).all():
                la = _last_activity(db, s, Message, TurnEvent)
                flag = "ANON" if s.anonymized_at else "    "
                print(f"{flag}  {s.email:42s} last_activity={la.date().isoformat() if la else '-'}")
            return 0

        # Build the target list.
        if args.email:
            s = db.query(Student).filter_by(email=args.email.strip().lower()).first()
            if s is None:
                print(f"No student with email {args.email!r}", file=sys.stderr)
                return 1
            targets = [s]
        else:
            targets = [
                s for s in db.query(Student).filter(Student.anonymized_at.is_(None)).all()
                if (la := _last_activity(db, s, Message, TurnEvent)) is not None and la < cutoff
            ]

        targets = [s for s in targets if s.anonymized_at is None]
        if not targets:
            print("No students to anonymize.")
            return 0

        if not args.confirm:
            reason = "by email" if args.email else f"inactive > {args.days}d"
            print(f"DRY RUN: {len(targets)} student(s) eligible ({reason}):")
            for s in targets:
                la = _last_activity(db, s, Message, TurnEvent)
                print(f"  {s.email}  (id {s.id}, last activity {la.date().isoformat() if la else '-'})")
            print("Re-run with --confirm to anonymize.")
            return 0

        n = 0
        for s in targets:
            orig = s.email
            counts = state.anonymize_student_data(db, s.id)
            db.commit()
            n += 1
            summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
            print(f"Anonymized {orig} (id {s.id}): {summary}")
        print(f"Anonymized {n} student(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
