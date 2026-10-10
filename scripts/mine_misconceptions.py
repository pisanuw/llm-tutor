"""Weekly cohort misconception mining + curation CLI (pedagogy item 12).

    python -m scripts.mine_misconceptions --days 14 --dry-run   # report only, zero API calls
    python -m scripts.mine_misconceptions --days 14             # mine eligible goals -> candidates
    python -m scripts.mine_misconceptions --list                # show candidates/approved
    python -m scripts.mine_misconceptions --approve 12 --reject 13

Writes (mining with a key, --approve, --reject) against a non-SQLite
(production) database refuse without --allow-prod; the weekly Action passes it.

Only APPROVED rows reach prompts. Exits 0 with a message when there is nothing
eligible or no API key (so the scheduled run never emails about a quiet week).
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=14, help="window size ending now (default 14)")
    p.add_argument("--dry-run", action="store_true", help="gather + report, no model calls")
    p.add_argument("--list", action="store_true", help="list library rows and exit")
    p.add_argument("--approve", type=int, action="append", default=[], metavar="ID")
    p.add_argument("--reject", type=int, action="append", default=[], metavar="ID")
    p.add_argument("--allow-prod", action="store_true",
                   help="allow writes to a non-SQLite (production) database")
    args = p.parse_args(argv)

    from tutor.agent import misconception_mining as mm
    from tutor.db.models import GoalMisconception, get_engine, get_session_factory
    from tutor.db.target import print_target, require_prod_ok

    engine = get_engine()
    target = print_target(engine)
    mining_writes = not (args.list or args.dry_run) and bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    if args.approve or args.reject:
        require_prod_ok(target, allow_prod=args.allow_prod,
                        action="change the approved misconception library")
    elif mining_writes:
        require_prod_ok(target, allow_prod=args.allow_prod, action="write misconception candidates")
    db = get_session_factory(engine)()
    try:
        if args.approve or args.reject:
            for rid in args.approve:
                print(f"approve {rid}:", "ok" if mm.set_status(db, rid, "approved") else "not found")
            for rid in args.reject:
                print(f"reject {rid}:", "ok" if mm.set_status(db, rid, "rejected") else "not found")
            db.commit()
            return 0
        if args.list:
            rows = db.query(GoalMisconception).order_by(
                GoalMisconception.status, GoalMisconception.goal_id, GoalMisconception.id).all()
            if not rows:
                print("library empty")
            for r in rows:
                print(f"[{r.id}] {r.status:9s} {r.goal_id} (n={r.evidence_count}) {r.description}")
                if r.distractor:
                    print(f"      distractor: {r.distractor}")
            return 0

        from tutor.utctime import utcnow

        until = utcnow()
        since = until - datetime.timedelta(days=args.days)
        dry = args.dry_run or not os.getenv("ANTHROPIC_API_KEY", "").strip()
        if dry and not args.dry_run:
            print("mine_misconceptions: no ANTHROPIC_API_KEY; reporting only")
        rep = mm.run(db, since, until, dry_run=dry)
        if dry:
            db.rollback()
        else:
            db.commit()
        print(f"mine_misconceptions: window {since:%Y-%m-%d}..{until:%Y-%m-%d}; "
              f"goals with notes {rep.goals_seen}, eligible {rep.goals_eligible}, "
              f"mined {rep.goals_mined}, candidates new {rep.candidates_new} / updated {rep.candidates_updated}"
              + (" (dry run)" if rep.dry_run else ""))
        for line in rep.lines:
            print("  ", line)
        if rep.goals_eligible == 0:
            print("mine_misconceptions: nothing eligible (need "
                  f">={mm.MIN_STUDENTS} distinct students with notes on a goal)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
