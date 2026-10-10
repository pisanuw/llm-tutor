#!/usr/bin/env python3
"""Download student conversations as Markdown files, off the server and out of the repo.

    python -m scripts.download_conversations                       # everything, one file per local day
    python -m scripts.download_conversations --days 14
    python -m scripts.download_conversations --since 2026-09-20 --until 2026-10-01
    python -m scripts.download_conversations --student S12
    python -m scripts.download_conversations --student ada@example.edu --with-names
    python -m scripts.download_conversations --class-id 3 --split student
    python -m scripts.download_conversations --plain --dry-run
    python -m scripts.download_conversations --list-classes

Read-only against the database ``DATABASE_URL`` names (production, from the
maintainer's .env; the target is printed on stderr first). A *conversation* is
what the admin Conversations tab shows: one student's run of messages with no
gap over 30 minutes (tutor/conversations.py), with the turns' cost and the
flags attached. A run lands whole in the file of the local day it *started*
(days follow ``tutor.timezone`` in courses.yaml), so midnight never cuts one
in two.

The tree, under ``~/tutor-data/conversations`` by default:

    pseudonymous/all/2026-10-08.md           every student, one file per day
    pseudonymous/class-3/2026-10-08.md       --class-id 3
    pseudonymous/student-S12/S12.md          --student S12 --split student
    identifiable/all/2026-10-08.md           --with-names

The default copy is pseudonymous: a student is ``S<id>``, and every student's
address and name is replaced by that pseudonym inside the message text too,
since a student who typed their own name would otherwise undo the pseudonym
(``--no-scrub`` leaves the text alone; a short name that is also an ordinary
word is the price of the scrub). With ``--with-names`` the roster's names and
addresses stay, and those files go in their own tree so an identifiable copy
and a pseudonymous one never share a directory. Staff (admins and instructors)
are left out unless ``--include-staff``.

Either way the files are student data under docs/retention-policy.md: the
script refuses to write inside a git repository, makes what it creates
readable by you alone (0700/0600), writes a README naming the rules beside
them, and downloads the whole history unless you narrow the window.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from tutor import conversations as conv
from tutor.courses import COURSES, IDENTITY
from tutor.transcripts import ROLE_LABEL, tool_notes
from tutor.utctime import REPORT_TZ, local_day, to_local, utcnow

DEFAULT_OUT = Path.home() / "tutor-data" / "conversations"
EPOCH = datetime.datetime(2000, 1, 1)         # before the first message; "everything"
DIR_MODE, FILE_MODE = 0o700, 0o600
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRUB_MIN = 4                                 # a name part shorter than this is left alone


# ---------------------------------------------------------------------------
# Where the files may go
# ---------------------------------------------------------------------------

def refuse_inside_repo(out: Path) -> None:
    """Exit rather than write student data into a working copy of any
    repository (this one included), where a stray ``git add -A`` would commit
    it. Checked before the database is touched."""
    for parent in [out, *out.parents]:
        if (parent / ".git").exists():
            print(f"\nREFUSING to write into {out}: {parent} is a git working copy, and these files are\n"
                  f"student transcripts (docs/retention-policy.md). Pick a path outside every repository,\n"
                  f"e.g. --out {DEFAULT_OUT}.\n", file=sys.stderr)
            raise SystemExit(2)
    if out == PROJECT_ROOT or PROJECT_ROOT in out.parents:
        print(f"\nREFUSING to write into {out}: it is inside the project at {PROJECT_ROOT}.\n", file=sys.stderr)
        raise SystemExit(2)


def mkdir_private(path: Path) -> None:
    """Make the directory and every one this run creates above it readable by
    the owner alone: the files under them are transcripts."""
    missing = [p for p in [path, *path.parents] if not p.exists()]
    path.mkdir(parents=True, exist_ok=True)
    for p in reversed(missing):
        os.chmod(p, DIR_MODE)
    os.chmod(path, DIR_MODE)


def write_private(path: Path, text: str) -> None:
    mkdir_private(path.parent)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, FILE_MODE)


# ---------------------------------------------------------------------------
# The window and the scope
# ---------------------------------------------------------------------------

def window(days: Optional[int], since: Optional[str], until: Optional[str],
           now: Optional[datetime.datetime] = None) -> tuple[datetime.datetime, datetime.datetime]:
    """The naive-UTC ``[since, until]`` to download: local calendar days
    from ``--since`` to the end of ``--until``, or the last ``--days``, or
    everything."""
    now = now or utcnow()
    start, end = EPOCH, now
    if since:
        start = local_day(since)[0]
    elif days:
        start = now - datetime.timedelta(days=days)
    if until:
        end = local_day(until)[1]
    return start, end


def resolve_student(db, text: str):
    """One student from an id, a pseudonym (``S12``), an address, or enough of
    an address or name to be unambiguous."""
    from tutor.db.models import Student

    needle = text.strip()
    sid: Optional[int] = None
    if needle.isdigit():
        sid = int(needle)
    elif len(needle) > 1 and needle[0] in "Ss" and needle[1:].isdigit():
        sid = int(needle[1:])
    if sid is not None:
        found = db.query(Student).filter(Student.id == sid).first()
        if found is None:
            raise SystemExit(f"No student {sid}.")
        return found
    matches = (db.query(Student)
               .filter(Student.email.ilike(f"%{needle}%") | Student.name.ilike(f"%{needle}%"))
               .order_by(Student.id).all())
    exact = [s for s in matches if (s.email or "").lower() == needle.lower()]
    if exact:
        return exact[0]
    if not matches:
        raise SystemExit(f"No student matches {needle!r} (try an id, S12, or an address).")
    if len(matches) > 1:
        listing = "\n".join(f"  S{s.id}  {s.email}" for s in matches[:20])
        raise SystemExit(f"{len(matches)} students match {needle!r}:\n{listing}\nName one exactly.")
    return matches[0]


def print_classes(db) -> None:
    from tutor import classes as classes_mod

    rows = classes_mod.all_classes(db)
    counts = classes_mod.enrollment_counts(db, [c.id for c in rows])
    if not rows:
        print("No classes.")
        return
    print(f"{'id':>4}  {'course':8}  {'students':>8}  class")
    for c in rows:
        print(f"{c.id:>4}  {c.course:8}  {counts.get(c.id, 0):>8}  {c.label}"
              + ("  (archived)" if c.archived else ""))


# ---------------------------------------------------------------------------
# Reading the conversations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MsgText:
    """One message in full, with the thumbs its reply got."""
    id: int
    role: str
    content: str
    course: Optional[str]
    lecture_num: Optional[int]
    slide_num: Optional[int]
    created_at: datetime.datetime
    tools: Optional[list] = None
    rating: Optional[int] = None
    comment: Optional[str] = None


@dataclass
class Download:
    rows: list                                  # conversations.Conversation, oldest first
    texts: dict[int, MsgText]
    students: dict[int, object]                 # every student row, for the labels and the scrub
    since: datetime.datetime
    until: datetime.datetime
    scope: str = "all"                          # the subdirectory: all, class-3, student-S12
    title: str = "all students"

    @property
    def student_count(self) -> int:
        return len({c.student_id for c in self.rows})

    @property
    def message_count(self) -> int:
        return sum(len(c.message_ids) for c in self.rows)


def load(db, since: datetime.datetime, until: datetime.datetime, *,
         student_ids: Optional[Iterable[int]] = None, course: Optional[str] = None,
         include_staff: bool = False) -> Download:
    """Every conversation in the window, with its messages in full.

    The grouping, the cost and the flags are tutor/conversations.py's, so a
    downloaded conversation is the one the Conversations tab lists; only the
    text is read in full here, which that list deliberately does not do."""
    from tutor.auth.access import is_staff
    from tutor.db.models import Message, MessageFeedback, Student, TurnEvent

    students = {s.id: s for s in db.query(Student).order_by(Student.id).all()}
    wanted = None if student_ids is None else {int(i) for i in student_ids}
    if not include_staff:
        staff = {sid for sid, s in students.items() if is_staff(s)}
        wanted = (set(students) - staff) if wanted is None else (wanted - staff)

    q = (db.query(Message.id, Message.student_id, Message.role, Message.created_at, Message.course,
                  Message.lecture_num, Message.slide_num, Message.content, Message.tool_summary)
         .filter(Message.created_at >= since, Message.created_at <= until))
    if wanted is not None:
        if not wanted:
            return Download([], {}, students, since, until)
        q = q.filter(Message.student_id.in_(sorted(wanted)))
    if course:
        q = q.filter(Message.course == course)
    raw = q.order_by(Message.student_id, Message.created_at, Message.id).all()

    texts = {
        r[0]: MsgText(id=r[0], role=r[2], content=r[7] or "", course=r[4], lecture_num=r[5],
                      slide_num=r[6], created_at=r[3], tools=tool_notes(r[8]))
        for r in raw
    }
    # Msg.preview carries the whole message here: conv.build only measures and
    # truncates it for the row's preview, and the full text is kept in `texts`.
    msgs = [conv.Msg(r[0], r[1], r[2], r[3], r[4], r[5], r[7] or "") for r in raw]
    rows = [conv.build(run) for run in conv.split(msgs)]
    if not rows:
        return Download([], texts, students, since, until)

    ids = sorted({c.student_id for c in rows})
    turns = (db.query(TurnEvent)
             .filter(TurnEvent.student_id.in_(ids), TurnEvent.ts >= since - conv.TURN_SLACK,
                     TurnEvent.ts <= until + conv.TURN_SLACK).all())
    lowest = min(c.message_ids[0] for c in rows)
    feedback = (db.query(MessageFeedback)
                .filter(MessageFeedback.student_id.in_(ids), MessageFeedback.message_id >= lowest).all())
    conv.attach_turns(rows, turns)
    conv.attach_feedback(rows, feedback)
    for f in feedback:
        m = texts.get(f.message_id)
        if m is not None:
            texts[m.id] = dataclasses.replace(m, rating=f.rating, comment=f.comment)

    from sqlalchemy import func

    first_ids = {int(sid): int(first) for sid, first in
                 db.query(Message.student_id, func.min(Message.id))
                 .filter(Message.student_id.in_(ids)).group_by(Message.student_id).all()}
    conv.attach_first_ever(rows, first_ids)

    labels: dict[int, list[str]] = {}
    from tutor.db.models import ClassEnrollment, CourseClass

    for sid, cls in (db.query(ClassEnrollment.student_id, CourseClass)
                     .join(CourseClass, CourseClass.id == ClassEnrollment.class_id)
                     .filter(ClassEnrollment.student_id.in_(ids)).order_by(CourseClass.id.desc()).all()):
        labels.setdefault(sid, []).append(cls.label)
    for c in rows:
        s = students.get(c.student_id)
        c.email = (s.email if s else "") or f"#{c.student_id}"
        c.name = (s.name or "") if s else ""
        c.classes = labels.get(c.student_id, [])
        c.span = conv.span_words(c.start, c.end)
    rows.sort(key=lambda c: (c.start, c.first_message_id))
    return Download(rows, texts, students, since, until)


# ---------------------------------------------------------------------------
# Names, or not
# ---------------------------------------------------------------------------

def pseudonym(student_id: int) -> str:
    """The same name the analytics store and the reports use."""
    return f"S{student_id}"


def display_label(student, named: bool) -> str:
    if not named:
        return pseudonym(student.id)
    name = (getattr(student, "name", "") or "").strip()
    email = getattr(student, "email", "") or ""
    return f"{name} <{email}>" if name else (email or pseudonym(student.id))


def file_slug(student, named: bool) -> str:
    """A filename for one student: the pseudonym, or the address's local part
    sanitized the way the admin download does it."""
    if not named:
        return pseudonym(student.id)
    local = (getattr(student, "email", "") or "").split("@", 1)[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", local).strip("-").lower() or pseudonym(student.id)


def build_scrubber(students: dict[int, object], enabled: bool) -> Callable[[str], str]:
    """Replace every student's address and name in the message text with their
    pseudonym, so a student who signed their question does not undo it.

    An address, and a name or name part of at least SCRUB_MIN characters, is
    matched whatever its case. A shorter name ("Ada", "Bob", "Max") is matched
    only as a capitalized word, so "find the max element" keeps its code and
    "Hi, I'm Max" does not; a short name written in lower case therefore
    survives, which is the limit of a scrub this cheap. The first student to
    claim a needle keeps it, so two students sharing a surname both read as
    the lower id."""
    if not enabled:
        return lambda text: text or ""
    loose: dict[str, str] = {}      # matched whatever the case
    strict: dict[str, str] = {}     # matched as capitalized, for the short names

    def claim(needle: str, pseud: str) -> None:
        needle = (needle or "").strip()
        if len(needle) < 2:
            return
        if len(needle) >= SCRUB_MIN:
            loose.setdefault(needle.lower(), pseud)
        else:
            strict.setdefault(needle.capitalize(), pseud)
            if needle[:1].isupper():
                strict.setdefault(needle, pseud)

    for sid in sorted(students):
        s, pseud = students[sid], pseudonym(sid)
        email = (getattr(s, "email", "") or "").strip()
        if email:
            claim(email, pseud)
            claim(email.split("@", 1)[0], pseud)
    for sid in sorted(students):                     # addresses first: they are never ambiguous
        s, pseud = students[sid], pseudonym(sid)
        name = (getattr(s, "name", "") or "").strip()
        if not name:
            continue
        claim(name, pseud)
        for part in re.split(r"\s+", name):
            claim(part, pseud)

    def compile_alt(needles: Iterable[str], flags: int = 0):
        ordered = sorted(needles, key=len, reverse=True)
        if not ordered:
            return None
        return re.compile("|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in ordered), flags)

    loose_re, strict_re = compile_alt(loose, re.I), compile_alt(strict)

    def scrub(text: Optional[str]) -> str:
        out = text or ""
        if loose_re is not None:
            out = loose_re.sub(lambda m: loose[m.group(0).lower()], out)
        if strict_re is not None:
            out = strict_re.sub(lambda m: strict[m.group(0)], out)
        return out

    return scrub


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def clock(ts: datetime.datetime) -> str:
    """The instant in local time, named ("09:14 PDT"), since the files are
    local days."""
    return to_local(ts).strftime("%H:%M %Z")


def day_of(ts: datetime.datetime) -> datetime.date:
    return to_local(ts).date()


def plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {many or word + 's'}"


def money(amount: float) -> str:
    return f"${amount:.4f}" if amount < 1 else f"${amount:.2f}"


class Renderer:
    """The Markdown. One conversation renders the same whatever file it lands
    in; only the heading level changes."""

    def __init__(self, data: Download, *, named: bool, plain: bool, scrub: Callable[[str], str]):
        self.data, self.named, self.plain, self.scrub = data, named, plain, scrub

    def label(self, student_id: int) -> str:
        s = self.data.students.get(student_id)
        return display_label(s, self.named) if s is not None else pseudonym(student_id)

    def slug(self, student_id: int) -> str:
        s = self.data.students.get(student_id)
        return file_slug(s, self.named) if s is not None else pseudonym(student_id)

    # --- one message -------------------------------------------------------

    def message(self, m: MsgText) -> list[str]:
        who = ROLE_LABEL.get(m.role, m.role)
        if self.plain:
            out = [f"**{who}:**", "", self.scrub(m.content).rstrip(), ""]
            return out
        meta = [who]
        if m.course:
            meta.append(m.course)
        if m.lecture_num:
            meta.append(f"L{m.lecture_num:02d}" + (f" slide {m.slide_num}" if m.slide_num else ""))
        meta.append(clock(m.created_at))
        out = ["**" + " · ".join(meta) + "**", "", self.scrub(m.content).rstrip(), ""]
        if m.tools:
            bits = []
            for n in m.tools:
                if not isinstance(n, dict):
                    continue
                bit = str(n.get("tool", ""))
                if n.get("query"):
                    bit += f" '{self.scrub(str(n['query']))}'"
                if n.get("top_source"):
                    bit += f" -> {n['top_source']}"
                if n.get("slide"):
                    bit += f" (showed slide {n['slide']})"
                if bit:
                    bits.append(bit)
            if bits:
                out += ["_[tools used: " + "; ".join(bits) + "]_", ""]
        if m.rating:
            glyph = "👍" if m.rating > 0 else "👎"
            note = f" {self.scrub(m.comment)}" if m.comment else ""
            out += [f"_[student feedback: {glyph}{note}]_", ""]
        return out

    # --- one conversation --------------------------------------------------

    def conversation(self, c, level: int, *, with_who: bool = True) -> list[str]:
        head = [clock(c.start)]
        if with_who:
            head.append(self.label(c.student_id))
        if c.course:
            head.append(c.course)
        if c.lectures and not self.plain:
            head.append(", ".join(c.lecture_labels))
        head.append(c.span or conv.span_words(c.start, c.end))
        lines = ["#" * level + " " + " · ".join(head), ""]
        if not self.plain:
            facts = [plural(c.questions, "question"), plural(c.replies, "reply", "replies")]
            if with_who and c.classes:
                facts.append(", ".join(c.classes))
            if c.cost_usd:
                facts.append(money(c.cost_usd))
            if c.thumbs_up:
                facts.append(f"👍 {c.thumbs_up}")
            if c.thumbs_down:
                facts.append(f"👎 {c.thumbs_down}")
            if c.cuts:
                facts.append(f"detector cut ×{c.cuts}")
            if c.exams:
                facts.append(f"exam turns ×{c.exams}")
            if c.quota_hits:
                facts.append(f"quota hit ×{c.quota_hits}")
            if c.first_ever:
                facts.append("first conversation")
            lines += [" · ".join(facts), ""]
        for mid in c.message_ids:
            m = self.data.texts.get(mid)
            if m is not None:
                lines += self.message(m)
        return lines

    # --- the files ---------------------------------------------------------

    def header(self, title: str, subtitle: str) -> list[str]:
        lines = [f"# {title}", "", subtitle, ""]
        if self.named:
            lines += ["> **Identifiable copy**: holds student names and addresses. Keep it off shared "
                      "drives and delete it when you are done (docs/retention-policy.md).", ""]
        return lines

    def day_file(self, day: datetime.date, rows: Sequence) -> str:
        students = len({c.student_id for c in rows})
        subtitle = (f"{plural(len(rows), 'conversation')} · {plural(students, 'student')}"
                    f" · times in {REPORT_TZ} · downloaded {clock(utcnow())} {day_of(utcnow())}")
        lines = self.header(f"{day:%A %d %B %Y} ({day})", subtitle)
        for c in rows:
            lines += self.conversation(c, 2)
        return "\n".join(lines).rstrip() + "\n"

    def student_file(self, student_id: int, rows: Sequence) -> str:
        days = sorted({day_of(c.start) for c in rows})
        subtitle = (f"{plural(len(rows), 'conversation')} over {plural(len(days), 'day')}"
                    + (f" · {days[0]} to {days[-1]}" if days else "")
                    + f" · times in {REPORT_TZ} · downloaded {clock(utcnow())} {day_of(utcnow())}")
        lines = self.header(self.label(student_id), subtitle)
        for day in days:
            lines += [f"## {day:%A} {day}", ""]
            for c in [r for r in rows if day_of(r.start) == day]:
                lines += self.conversation(c, 3, with_who=False)
        return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# The file set
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    files: dict[Path, str] = field(default_factory=dict)       # relative path -> text
    counts: list[tuple[str, int, int, int, int]] = field(default_factory=list)   # file, convs, students, q, a


def group(rows: Sequence, key: Callable) -> dict:
    out: dict = {}
    for c in rows:
        out.setdefault(key(c), []).append(c)
    return out


def plan(data: Download, *, split: str, named: bool, plain: bool, scrub: Callable[[str], str],
         scrubbed: bool = True) -> Plan:
    """Every file to write, relative to the scope directory, plus its index."""
    r = Renderer(data, named=named, plain=plain, scrub=scrub)
    out = Plan()
    if split == "student":
        for sid, rows in sorted(group(data.rows, lambda c: c.student_id).items()):
            name = f"{r.slug(sid)}.md"
            out.files[Path(name)] = r.student_file(sid, rows)
            out.counts.append((name, len(rows), 1, sum(c.questions for c in rows), sum(c.replies for c in rows)))
    else:
        for day, rows in sorted(group(data.rows, lambda c: day_of(c.start)).items()):
            name = f"{day}.md"
            out.files[Path(name)] = r.day_file(day, rows)
            out.counts.append((name, len(rows), len({c.student_id for c in rows}),
                               sum(c.questions for c in rows), sum(c.replies for c in rows)))
    out.files[Path("index.md")] = index_file(data, out.counts, split=split, named=named, plain=plain,
                                             scrubbed=scrubbed)
    return out


def index_file(data: Download, counts: Sequence[tuple[str, int, int, int, int]], *, split: str,
               named: bool, plain: bool, scrubbed: bool) -> str:
    mode = ("identifiable: names and addresses as the roster has them" if named
            else "pseudonymous: every student is S<id>" + (", and the text is scrubbed too" if scrubbed else ""))
    lines = [
        f"# Index: {data.title}", "",
        f"{plural(len(data.rows), 'conversation')} · {plural(data.student_count, 'student')} · "
        f"{plural(data.message_count, 'message')}",
        f"Window: {data.since.date()} to {data.until.date()} (UTC bounds) · one file per "
        + ("student" if split == "student" else f"day ({REPORT_TZ})"),
        f"Mode: {mode}" + (" · plain (no metadata)" if plain else ""),
        f"Downloaded {clock(utcnow())} {day_of(utcnow())}", "",
        "| File | Conversations | Students | Questions | Replies |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, convs, students, q, a in counts:
        lines.append(f"| [{name}]({name}) | {convs} | {students} | {q} | {a} |")
    if counts:
        lines.append(f"| **total** | **{sum(c[1] for c in counts)}** | **{data.student_count}** | "
                     f"**{sum(c[3] for c in counts)}** | **{sum(c[4] for c in counts)}** |")
    return "\n".join(lines) + "\n"


README = f"""# {IDENTITY.name} conversations (downloaded)

Student transcripts, downloaded with `python -m scripts.download_conversations`
from the {IDENTITY.name} database. **This is student data**, governed by
docs/retention-policy.md in the tutor's repository:

- Never commit it, share it, or put it on a drive other people read.
- `pseudonymous/` names every student `S<id>` and scrubs addresses and names
  out of the message text. `identifiable/` keeps them: treat it as a roster.
- Delete a student's files on a deletion request, and the whole tree once you
  no longer need it (identifiable records are not kept past a year).

Each file is one day in {REPORT_TZ} (or one student, with `--split student`); a
conversation is one student's run of messages with no gap over 30 minutes, and
it belongs to the day it started. Re-running the command rewrites these files.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.download_conversations", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"where the files go (default {DEFAULT_OUT})")
    p.add_argument("--days", type=int, help="only the last N days (default: the whole history)")
    p.add_argument("--since", help="from this local day on (YYYY-MM-DD)")
    p.add_argument("--until", help="up to the end of this local day (YYYY-MM-DD)")
    p.add_argument("--student", help="one student: id, pseudonym (S12), address, or enough of one")
    p.add_argument("--class-id", type=int, help="one class's roster (see --list-classes)")
    p.add_argument("--course", choices=list(COURSES), help="only this course's messages")
    p.add_argument("--with-names", action="store_true",
                   help="keep names and addresses (identifiable; written under identifiable/)")
    p.add_argument("--no-scrub", action="store_true",
                   help="do not replace names and addresses inside the message text")
    p.add_argument("--split", choices=("day", "student"), default="day",
                   help="one file per local day (default) or one per student")
    p.add_argument("--plain", action="store_true", help="role and text only, no metadata")
    p.add_argument("--include-staff", action="store_true", help="include admins' and instructors' own chats")
    p.add_argument("--list-classes", action="store_true", help="print the classes and their ids, then stop")
    p.add_argument("--dry-run", action="store_true", help="say what would be written, write nothing")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    refuse_inside_repo(out)

    from tutor.db.models import get_engine, get_session_factory
    from tutor.db.target import print_target

    engine = get_engine()
    print_target(engine)                    # read-only, but always say which database
    db = get_session_factory(engine)()
    try:
        if args.list_classes:
            print_classes(db)
            return 0
        since, until = window(args.days, args.since, args.until)
        scope, title, ids = "all", "all students", None
        if args.student and args.class_id:
            print("Pick --student or --class-id, not both.", file=sys.stderr)
            return 2
        if args.student:
            s = resolve_student(db, args.student)
            scope = f"student-{file_slug(s, args.with_names)}"
            title = display_label(s, args.with_names)
            ids = [s.id]
        elif args.class_id:
            from tutor import classes as classes_mod

            klass = classes_mod.get_class(db, args.class_id)
            if klass is None:
                print(f"No class {args.class_id}. Try --list-classes.", file=sys.stderr)
                return 2
            scope, title = f"class-{klass.id}", f"{klass.label} (course {klass.course})"
            ids = classes_mod.roster_ids(db, klass.id)
            if not ids:
                print(f"Class {klass.id} has no students enrolled.", file=sys.stderr)
                return 1
        data = load(db, since, until, student_ids=ids, course=args.course, include_staff=args.include_staff)
    finally:
        db.close()

    data.scope, data.title = scope, title + (f" · course {args.course}" if args.course else "")
    if not data.rows:
        print("No conversations in that window.", file=sys.stderr)
        return 1
    scrubbing = not args.with_names and not args.no_scrub
    scrub = build_scrubber(data.students, enabled=scrubbing)
    built = plan(data, split=args.split, named=args.with_names, plain=args.plain, scrub=scrub,
                 scrubbed=scrubbing)

    tree = out / ("identifiable" if args.with_names else "pseudonymous") / scope
    if args.dry_run:
        print(f"Would write {len(built.files)} files to {tree}:", file=sys.stderr)
        for name in sorted(built.files):
            print(f"  {name}  ({len(built.files[name]):,} bytes)", file=sys.stderr)
    else:
        mkdir_private(out)
        write_private(out / "README.md", README)
        for name, text in built.files.items():
            write_private(tree / name, text)
    verb = "Would download" if args.dry_run else "Downloaded"
    print(f"{verb} {plural(len(data.rows), 'conversation')} "
          f"({plural(data.message_count, 'message')}, {plural(data.student_count, 'student')}) -> {tree}",
          file=sys.stderr)
    if args.with_names:
        print("These files name students. They are student data: keep them private, delete when done.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
