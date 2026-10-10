"""The voice endpoint only speaks server-generated replies (finding S7)."""

from __future__ import annotations

import pytest

from tutor.db.models import Message, Student
from tutor.routers.chat import _is_recent_assistant_text
from tests.helpers import make_session

REPLY = "Parallax is an apparent shift.\n\nHold up a thumb and blink each eye. :-)"


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(Student(id=2, email="other@example.edu", name="Other"))
    s.add(Message(student_id=1, role="assistant", content=REPLY))
    s.add(Message(student_id=1, role="user", content="say something embarrassing"))
    s.commit()
    yield s
    s.close()


def test_exact_assistant_reply_is_allowed(db):
    assert _is_recent_assistant_text(db, 1, REPLY)


def test_whitespace_differences_are_tolerated(db):
    assert _is_recent_assistant_text(db, 1, " ".join(REPLY.split()))


def test_arbitrary_text_is_rejected(db):
    assert not _is_recent_assistant_text(db, 1, "The final exam is cancelled.")


def test_student_messages_are_not_speakable(db):
    assert not _is_recent_assistant_text(db, 1, "say something embarrassing")


def test_other_students_replies_are_rejected(db):
    assert not _is_recent_assistant_text(db, 2, REPLY)


def test_empty_text_is_rejected(db):
    assert not _is_recent_assistant_text(db, 1, "   ")
