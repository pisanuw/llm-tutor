"""Live activity engagement (activity analytics, phase 4):
the instructor dashboard's on-screen, lecture and narration columns, the
student drill-down's lectures table, the weekly email's two usage lines,
and the admin Timeline tab, all from the browser's activity events.
"""
from __future__ import annotations

import datetime
import json

import pytest
from fastapi.testclient import TestClient

from tests.helpers import login_cookie, make_session
from tutor import classes as classes_mod, engagement, instructor_report
from tutor.auth.access import set_instructor
from tutor.db.models import ActivityEvent, LearningGoal, Message, Student, TurnEvent, WhitelistEntry

ADMIN = "instructor@example.edu"
NOW = datetime.datetime(2026, 10, 5, 12, 0, 0)
SINCE = NOW - datetime.timedelta(days=7)


def at(minutes: float, day_offset: int = -1) -> datetime.datetime:
    return NOW + datetime.timedelta(days=day_offset, minutes=minutes)


def _event(sid, name, ts, seq, page="p", **fields):
    detail = {k: v for k, v in fields.items() if k not in
              ("course", "lecture_num", "slide_num", "value_ms", "turn_id", "message_id", "path", "label")}
    return ActivityEvent(
        student_id=sid, visit_id=f"v{sid}", page_view_id=f"{page}{sid}", seq=seq, name=name, ts=ts,
        course=fields.get("course", "AST101"), lecture_num=fields.get("lecture_num"), slide_num=fields.get("slide_num"),
        value_ms=fields.get("value_ms"), turn_id=fields.get("turn_id"), message_id=fields.get("message_id"),
        path=fields.get("path", "/chat"), label=fields.get("label"),
        detail_json=json.dumps(detail, sort_keys=True) if detail else None,
    )


@pytest.fixture
def db(monkeypatch):
    import tutor.config as config

    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    config.get_config.cache_clear()
    s = make_session()
    s.add_all([
        Student(id=1, email=ADMIN, name="Alex Morgan"),
        Student(id=2, email="teach@example.edu", name="Terry Teacher"),
        Student(id=4, email="keen@example.edu", name="Kim Keen", login_count=3, last_login=at(0)),
        Student(id=5, email="quiet@example.edu", name="Quinn Quiet"),
        WhitelistEntry(email="teach@example.edu", added_by="t"),
        LearningGoal(id="AST101-L03-sea", title="Seasons", description="", course="AST101", lecture_num=3),
    ])
    s.commit()
    set_instructor(s, "teach@example.edu", True)
    klass = classes_mod.create_class(s, name="AST 101 A", course="AST101", instructor_email="teach@example.edu",
                                     term="Autumn 2026", created_by=ADMIN)
    for e in ("keen@example.edu", "quiet@example.edu"):
        classes_mod.enroll(s, klass.id, e)
    # keen: a question, a reply, the lecture-3 deck for a while with narration.
    q = Message(student_id=4, role="user", content="seasons?", course="AST101", lecture_num=3, created_at=at(0))
    a = Message(student_id=4, role="assistant", content="The tilt of Earth's axis.", course="AST101",
                lecture_num=3, created_at=at(0.2))
    s.add_all([q, a])
    s.flush()
    s.add(TurnEvent(student_id=4, kind="chat", turn_id="abc123", ts=at(0.02), goal_ids_json='["AST101-L03-sea"]',
                    verdicts_json='{"verdict": "allow"}'))
    s.add_all([
        _event(4, "page_view", at(-0.5), 1, vw=1280, vh=800),
        _event(4, "question_sent", at(0), 2, lecture_num=3, chars=10, kind="typed"),
        _event(4, "reply_done", at(0.25), 3, lecture_num=3, turn_id="abc123", message_id=a.id, value_ms=15000,
               chars=26, outcome="done"),
        _event(4, "deck_open", at(1), 1, page="d", lecture_num=3, slide_num=1, context="chat", has_audio=True,
               slides_total=6, path="/slides/AST101/lecture-03/index.html"),
        _event(4, "slide_view", at(2), 2, page="d", lecture_num=3, slide_num=2, value_ms=60000, prev_slide=1,
               how="next", path="/slides/AST101/lecture-03/index.html"),
        _event(4, "slide_view", at(3), 3, page="d", lecture_num=3, slide_num=3, value_ms=60000, prev_slide=2,
               how="next", path="/slides/AST101/lecture-03/index.html"),
        _event(4, "narration_play", at(3.1), 4, page="d", lecture_num=3, slide_num=3, value_ms=0, position_s=0,
               path="/slides/AST101/lecture-03/index.html"),
        _event(4, "narration_ended", at(5), 5, page="d", lecture_num=3, slide_num=3, value_ms=114000,
               position_s=114, path="/slides/AST101/lecture-03/index.html"),
        _event(4, "deck_close", at(5.5), 6, page="d", lecture_num=3, slide_num=3, value_ms=150000,
               active_ms=270000, slides_seen=3, path="/slides/AST101/lecture-03/index.html"),
        _event(4, "page_hide", at(6), 4, value_ms=390000),
    ])
    s.commit()
    yield s, klass
    s.close()
    config.get_config.cache_clear()


def test_class_activity_measures_the_roster(db):
    session, klass = db
    activity = engagement.class_activity(session, "AST101", [4, 5], SINCE, NOW)
    keen = activity.rows[4]
    # Each signal holds for at most 75 s: the 114 s clip leaves a 39 s hole,
    # and the server-side reply row cuts the hold for 3 s.
    assert keen.visits == 1 and 5.0 <= keen.active_min <= 5.6
    assert keen.deck_min == pytest.approx(3.9, abs=0.1)
    assert keen.narration_min == 1.9 and keen.lectures_opened == 1 and keen.slides_seen == 3
    assert keen.last_seen == at(6)
    assert keen.lectures[0].dwell_s == {1: 60, 2: 60, 3: 150}
    quiet = activity.rows[5]
    assert quiet.visits == 0 and quiet.active_min == 0 and quiet.lectures_label == "0"
    assert activity.total_visits == 1 and activity.students_with_deck == 1
    assert activity.median_active_min == keen.active_min


@pytest.fixture
def teacher(db):
    from tutor.auth.deps import get_db
    from tutor.main import app

    session, klass = db
    app.dependency_overrides[get_db] = lambda: session
    client = TestClient(app)
    client.cookies.set("session_token", login_cookie(session, student_id=2))
    return client, klass


def test_the_dashboard_shows_time_lectures_and_narration(teacher):
    client, klass = teacher
    html = client.get(f"/instructor?class_id={klass.id}&days=7").text
    assert "On screen" in html and "Narration" in html
    assert "data-active-min" in html and "min over 1 visit" in html
    assert "1 student opened a deck" in html
    page = client.get(f"/instructor?class_id={klass.id}&days=7&student=keen%40example.edu").text
    assert "data-detail-activity" in page and "data-detail-lectures" in page
    assert "L03 Seasons" in page or "L03" in page
    assert "3 slides" in page and "narration 1.9 min" in page


def test_the_weekly_email_says_how_much_time_the_class_spent(db):
    session, klass = db
    teacher = session.get(Student, 2)
    weeks = instructor_report.gather(session, teacher, SINCE, NOW, now=NOW)
    (week,) = weeks
    lines = week.activity_lines()
    assert lines[0].startswith("**Time on the site:**") and "over 1 visit" in lines[0]
    assert lines[1] == "**Lectures:** 1 student opened a lecture deck; 0 lectures listened through"
    md = instructor_report.render_markdown(teacher, weeks, "this week", "https://x")
    assert "- **Time on the site:**" in md
    html = instructor_report.render_html(teacher, weeks, "this week", "https://x")
    assert "<li><b>Time on the site:</b>" in html and "<li><b>Lectures:</b> 1 student opened" in html


def test_a_quiet_class_adds_no_usage_lines(db):
    session, klass = db
    session.query(ActivityEvent).delete()
    session.commit()
    teacher = session.get(Student, 2)
    (week,) = instructor_report.gather(session, teacher, SINCE, NOW, now=NOW)
    assert week.activity_lines() == []


def test_the_admin_timeline_tab_tells_the_last_two_days(db, monkeypatch):
    import tutor.config as config
    from tutor.auth.deps import get_db
    from tutor.main import app

    session, klass = db
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-timeline")
    config.get_config.cache_clear()
    app.dependency_overrides[get_db] = lambda: session
    client = TestClient(app, base_url="http://localhost")   # the admin cookie is Secure elsewhere
    assert client.post("/admin/login", data={"token": "tkn-timeline"}, follow_redirects=False).status_code == 303
    empty = client.get("/admin?tab=timeline").text
    assert "Enter a student email" in empty
    # The tab reads the last 48 hours from the real clock; pin "now" to the fixture's day.
    monkeypatch.setattr(engagement, "recent_timeline",
                        lambda db, sid, hours=48, now=None, thresholds=engagement.THRESHOLDS:
                        _real_recent_timeline(db, sid, hours=hours, now=NOW + datetime.timedelta(hours=1),
                                              thresholds=thresholds))
    page = client.get("/admin?tab=timeline&student=keen%40example.edu").text
    assert "data-timeline" in page and "question" in page and "AST101-L03 (Seasons)" in page
    assert "slide 2, 60 s on slide 1" in page and "114 s played" in page
    nobody = client.get("/admin?tab=timeline&student=quiet%40example.edu").text
    assert "Nothing recorded for this student" in nobody


_real_recent_timeline = engagement.recent_timeline
