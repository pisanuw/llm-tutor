"""Instructor dashboard, Phase 2 of the instructor role: the engagement
metrics, the scoped routes, and the downloads."""

from __future__ import annotations

import csv
import datetime
import io
import json

import pytest
from fastapi.testclient import TestClient

from tutor import class_metrics, classes as classes_mod
from tutor.auth.access import set_instructor
from tutor.db.models import (
    ExerciseAttempt, LearningGoal, MasteryAttempt, Message, Student, StudentProgress,
)
from tests.helpers import make_session

ADMIN = "instructor@example.edu"
NOW = datetime.datetime(2026, 9, 24, 12, 0, 0)
SINCE = NOW - datetime.timedelta(days=7)


@pytest.fixture()
def db(monkeypatch):
    import tutor.config as config

    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    config.get_config.cache_clear()

    s = make_session()
    s.add_all([
        Student(id=1, email=ADMIN, name="Alex Morgan"),
        Student(id=2, email="teach@example.edu", name="Terry Teacher"),
        Student(id=3, email="other@example.edu", name="Olive Other"),
        # keen: active; quiet: logged in, no messages in window; newbie: never
        Student(id=4, email="keen@example.edu", name="Kim Keen", login_count=9,
                last_login=NOW - datetime.timedelta(days=1)),
        Student(id=5, email="quiet@example.edu", name="Quinn Quiet", login_count=2,
                last_login=NOW - datetime.timedelta(days=30)),
        Student(id=6, email="newbie@example.edu", name="Nat New", login_count=0),
        Student(id=7, email="theirs@example.edu", name="Thea Theirs"),
    ])
    s.add_all([
        LearningGoal(id="AST101-L04-telescope-aperture", title="Telescope aperture", description="",
                     course="AST101", lecture_num=4),
        LearningGoal(id="AST101-L03-axial-tilt-seasons", title="Seasons", description="",
                     course="AST101", lecture_num=3),
        LearningGoal(id="AST102-L02-hr-diagram", title="H-R diagram", description="",
                     course="AST102", lecture_num=2),
    ])
    s.commit()
    set_instructor(s, "teach@example.edu", True)
    set_instructor(s, "other@example.edu", True)
    yield s
    s.close()
    config.get_config.cache_clear()


@pytest.fixture()
def seeded(db):
    """An AST101 class for teach@ (keen, quiet, newbie) and an AST102 class for other@."""
    mine = classes_mod.create_class(db, name="AST 101 A", course="AST101",
                                    instructor_email="teach@example.edu",
                                    term="Autumn 2026", created_by=ADMIN)
    theirs = classes_mod.create_class(db, name="AST 102 B", course="AST102",
                                      instructor_email="other@example.edu", created_by=ADMIN)
    for e in ("keen@example.edu", "quiet@example.edu", "newbie@example.edu"):
        classes_mod.enroll(db, mine.id, e)
    classes_mod.enroll(db, theirs.id, "theirs@example.edu")

    # keen: 3 questions across 2 days in the window, plus an AST102 message that
    # must NOT count toward the AST101 class.
    db.add_all([
        Message(student_id=4, role="user", content="what is aperture?", course="AST101",
                lecture_num=4, created_at=NOW - datetime.timedelta(days=1)),
        Message(student_id=4, role="assistant", content="Let's look at lecture 4.",
                course="AST101", lecture_num=4, created_at=NOW - datetime.timedelta(days=1)),
        Message(student_id=4, role="user", content="and focal length?", course="AST101",
                lecture_num=4, created_at=NOW - datetime.timedelta(days=1, minutes=5)),
        Message(student_id=4, role="user", content="why are there seasons?", course="AST101",
                lecture_num=3, created_at=NOW - datetime.timedelta(days=3)),
        Message(student_id=4, role="user", content="AST102 question", course="AST102",
                lecture_num=2, created_at=NOW - datetime.timedelta(days=1)),
        # quiet: used it before the window opened.
        Message(student_id=5, role="user", content="old question", course="AST101",
                lecture_num=4, created_at=NOW - datetime.timedelta(days=30)),
    ])
    # keen mastered one AST101 goal and confirmed it; one AST102 goal must be ignored.
    db.add_all([
        StudentProgress(student_id=4, goal_id="AST101-L04-telescope-aperture", status="mastered",
                        seen=True, mastered_at=NOW - datetime.timedelta(days=2),
                        confirmed_at=NOW - datetime.timedelta(days=1)),
        StudentProgress(student_id=4, goal_id="AST101-L03-axial-tilt-seasons", status="unlocked", seen=True),
        StudentProgress(student_id=4, goal_id="AST102-L02-hr-diagram", status="mastered",
                        mastered_at=NOW - datetime.timedelta(days=2)),
    ])
    db.add_all([
        ExerciseAttempt(student_id=4, exercise_id="AST101-L04-telescope-aperture-code", submission="x",
                        score=3.0, passed=True, created_at=NOW - datetime.timedelta(days=2)),
        ExerciseAttempt(student_id=4, exercise_id="AST101-L04-telescope-aperture-debug", submission="y",
                        score=4.0, passed=True, created_at=NOW - datetime.timedelta(days=2)),
        # An AST102 exercise and an out-of-window one: neither counts for this class.
        ExerciseAttempt(student_id=4, exercise_id="AST102-L02-hr-diagram-code", submission="z",
                        score=1.0, passed=False, created_at=NOW - datetime.timedelta(days=2)),
        ExerciseAttempt(student_id=4, exercise_id="AST101-L03-axial-tilt-seasons-code", submission="w",
                        score=0.0, passed=False, created_at=NOW - datetime.timedelta(days=40)),
    ])
    db.add_all([
        MasteryAttempt(student_id=4, goal_id="AST101-L04-telescope-aperture", questions_json="[]",
                       status="passed", completed_at=NOW - datetime.timedelta(days=2)),
        MasteryAttempt(student_id=4, goal_id="AST101-L03-axial-tilt-seasons", questions_json="[]",
                       status="failed", completed_at=NOW - datetime.timedelta(days=2)),
    ])
    db.commit()
    return mine, theirs


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_engagement_classifies_and_scopes_to_the_course(db, seeded):
    mine, _ = seeded
    roster = classes_mod.class_roster(db, mine.id)
    summary, rows = class_metrics.class_report(db, mine, roster, SINCE, NOW, now=NOW)

    by_email = {r.email: r for r in rows}
    keen = by_email["keen@example.edu"]
    assert keen.status == "active"
    assert keen.messages == 3            # the AST102 message is excluded
    assert keen.replies == 1
    assert keen.active_days == 2
    assert keen.goals_mastered == 1      # the AST102 goal is excluded
    assert keen.goals_confirmed == 1
    assert keen.goals_seen == 2
    assert keen.exercises == 2           # AST102 and out-of-window excluded
    assert keen.avg_score == 3.5
    assert keen.exams_passed == 1 and keen.exams_failed == 1

    assert by_email["quiet@example.edu"].status == "dormant"
    assert by_email["quiet@example.edu"].messages == 0
    assert by_email["quiet@example.edu"].last_active is not None
    assert by_email["newbie@example.edu"].status == "never"
    assert by_email["newbie@example.edu"].needs_attention is True
    assert keen.needs_attention is False

    # Least active first, so the students to chase are at the top.
    assert [r.email for r in rows] == ["newbie@example.edu", "quiet@example.edu", "keen@example.edu"]


def test_class_summary_rolls_up(db, seeded):
    mine, _ = seeded
    roster = classes_mod.class_roster(db, mine.id)
    summary, _rows = class_metrics.class_report(db, mine, roster, SINCE, NOW, now=NOW)

    assert (summary.students, summary.active, summary.dormant, summary.never) == (3, 1, 1, 1)
    assert summary.messages == 3 and summary.replies == 1
    assert summary.exercises == 2 and summary.avg_score == 3.5
    assert summary.goals_mastered == 1 and summary.goals_confirmed == 1
    assert summary.exams_passed == 1 and summary.exams_failed == 1
    assert summary.median_messages == 0          # 0, 0, 3
    assert summary.participation == pytest.approx(1 / 3)
    assert summary.label == "AST 101 A (Autumn 2026)"
    assert summary.top_lectures == [(4, 2), (3, 1)]


def test_empty_class_is_not_a_division_by_zero(db, seeded):
    mine, _ = seeded
    for e in ("keen@example.edu", "quiet@example.edu", "newbie@example.edu"):
        classes_mod.unenroll(db, mine.id, e)
    summary, rows = class_metrics.class_report(db, mine, [], SINCE, NOW, now=NOW)
    assert rows == [] and summary.students == 0
    assert summary.participation is None and summary.median_messages == 0.0


# ---------------------------------------------------------------------------
# Route auth
# ---------------------------------------------------------------------------

def _client(db, viewer: Student):
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_student] = lambda: viewer
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def as_teacher(db, seeded, monkeypatch):
    # The routes take their window from the clock; the fixture's rows are
    # dated from NOW, so the clock is NOW too (this rotted on 2026-09-28,
    # when the 7-day window no longer reached the three-day-old message).
    monkeypatch.setattr("tutor.routers.instructor.utcnow", lambda: NOW)
    client = _client(db, db.get(Student, 2))
    yield client, seeded
    from tutor.main import app
    app.dependency_overrides.clear()


def test_plain_student_cannot_reach_the_dashboard(db, seeded):
    client = _client(db, db.get(Student, 4))
    try:
        assert client.get("/instructor").status_code == 403
        assert client.get(f"/instructor/export?class_id={seeded[0].id}").status_code == 403
    finally:
        from tutor.main import app
        app.dependency_overrides.clear()


def test_dashboard_lists_the_roster_least_active_first(as_teacher):
    client, (mine, _theirs) = as_teacher
    r = client.get("/instructor")
    assert r.status_code == 200
    assert "AST 101 A (Autumn 2026)" in r.text
    assert "newbie@example.edu" in r.text and "keen@example.edu" in r.text
    assert "never signed in" in r.text and "quiet" in r.text
    # The other instructor's class and student never appear.
    assert "theirs@example.edu" not in r.text and "AST 102 B" not in r.text
    assert r.text.index("newbie@example.edu") < r.text.index("keen@example.edu")


def test_a_blank_class_id_shows_the_first_class(as_teacher):
    client, (mine, _theirs) = as_teacher
    r = client.get("/instructor?class_id=&days=7")
    assert r.status_code == 200 and "AST 101 A (Autumn 2026)" in r.text
    # A hand-edited class id falls back to the first class, not an error
    # page (bulletproofing R16).
    r = client.get("/instructor?class_id=abc")
    assert r.status_code == 200 and "AST 101 A (Autumn 2026)" in r.text


def test_instructor_cannot_open_another_instructors_class(as_teacher):
    client, (_mine, theirs) = as_teacher
    assert client.get(f"/instructor?class_id={theirs.id}").status_code == 403
    assert client.get(f"/instructor/export?class_id={theirs.id}").status_code == 403
    # An id that does not exist answers the same way, so ids cannot be probed.
    assert client.get("/instructor?class_id=9999").status_code == 403


def test_instructor_cannot_open_a_student_outside_their_roster(as_teacher):
    client, (mine, _theirs) = as_teacher
    ok = client.get(f"/instructor?class_id={mine.id}&student=keen@example.edu")
    assert ok.status_code == 200 and "Recent conversation" in ok.text
    assert "what is aperture?" in ok.text
    # Not enrolled anywhere the viewer teaches.
    bad = client.get(f"/instructor?class_id={mine.id}&student=theirs@example.edu")
    assert bad.status_code == 403
    assert client.get(f"/instructor?class_id={mine.id}&student=ghost@example.edu").status_code == 403


def test_drilldown_shows_only_this_classes_course(as_teacher):
    client, (mine, _theirs) = as_teacher
    r = client.get(f"/instructor?class_id={mine.id}&student=keen@example.edu")
    assert "what is aperture?" in r.text
    assert "AST102 question" not in r.text


def test_admin_can_open_any_class(db, seeded):
    client = _client(db, db.get(Student, 1))
    try:
        for c in seeded:
            assert client.get(f"/instructor?class_id={c.id}").status_code == 200
    finally:
        from tutor.main import app
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

def test_csv_export_has_a_row_per_student(as_teacher):
    client, (mine, _theirs) = as_teacher
    r = client.get(f"/instructor/export?class_id={mine.id}&days=7&format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["email"] for row in rows] == ["newbie@example.edu", "quiet@example.edu", "keen@example.edu"]
    keen = rows[-1]
    assert keen["status"] == "active" and keen["messages"] == "3"
    assert keen["avg_score"] == "3.5" and keen["goals_confirmed"] == "1"
    assert rows[0]["avg_score"] == ""          # None renders empty, not "None"


def test_json_export_carries_the_summary(as_teacher):
    client, (mine, _theirs) = as_teacher
    r = client.get(f"/instructor/export?class_id={mine.id}&days=7&format=json")
    payload = json.loads(r.text)
    assert payload["class"]["name"] == "AST 101 A"
    assert payload["window"]["days"] == 7
    assert payload["summary"]["active"] == 1 and payload["summary"]["never"] == 1
    assert len(payload["students"]) == 3
    assert payload["students"][-1]["email"] == "keen@example.edu"


def test_transcript_download_is_scoped(as_teacher):
    client, (mine, theirs) = as_teacher
    r = client.get(f"/instructor/transcript?class_id={mine.id}&student=keen@example.edu&format=md")
    assert r.status_code == 200
    assert "what is aperture?" in r.text
    assert "AST102 question" not in r.text        # narrowed to the class's course
    assert "tutor-conversation-keen-" in r.headers["content-disposition"]

    body = json.loads(client.get(
        f"/instructor/transcript?class_id={mine.id}&student=keen@example.edu&format=json").text)
    assert body["course"] == "AST101" and body["message_count"] == 4

    assert client.get(
        f"/instructor/transcript?class_id={theirs.id}&student=theirs@example.edu"
    ).status_code == 403
    assert client.get(
        f"/instructor/transcript?class_id={mine.id}&student=theirs@example.edu"
    ).status_code == 403


def test_window_is_clamped(as_teacher):
    client, (mine, _theirs) = as_teacher
    assert client.get(f"/instructor?class_id={mine.id}&days=0").status_code == 200
    assert client.get(f"/instructor?class_id={mine.id}&days=9999").status_code == 200


def test_instructor_can_switch_the_weekly_email_off_and_on(as_teacher, db):
    client, _ = as_teacher
    page = client.get("/instructor").text
    assert "Weekly email: <b" in page and "Turn it off" in page
    r = client.post("/instructor/weekly-email", data={"enabled": "off"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/instructor"
    db.expire_all()
    assert db.get(Student, 2).weekly_email_opt_out is True
    assert "Turn it on" in client.get("/instructor").text
    client.post("/instructor/weekly-email", data={"enabled": "on"}, follow_redirects=False)
    db.expire_all()
    assert db.get(Student, 2).weekly_email_opt_out is False


def test_a_student_cannot_touch_the_weekly_email_switch(db, seeded):
    client = _client(db, db.get(Student, 4))
    try:
        assert client.post("/instructor/weekly-email", data={"enabled": "off"}).status_code == 403
    finally:
        from tutor.main import app
        app.dependency_overrides.clear()


def test_instructor_with_no_classes_sees_an_explanation(db):
    from tutor.main import app

    client = _client(db, db.get(Student, 2))
    try:
        r = client.get("/instructor")
        assert r.status_code == 200 and "No classes yet." in r.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Header link
# ---------------------------------------------------------------------------

def test_instructor_link_shows_only_for_instructors(db, seeded):
    from tutor.main import app
    from tutor.db.models import StudentPath, Track

    db.add(Track(id="AST101-lecture-order", course="AST101", title="Lecture Order",
                 description="", goal_ids_json="[]", graph_version=1))
    db.add(StudentPath(student_id=2, course="AST101", track_id="AST101-lecture-order"))
    db.add(StudentPath(student_id=4, course="AST101", track_id="AST101-lecture-order"))
    db.commit()

    client = _client(db, db.get(Student, 2))
    try:
        assert 'href="/instructor"' in client.get("/chat").text
    finally:
        app.dependency_overrides.clear()

    client = _client(db, db.get(Student, 4))
    try:
        assert 'href="/instructor"' not in client.get("/chat").text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Several instructors per class (2026-10-06)
# ---------------------------------------------------------------------------

def test_a_co_instructor_sees_the_class_like_the_lead(db, seeded):
    mine, theirs = seeded
    assert classes_mod.add_instructor(db, mine.id, "other@example.edu", added_by="teach@example.edu") == "added_no_login"
    client = _client(db, db.get(Student, 3))
    try:
        r = client.get(f"/instructor?class_id={mine.id}")
        assert r.status_code == 200
        assert "AST 101 A (Autumn 2026)" in r.text and "keen@example.edu" in r.text
        assert client.get(f"/instructor/export?class_id={mine.id}&format=csv").status_code == 200
        # Both classes on the overview; the chips name both instructors.
        assert "AST 101 A" in client.get("/instructor?view=all").text
        assert 'data-instructor="other@example.edu"' in r.text and 'data-instructor="teach@example.edu"' in r.text
    finally:
        from tutor.main import app
        app.dependency_overrides.clear()


def test_instructor_adds_and_removes_a_co_instructor(as_teacher, db):
    client, (mine, theirs) = as_teacher
    from tutor.db.models import WhitelistEntry
    db.add(WhitelistEntry(email="other@example.edu"))
    db.commit()

    page = client.get(f"/instructor?class_id={mine.id}").text
    assert "Add instructor" in page and "(you)" in page
    assert 'action="/instructor/instructors/remove"' not in page      # alone: nothing to remove

    r = client.post("/instructor/instructors/add",
                    data={"class_id": mine.id, "email": "other@example.edu"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/instructor?class_id={mine.id}&note=added"
    page = client.get(r.headers["location"]).text
    assert "Added." in page
    assert 'data-instructor="other@example.edu"' in page
    assert 'action="/instructor/instructors/remove"' in page           # the other one is removable
    assert classes_mod.instructor_ids(db, mine.id) == [2, 3]

    # Not oneself.
    r = client.post("/instructor/instructors/remove",
                    data={"class_id": mine.id, "email": "teach@example.edu"}, follow_redirects=False)
    assert r.headers["location"].endswith("&note=self")
    assert "cannot remove yourself" in client.get(r.headers["location"]).text
    assert classes_mod.instructor_ids(db, mine.id) == [2, 3]

    # A whitelisted-nowhere address is added with the warning.
    r = client.post("/instructor/instructors/add",
                    data={"class_id": mine.id, "email": "guest@example.edu"}, follow_redirects=False)
    assert r.headers["location"].endswith("&note=added_no_login")

    r = client.post("/instructor/instructors/remove",
                    data={"class_id": mine.id, "email": "other@example.edu"}, follow_redirects=False)
    assert r.headers["location"].endswith("&note=removed")
    assert classes_mod.instructor_ids(db, mine.id) == [2, db.query(Student).filter_by(
        email="guest@example.edu").one().id]

    # Another instructor's class is out of reach, add and remove alike.
    assert client.post("/instructor/instructors/add",
                       data={"class_id": theirs.id, "email": "teach@example.edu"}).status_code == 403
    assert client.post("/instructor/instructors/remove",
                       data={"class_id": theirs.id, "email": "other@example.edu"}).status_code == 403
    assert classes_mod.instructor_ids(db, theirs.id) == [3]


def test_a_student_cannot_touch_a_classes_instructors(db, seeded):
    mine, _ = seeded
    client = _client(db, db.get(Student, 4))
    try:
        assert client.post("/instructor/instructors/add",
                           data={"class_id": mine.id, "email": "keen@example.edu"}).status_code == 403
        assert client.post("/instructor/instructors/remove",
                           data={"class_id": mine.id, "email": "teach@example.edu"}).status_code == 403
    finally:
        from tutor.main import app
        app.dependency_overrides.clear()
    assert classes_mod.instructor_ids(db, mine.id) == [2]


def test_the_instructors_block_fits_a_phone(browser, db, seeded, monkeypatch):
    from tests.browser_support import ORIGIN, app_client, open_page

    from tutor.db.models import WhitelistEntry

    mine, _ = seeded
    classes_mod.add_instructor(db, mine.id, "other@example.edu")
    db.add(WhitelistEntry(email="@example.edu", added_by="t"))     # the real sign-in dependency runs here
    db.commit()
    monkeypatch.setattr("tutor.routers.instructor.utcnow", lambda: NOW)
    with app_client(db, student_id=2) as client:
        with open_page(browser, client, width=390, height=844, clock=False, touch=True) as (page, net):
            page.goto(f"{ORIGIN}/instructor?class_id={mine.id}")
            page.wait_for_selector("[data-instructors]")
            assert page.evaluate("() => document.documentElement.scrollWidth") <= 390
            assert page.locator("[data-instructors] [data-instructor]").count() == 2
            assert page.locator("[data-add-instructor] button").is_visible()
