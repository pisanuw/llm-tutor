#!/usr/bin/env python3
"""Set a student's mastery on a goal or a whole lecture, so a demo turn runs at
a ceiling other than H5.

    python -m scripts.set_mastery --student ada@example.edu --list
    python -m scripts.set_mastery --student ada@example.edu --lecture 2                 # dry run
    python -m scripts.set_mastery --student ada@example.edu --lecture 2 --confirm
    python -m scripts.set_mastery --student ada@example.edu --all-lectures --confirm    # the whole course
    python -m scripts.set_mastery --student ada@example.edu --goal AST101-L02-explain-moon-phases \
        --mastery 0.9 --confirm
    python -m scripts.set_mastery --student ada@example.edu --course AST102 --lecture 1 --mastery 0 --confirm

Why this exists: the per-turn hint ceiling is the *most permissive* band among
the goals in scope (``policy.compute_envelope``: meet the student at their
weakest goal), and a goal with no graded evidence counts as mastery 0.0, which
is H5. So one goal with no evidence pins the whole turn at H5, which is why,
until students have graded evidence, every chat turn shows "mastery band
ceiling H5", and why the interesting paths (the constructive-code floor at
H4, the judge's code-adjacent gate at H4 and below) do not fire. Raising mastery on
**every** goal a turn puts in scope is what makes the ceiling move, so
``--lecture`` sets all of that lecture's goals by default.

The goals in scope are the ones the classifier picks out of the question, not
the lecture you had in mind: a question about one topic can scope goals from
three other lectures while the lecture you mastered sits unused, so the turn
still runs at H5.
``--all-lectures`` covers the whole course, which is the reliable way to get a
ceiling other than H5 whatever the demo asks.

    mastery >= 0.85 -> H2      0.70 -> H3      0.40 -> H4      below -> H5

It writes real graded events through ``state.record_graded_event`` (the single
writer for learner state), ``WINDOW_K`` of them so the window's mean is exactly
the mastery asked for, tagged ``source="demo"`` so they can be found again.
That means it moves the student's real progress: the strategist's trajectory,
the review schedule and the ladder all read these rows. Point it at a demo
account, not at a student, and prefer a local database (``DATABASE_URL`` to a
SQLite copy) when the demo can run there.

A dry run by default: ``--confirm`` writes, and a non-SQLite target needs
``--allow-prod`` on top (tutor/db/target.py). Status only ever moves up, so
``--mastery 0`` drops the score but leaves a goal that reached "mastered"
marked as such; the ceiling follows the score, which is what a demo needs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from tutor.courses import COURSES, default_course

DEMO_SOURCE = "demo"
DEFAULT_MASTERY = 0.9


def ceiling_for(mastery: float) -> int:
    from tutor.agent.policy import rung_for_mastery

    return rung_for_mastery(mastery)


def resolve_student(db, text: str):
    """A student by id or address; an ambiguous fragment lists the matches."""
    from tutor.db.models import Student

    needle = (text or "").strip()
    if needle.isdigit():
        found = db.query(Student).filter(Student.id == int(needle)).first()
        if found is None:
            raise SystemExit(f"No student {needle}.")
        return found
    rows = (db.query(Student)
            .filter(Student.email.ilike(f"%{needle}%") | Student.name.ilike(f"%{needle}%"))
            .order_by(Student.id).all())
    exact = [s for s in rows if (s.email or "").lower() == needle.lower()]
    if exact:
        return exact[0]
    if not rows:
        raise SystemExit(f"No student matches {needle!r}.")
    if len(rows) > 1:
        listing = "\n".join(f"  {s.id:>4}  {s.email}" for s in rows[:20])
        raise SystemExit(f"{len(rows)} students match {needle!r}:\n{listing}\nName one exactly.")
    return rows[0]


def goals_for(db, course: str, lecture: Optional[int], goal_id: Optional[str]) -> list:
    from tutor.db.models import LearningGoal

    q = db.query(LearningGoal)
    if goal_id:
        rows = q.filter(LearningGoal.id == goal_id).all()
        if not rows:
            raise SystemExit(f"No learning goal {goal_id!r}.")
        return rows
    q = q.filter(LearningGoal.course == course)
    if lecture is not None:
        q = q.filter(LearningGoal.lecture_num == lecture)
    return q.order_by(LearningGoal.lecture_num, LearningGoal.id).all()


def current_mastery(db, student_id: int, goal_ids: Sequence[str]) -> dict[str, Optional[float]]:
    from tutor.db.models import StudentProgress

    rows = (db.query(StudentProgress)
            .filter(StudentProgress.student_id == student_id,
                    StudentProgress.goal_id.in_(list(goal_ids) or [""])).all())
    return {r.goal_id: r.mastery_score for r in rows}


def print_listing(db, student, course: str, lecture: Optional[int]) -> None:
    """Every goal in scope of the listing with its mastery and the ceiling it
    would give on its own, plus the ceiling a turn covering them all gets."""
    goals = goals_for(db, course, lecture, None)
    have = current_mastery(db, student.id, [g.id for g in goals])
    print(f"{student.email} (id {student.id}) — course {course}"
          + (f", lecture {lecture}" if lecture is not None else "") + "\n")
    print(f"{'goal':<44}{'mastery':>9}{'ceiling':>9}  title")
    for g in goals:
        m = have.get(g.id)
        shown = "—" if m is None else f"{m:.2f}"
        print(f"{g.id:<44}{shown:>9}{'H' + str(ceiling_for(m or 0.0)):>9}  {g.title[:40]}")
    if goals:
        worst = max(ceiling_for(have.get(g.id) or 0.0) for g in goals)
        print(f"\nA turn with all {len(goals)} in scope runs at H{worst} "
              "(the most permissive band among them).")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.set_mastery", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--student", required=True, help="id or address")
    p.add_argument("--course", default=default_course(), choices=list(COURSES),
                   help="course id from courses.yaml (default: the first)")
    p.add_argument("--lecture", type=int, help="every goal of this lecture")
    p.add_argument("--all-lectures", action="store_true",
                   help="every goal of the course (a demo account: it masters the whole course)")
    p.add_argument("--goal", help="one goal id instead of a lecture")
    p.add_argument("--mastery", type=float, default=DEFAULT_MASTERY,
                   help=f"0..1 (default {DEFAULT_MASTERY}; 0.85+ is H2, 0.70 H3, 0.40 H4)")
    p.add_argument("--list", action="store_true", help="show the goals and their mastery, write nothing")
    p.add_argument("--confirm", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--allow-prod", action="store_true", help="required to write to a non-SQLite database")
    args = p.parse_args(argv)

    if not 0.0 <= args.mastery <= 1.0:
        print("--mastery wants 0..1.", file=sys.stderr)
        return 2
    if args.lecture is None and not args.goal and not args.all_lectures and not args.list:
        print("Name --lecture N, --goal ID or --all-lectures (or --list to look).", file=sys.stderr)
        return 2
    if args.all_lectures and (args.lecture is not None or args.goal):
        print("--all-lectures is the whole course: do not also name --lecture or --goal.",
              file=sys.stderr)
        return 2

    from tutor.agent import state
    from tutor.db.models import TurnEvent, get_engine, get_session_factory
    from tutor.db.target import print_target, require_prod_ok

    engine = get_engine()
    target = print_target(engine)
    db = get_session_factory(engine)()
    try:
        student = resolve_student(db, args.student)
        if args.list:
            print_listing(db, student, args.course, args.lecture)
            return 0

        goals = goals_for(db, args.course, args.lecture, args.goal)
        if not goals:
            print(f"No goals for course {args.course}"
                  + (f", lecture {args.lecture}" if args.lecture is not None else "") + ".",
                  file=sys.stderr)
            return 1
        turns = db.query(TurnEvent).filter_by(student_id=student.id, kind="chat").count()

        ceiling = ceiling_for(args.mastery)
        print(f"\n{student.email} (id {student.id}): {len(goals)} goal(s) -> mastery "
              f"{args.mastery:.2f}, ceiling H{ceiling} "
              f"({len(goals) * state.WINDOW_K} graded events)")
        for g in goals:
            print(f"  {g.id}")
        if turns:
            print(f"\nNote: this student has {turns} real chat turn(s). These writes become part of "
                  "their progress, review schedule and trajectory. Prefer a demo account.")
        if not args.confirm:
            print("\nDry run: nothing written. Re-run with --confirm"
                  + ("" if target.is_sqlite else " --allow-prod") + ".")
            return 0

        require_prod_ok(target, allow_prod=args.allow_prod,
                        action=f"write {state.WINDOW_K} graded events per goal for {student.email}")
        for g in goals:
            score = 0.0
            for _ in range(state.WINDOW_K):
                score = state.record_graded_event(
                    db, student_id=student.id, goal_id=g.id, success=args.mastery,
                    source=DEMO_SOURCE,
                )
            print(f"  {g.id}: mastery {score:.2f} -> ceiling H{ceiling_for(score)}")
        db.commit()
        worst = max(ceiling_for(args.mastery) for _g in goals)
        print(f"\nWritten. A turn with these goals in scope now runs at H{worst}; a goal with no "
              "evidence still pins a turn at H5, so set every goal a demo question touches.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
