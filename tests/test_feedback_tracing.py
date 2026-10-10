"""A thumbs up or down reaches its conversation in one click
(student-facing review, step 0.4; findings F14 to F22, F25).

F14: a reply older than the student's last 20 could not be rated. F17: the
daily email's link lost its destination when signed out. F18: the anchored
message could be off the page. F19: nothing highlighted it. F20: two link
shapes. F21: thumbs-up invisible. F22: a re-rating erased the complaint.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor import conversations as conv
from tutor.db.models import (
    ClassEnrollment, ClassInstructor, CourseClass, FeedbackEvent, Message, MessageFeedback, Student,
    WhitelistEntry,
)
from tutor.utctime import utcnow
from tests.helpers import login_cookie, make_session

T0 = datetime.datetime(2026, 10, 1, 15, 0, 0)


@pytest.fixture
def world(monkeypatch):
    """Sam (a student with 60 messages), Prof (the admin), Kim (an instructor
    of Sam's class) and Lee (an instructor of another class)."""
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session()
    sam = Student(id=1, email="sam@example.edu", name="Sam")
    prof = Student(id=2, email="prof@example.edu", name="Prof")
    kim = Student(id=3, email="kim@example.edu", name="Kim", is_instructor=True)
    lee = Student(id=4, email="lee@example.edu", name="Lee", is_instructor=True)
    db.add_all([sam, prof, kim, lee])
    for s in (sam, prof, kim, lee):
        db.add(WhitelistEntry(email=s.email, added_by="test"))
    db.flush()
    mine = CourseClass(name="AST 101 A", course="AST101", term="Autumn 2026", instructor_id=3)
    other = CourseClass(name="AST 102 B", course="AST102", term="Autumn 2026", instructor_id=4)
    db.add_all([mine, other])
    db.flush()
    db.add(ClassInstructor(class_id=mine.id, student_id=3))
    db.add(ClassInstructor(class_id=other.id, student_id=4))
    db.add(ClassEnrollment(class_id=mine.id, student_id=1))
    for i in range(30):
        db.add(Message(student_id=1, role="user", content=f"question {i}", course="AST101", lecture_num=4,
                       created_at=T0 + datetime.timedelta(minutes=2 * i)))
        db.add(Message(student_id=1, role="assistant", content=f"reply {i}", course="AST101", lecture_num=4,
                       created_at=T0 + datetime.timedelta(minutes=2 * i + 1), turn_id=f"{i:032x}"))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    try:
        yield client, db, {"mine": mine.id, "other": other.id}
    finally:
        app.dependency_overrides.clear()
        db.close()
        config.get_config.cache_clear()


def _as(client, db, student_id):
    client.cookies.clear()
    client.cookies.set("session_token", login_cookie(db, student_id))


def _reply(db, i):
    return db.query(Message).filter_by(student_id=1, role="assistant", content=f"reply {i}").one()


# ---------------------------------------------------------------------------
# Rating by id, the history's rating, the event log
# ---------------------------------------------------------------------------

def test_an_old_reply_can_be_rated_by_its_id_and_the_thumb_is_remembered(world):
    client, db, _ = world
    _as(client, db, 1)
    old = _reply(db, 2)                                   # far older than the newest 20
    r = client.post("/chat/feedback", data={"message_id": old.id, "text": "whatever the page had", "rating": "-1",
                                            "comment": "It skipped the new Moon."})
    assert r.status_code == 200 and r.json()["message_id"] == old.id
    history = client.get("/chat/history?course=AST101").json()
    rated = [m for m in history if m["id"] == old.id]
    assert rated and rated[0]["rating"] == -1
    assert [m["rating"] for m in history if m["role"] == "user"] == [None] * 30
    # Another student's reply, or a student's own question, is nobody's to rate.
    _as(client, db, 3)
    assert client.post("/chat/feedback", data={"message_id": old.id, "text": "x", "rating": "1"}).status_code == 404


def test_every_rating_is_kept_and_a_new_one_clears_the_seen_mark(world):
    client, db, _ = world
    _as(client, db, 1)
    reply = _reply(db, 5)
    client.post("/chat/feedback", data={"message_id": reply.id, "text": "", "rating": "-1", "comment": "wrong"})
    fb = db.query(MessageFeedback).one()
    from tutor.agent import state

    state.mark_feedback_seen(db, fb.id)
    db.commit()
    assert db.get(MessageFeedback, fb.id).seen_at is not None
    client.post("/chat/feedback", data={"message_id": reply.id, "text": "", "rating": "1"})
    db.expire_all()
    live = db.get(MessageFeedback, fb.id)
    assert (live.rating, live.comment, live.seen_at) == (1, None, None)          # the live row: the latest
    events = db.query(FeedbackEvent).order_by(FeedbackEvent.id).all()
    assert [(e.rating, e.comment) for e in events] == [(-1, "wrong"), (1, None)]  # the history: everything
    assert {e.message_id for e in events} == {reply.id} and {e.student_id for e in events} == {1}


def test_a_cap_notice_gets_no_thumbs_in_the_page_script():
    from tutor.templating import STATIC_DIR

    script = (STATIC_DIR / "chat.js").read_text()
    assert "if (messageId) row.appendChild(buildFeedbackControls(speakText, messageId, rating));" in script
    assert "fd.append('message_id', String(messageId))" in script


# ---------------------------------------------------------------------------
# One link, for whoever opens it
# ---------------------------------------------------------------------------

def _flag(client, db, i=2, comment="It skipped the new Moon."):
    _as(client, db, 1)
    reply = _reply(db, i)
    client.post("/chat/feedback", data={"message_id": reply.id, "text": "", "rating": "-1", "comment": comment})
    return reply, db.query(MessageFeedback).filter_by(message_id=reply.id).one()


def test_the_link_sends_each_reader_to_their_own_transcript_around_the_reply(world):
    client, db, ids = world
    reply, fb = _flag(client, db)
    link = f"/feedback/{fb.id}"

    client.cookies.clear()                                           # signed out: the login page, destination kept
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/auth/login?next=%2Ffeedback%2F{fb.id}"

    _as(client, db, 2)                                               # the admin, by their own login
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin?tab=conversations&student=sam%40example.edu&msg={reply.id}#msg-{reply.id}"
    page = client.get(r.headers["location"].split("#")[0]).text
    assert f'id="msg-{reply.id}"' in page and "target:ring-2" in page and "It skipped the new Moon." in page
    assert "reply 0" in page and "reply 29" in page                  # the window holds the old reply's neighbours

    _as(client, db, 3)                                               # Kim teaches Sam's class
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == (f"/instructor/transcript/view?class_id={ids['mine']}&student=sam%40example.edu"
                                     f"&msg={reply.id}#msg-{reply.id}")
    page = client.get(r.headers["location"].split("#")[0]).text
    assert f'id="msg-{reply.id}"' in page and "target:ring-2" in page

    _as(client, db, 4)                                               # Lee does not
    assert client.get(link, follow_redirects=False).status_code == 403
    assert client.get("/feedback/999999", follow_redirects=False).status_code == 404


def test_the_admin_login_round_trip_keeps_the_destination(world):
    client, db, _ = world
    client.cookies.clear()
    r = client.get("/admin?tab=conversations&student=sam%40example.edu&msg=7", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login?next=/admin%3Ftab%3Dconversations%26student%3Dsam%2540example.edu%26msg%3D7"
    r = client.get("/admin?tab=access&utm_source=mail", follow_redirects=False)   # only the page's own parameters ride along
    assert r.headers["location"] == "/auth/login?next=/admin%3Ftab%3Daccess"


def test_the_window_around_a_message_and_the_newest_300_agree_on_feedback(world):
    client, db, _ = world
    reply, fb = _flag(client, db, i=10)
    around = conv.thread_around(db, 1, reply.id, before=3, after=2)
    assert [m.content for m in around] == ["question 9", "reply 9", "question 10", "reply 10",
                                           "question 11", "reply 11"]
    assert around[3].feedback is not None and around[3].feedback.comment == "It skipped the new Moon."
    assert conv.thread_around(db, 1, reply.id, course="AST102") == []


def test_thumbs_up_is_counted_and_badged(world):
    client, db, _ = world
    reply, _fb = _flag(client, db, i=20)
    _as(client, db, 1)
    client.post("/chat/feedback", data={"message_id": _reply(db, 21).id, "text": "", "rating": "1"})
    from tutor.routers.admin import issue_admin_session

    client.cookies.clear()
    client.cookies.set("admin_session", issue_admin_session())
    page = client.get("/admin?tab=conversations&days=30").text
    assert 'data-badge="thumbs-up">👍 1<' in page and 'data-badge="thumbs">👎 1<' in page


def test_the_today_panel_lists_the_flag_with_its_seen_mark(world):
    client, db, _ = world
    reply, fb = _flag(client, db, i=25)
    from tutor.routers.admin import issue_admin_session

    client.cookies.clear()
    client.cookies.set("admin_session", issue_admin_session())
    page = client.get("/admin").text
    assert "Replies students flagged (1, 1 unseen)" in page and f'href="/feedback/{fb.id}"' in page
    assert f'action="/admin/feedback/{fb.id}/seen"' in page and "Mark seen" in page
    r = client.post(f"/admin/feedback/{fb.id}/seen", data={"seen": "1"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/admin").text
    assert "Replies students flagged (1, 0 unseen)" in page and "Mark unseen" in page
    assert client.post("/admin/feedback/999999/seen", data={"seen": "1"}).status_code == 404


def test_the_daily_email_links_the_one_link_in_local_time(world):
    client, db, _ = world
    reply, fb = _flag(client, db, i=12)
    from tutor import feedback_report

    flags = feedback_report.thumbs_down_in_window(db, utcnow() - datetime.timedelta(days=1), utcnow())
    assert [f.feedback_id for f in flags] == [fb.id]
    md = feedback_report.render_report_markdown(flags, "2026-10-07", "https://tutor.example.edu")
    assert f"Link: https://tutor.example.edu/feedback/{fb.id}" in md and " UTC" not in md
    assert "for an admin or the class instructor" in md
    html = feedback_report.render_report_html(flags, "2026-10-07", "https://tutor.example.edu")
    assert f"href='https://tutor.example.edu/feedback/{fb.id}'" in html
