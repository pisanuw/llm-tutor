"""scripts/download_conversations.py: the conversations as Markdown on the
instructor's own machine.

One file per local day (the class's time zone, courses.yaml) holding every
conversation that started in it (or one
file per student), pseudonymous by default -- including the names and addresses
students type into the text -- identifiable only on request and then in its own
tree, never inside a git working copy.
"""
from __future__ import annotations

import datetime

import pytest

from tests.helpers import make_session
import scripts.download_conversations as dc
from tutor.db.models import (ClassEnrollment, CourseClass, Message, MessageFeedback, Student,
                             TurnEvent)

# 06:50 UTC on the 8th is 23:50 PDT on the 7th in the fixture's zone
# (America/Los_Angeles): a conversation starting here crosses local midnight.
LATE = datetime.datetime(2026, 10, 8, 6, 50)
MORNING = datetime.datetime(2026, 10, 7, 16, 14)        # 09:14 PDT


def _m(mid, sid, when, role="user", text="q", course="AST101", lecture=2, slide=None, tools=None):
    return Message(id=mid, student_id=sid, role=role, content=text, course=course,
                   lecture_num=lecture, slide_num=slide, tool_summary=tools, created_at=when)


@pytest.fixture
def db():
    s = make_session(
        Student(id=1, email="ada@example.edu", name="Ada Lovelace"),
        Student(id=2, email="bob.stone@example.edu", name="Bob Stone"),
        Student(id=3, email="prof@example.edu", name="Prof", is_instructor=True),
        CourseClass(id=3, name="A", course="AST101", term="Au26", instructor_id=3),
        ClassEnrollment(class_id=3, student_id=1),
        # Ada, morning: two messages, with a thumbs-down and a tool note
        _m(1, 1, MORNING, text="Hi, I'm Ada (ada@example.edu), why does the Moon change shape?", slide=3,
           tools='[{"tool": "find_topic", "query": "phases", "top_source": "L02", "slide": 3}]'),
        _m(2, 1, MORNING + datetime.timedelta(minutes=1), "assistant", "Look at where the Sun is."),
        # Ada again, an hour later: a second conversation the same day
        _m(3, 1, MORNING + datetime.timedelta(hours=1), text="and eclipses?"),
        _m(4, 1, MORNING + datetime.timedelta(hours=1, minutes=1), "assistant", "Same idea."),
        # Ada across local midnight: one conversation, the earlier day's file
        _m(5, 1, LATE, text="late question"),
        _m(6, 1, LATE + datetime.timedelta(minutes=20), "assistant", "Answer."),
        # Bob, the next day, in the other course
        _m(7, 2, MORNING + datetime.timedelta(days=1), text="redshift?", course="AST102", lecture=4),
        _m(8, 2, MORNING + datetime.timedelta(days=1, minutes=1), "assistant", "Stretched light.",
           course="AST102", lecture=4),
        # the instructor's own chat
        _m(9, 3, MORNING, text="testing"),
        _m(10, 3, MORNING + datetime.timedelta(minutes=1), "assistant", "Works."),
        MessageFeedback(message_id=2, student_id=1, rating=-1, comment="too vague for Ada"),
        TurnEvent(student_id=1, ts=MORNING + datetime.timedelta(minutes=1), kind="chat", turn_id="t1",
                  cost_usd=0.0412),
    )
    yield s
    s.close()


def _load(db, **kw):
    return dc.load(db, dc.EPOCH, datetime.datetime(2026, 12, 1), **kw)


def _files(data, **kw):
    kw.setdefault("split", "day")
    kw.setdefault("named", False)
    kw.setdefault("plain", False)
    scrub = kw.pop("scrub", None) or dc.build_scrubber(data.students, enabled=not kw["named"])
    return {str(p): text for p, text in dc.plan(data, scrub=scrub, **kw).files.items()}


# ---------------------------------------------------------------------------
# What gets downloaded
# ---------------------------------------------------------------------------

def test_staff_are_left_out_unless_asked(db):
    assert {c.student_id for c in _load(db).rows} == {1, 2}
    assert {c.student_id for c in _load(db, include_staff=True).rows} == {1, 2, 3}


def test_a_scope_narrows_to_one_student_or_one_roster(db):
    assert {c.student_id for c in _load(db, student_ids=[1]).rows} == {1}
    assert {c.student_id for c in _load(db, student_ids=[]).rows} == set()      # an empty roster
    assert {c.course for c in _load(db, course="AST102").rows} == {"AST102"}


def test_a_conversation_carries_its_cost_flags_and_messages(db):
    first = _load(db, student_ids=[1]).rows[0]
    assert (first.questions, first.replies) == (1, 1)
    assert first.cost_usd == pytest.approx(0.0412) and first.thumbs_down == 1
    assert first.first_ever and first.classes == ["A (Au26)"]
    data = _load(db, student_ids=[1])
    assert data.texts[1].content.startswith("Hi, I'm Ada")       # the whole message, not a preview
    assert data.texts[2].rating == -1 and data.texts[2].comment == "too vague for Ada"


# ---------------------------------------------------------------------------
# One file per local day
# ---------------------------------------------------------------------------

def test_a_day_file_holds_every_conversation_that_started_in_it(db):
    files = _files(_load(db))
    assert sorted(files) == ["2026-10-07.md", "2026-10-08.md", "index.md"]
    day = files["2026-10-07.md"]
    # three of Ada's conversations: 09:14, 10:14, and the one starting 23:50
    assert day.count("\n## ") == 3
    assert "23:50 PDT" in day and "late question" in day       # not pushed into the 8th
    assert "2 conversations" not in day and "3 conversations" in day
    assert files["2026-10-08.md"].count("\n## ") == 1           # Bob's, the next day


def test_the_index_counts_the_files_it_lists(db):
    index = _files(_load(db))["index.md"]
    assert "| [2026-10-07.md](2026-10-07.md) | 3 | 1 | 3 | 3 |" in index
    assert "| **total** | **4** | **2** |" in index
    assert "pseudonymous: every student is S<id>, and the text is scrubbed too" in index


def test_split_student_writes_one_file_per_student_with_day_headings(db):
    files = _files(_load(db), split="student")
    assert sorted(files) == ["S1.md", "S2.md", "index.md"]
    assert files["S1.md"].startswith("# S1\n")
    assert "## Wednesday 2026-10-07" in files["S1.md"]
    assert files["S1.md"].count("\n### ") == 3                   # the conversations sit under the day
    assert "S2" not in files["S1.md"]


# ---------------------------------------------------------------------------
# Names, or not
# ---------------------------------------------------------------------------

def test_the_default_copy_replaces_names_and_addresses_in_the_text_too(db):
    day = _files(_load(db))["2026-10-07.md"]
    assert "ada@example.edu" not in day and "Ada" not in day
    assert "Hi, I'm S1 (S1), why does the Moon change shape?" in day   # the address and the short first name
    assert "too vague for S1" in day                            # the feedback comment as well
    assert "S1" in day and "## 09:14 PDT · S1 · AST101" in day


def test_the_scrub_leaves_an_ordinary_word_alone(db):
    scrub = dc.build_scrubber(_load(db).students, enabled=True)
    assert scrub("find the max brightness") == "find the max brightness"  # not Max the student
    assert scrub("Ada and Bob paired up") == "S1 and S2 paired up"
    assert scrub("mail bob.stone@example.edu or Stone") == "mail S2 or S2"
    assert scrub("adaptive optics") == "adaptive optics"                # a substring is not a name
    assert dc.build_scrubber(_load(db).students, enabled=False)("Ada") == "Ada"


def test_with_names_keeps_the_roster_and_marks_the_file(db):
    files = _files(_load(db), named=True)
    day = files["2026-10-07.md"]
    assert "Ada Lovelace <ada@example.edu>" in day and "Hi, I'm Ada (ada@example.edu)" in day
    assert "**Identifiable copy**" in day
    assert "identifiable: names and addresses as the roster has them" in files["index.md"]
    assert sorted(_files(_load(db), named=True, split="student")) == ["ada.md", "bob.stone.md", "index.md"]


def test_plain_drops_the_metadata_and_keeps_the_conversation(db):
    day = _files(_load(db), plain=True)["2026-10-07.md"]
    assert "**Student:**" in day and "**Tutor:**" in day
    assert "late question" in day
    for gone in ("$0.04", "tools used", "student feedback", "L02", "questions ·"):
        assert gone not in day
    assert "## 23:50 PDT · S1 · AST101 · 20 min" in day            # the header still places it


# ---------------------------------------------------------------------------
# The window, the scope arguments, and where files may go
# ---------------------------------------------------------------------------

def test_the_window_reads_local_days_or_the_last_n_days():
    now = datetime.datetime(2026, 10, 9, 12, 0)
    assert dc.window(None, None, None, now) == (dc.EPOCH, now)          # everything
    assert dc.window(3, None, None, now)[0] == now - datetime.timedelta(days=3)
    since, until = dc.window(None, "2026-10-07", "2026-10-07", now)
    assert since == datetime.datetime(2026, 10, 7, 7, 0)                # local midnight, in UTC
    assert until == datetime.datetime(2026, 10, 8, 7, 0)                # the end of that day
    assert dc.window(5, "2026-10-07", None, now)[0] == since            # --since wins over --days


def test_a_student_is_named_by_id_pseudonym_or_address(db):
    assert dc.resolve_student(db, "1").id == 1
    assert dc.resolve_student(db, "S1").id == 1
    assert dc.resolve_student(db, "ada@example.edu").id == 1
    assert dc.resolve_student(db, "Lovelace").id == 1                   # enough of a name
    with pytest.raises(SystemExit):
        dc.resolve_student(db, "example.edu")                                # matches everyone
    with pytest.raises(SystemExit):
        dc.resolve_student(db, "S99")


def test_it_refuses_to_write_inside_a_git_working_copy(tmp_path):
    dc.refuse_inside_repo(tmp_path / "convos")                          # no repository above: fine
    (tmp_path / ".git").mkdir()
    with pytest.raises(SystemExit) as caught:
        dc.refuse_inside_repo(tmp_path / "convos")
    assert caught.value.code == 2
    with pytest.raises(SystemExit):
        dc.refuse_inside_repo(dc.PROJECT_ROOT / "convos")               # this repository included


def test_what_it_writes_is_readable_by_the_owner_alone(tmp_path):
    target = tmp_path / "tree" / "pseudonymous" / "all" / "2026-10-07.md"
    dc.write_private(target, "# day\n")
    assert target.read_text() == "# day\n"
    assert (target.stat().st_mode & 0o777) == dc.FILE_MODE
    assert (target.parent.stat().st_mode & 0o777) == dc.DIR_MODE
    assert ((tmp_path / "tree").stat().st_mode & 0o777) == dc.DIR_MODE
