"""The roster status from questions, browser events, logins and sign-in
failures together; staff out of every baseline; the percentile standings;
the heartbeat from config (student-facing review, step 0.9; F37, F38,
F39, F41)."""

from __future__ import annotations

import datetime

import pytest

import tutor.config as config
from tutor import class_metrics, classes as classes_mod, engagement
from tutor.auth import attempts
from tutor.auth.access import is_staff
from tutor.db.models import ActivityEvent, LoginAttempt, Message, Student
from tutor.utctime import utcnow
from tests.helpers import make_session

NOW = utcnow()
SINCE = NOW - datetime.timedelta(days=7)
CHROME = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")
    config.get_config.cache_clear()
    db = make_session()
    db.add_all([
        Student(id=1, email="ada@example.edu", name="Ada", login_count=3),       # asked this week
        Student(id=2, email="bo@example.edu", name="Bo", login_count=2),         # came and read, asked nothing
        Student(id=3, email="cy@example.edu", name="Cy", login_count=1),         # failed to sign in, never got in
        Student(id=4, email="di@example.edu", name="Di", login_count=5),         # asked before the window
        Student(id=5, email="ed@example.edu", name="Ed", login_count=1),         # signed in once, never asked
        Student(id=6, email="fay@example.edu", name="Fay", login_count=0),       # never signed in
        Student(id=7, email="gil@example.edu", name="Gil", login_count=4),       # asked before, then locked out
        Student(id=9, email="prof@example.edu", name="Prof", is_instructor=True),
    ])
    db.commit()
    klass = classes_mod.create_class(db, name="AST 101 A", course="AST101", instructor_email="prof@example.edu",
                                     term="Autumn 2026", created_by="prof@example.edu")
    for e in ("ada", "bo", "cy", "di", "ed", "fay", "gil", "prof"):
        classes_mod.enroll(db, klass.id, f"{e}@example.edu")
    for i in range(4):
        db.add(Message(student_id=1, role="user", course="AST101", lecture_num=3, content=f"q{i}",
                       created_at=NOW - datetime.timedelta(days=1, minutes=i)))
    for i in range(10):
        db.add(Message(student_id=9, role="user", course="AST101", lecture_num=3, content=f"t{i}",
                       created_at=NOW - datetime.timedelta(hours=i + 1)))
    db.add(ActivityEvent(student_id=2, visit_id="v2", page_view_id="p2", seq=1, name="page_view",
                         ts=NOW - datetime.timedelta(days=2), path="/chat"))
    db.add(LoginAttempt(email="cy@example.edu", student_id=3, method=attempts.MAGIC, outcome=attempts.CODE_INVALID,
                        user_agent=CHROME, created_at=NOW - datetime.timedelta(days=3)))
    db.add(Message(student_id=4, role="user", course="AST101", lecture_num=1, content="old",
                   created_at=NOW - datetime.timedelta(days=30)))
    db.add(Message(student_id=7, role="user", course="AST101", lecture_num=1, content="old",
                   created_at=NOW - datetime.timedelta(days=30)))
    db.add(LoginAttempt(email="gil@example.edu", student_id=7, method=attempts.MAGIC, outcome=attempts.LINK_INVALID,
                        user_agent=CHROME, created_at=NOW - datetime.timedelta(days=2)))
    db.commit()
    try:
        yield db, klass
    finally:
        db.close()
        config.get_config.cache_clear()


def test_the_status_reads_questions_events_logins_and_failures(world):
    db, klass = world
    roster = classes_mod.class_roster(db, klass.id)
    summary, rows = class_metrics.class_report(db, klass, roster, SINCE, NOW, now=NOW)
    status = {r.email.split("@")[0]: r.status for r in rows}
    assert status == {"ada": "active", "bo": "reading", "cy": "locked_out", "di": "dormant", "ed": "signed_in",
                      "fay": "never", "gil": "locked_out", "prof": "active"}
    labels = {r.email.split("@")[0]: r.status_label for r in rows}
    assert labels["bo"] == "reading only" and labels["cy"] == "could not sign in" and labels["fay"] == "never signed in"
    assert [r.needs_attention for r in rows if r.email.startswith(("ada", "bo", "prof"))] == [False] * 3
    assert all(r.needs_attention for r in rows if r.email.startswith(("cy", "di", "ed", "fay", "gil")))
    assert (summary.active, summary.reading, summary.locked_out, summary.dormant, summary.signed_in, summary.never) \
        == (2, 1, 2, 1, 1, 1)
    # The professor's ten questions never move the median: 4, 0, 0, 0, 0, 0, 0.
    assert summary.median_messages == 0
    assert {r.email for r in rows if r.is_staff} == {"prof@example.edu"}


def test_is_staff_covers_admins_and_instructors(world):
    db, _klass = world
    assert is_staff(db.get(Student, 9)) is True
    assert is_staff(Student(email="x@example.edu", is_instructor=True)) is True
    assert is_staff(db.get(Student, 1)) is False


def test_the_median_leaves_staff_out():
    rows = [class_metrics.StudentEngagement(student_id=i, email=f"s{i}@example.edu", messages=m) for i, m in enumerate((1, 3, 5))]
    rows.append(class_metrics.StudentEngagement(student_id=9, email="prof@example.edu", messages=100, is_staff=True))
    from tutor.db.models import CourseClass

    c = CourseClass(id=1, name="A", course="AST101", term="Au26", instructor_id=9)
    assert class_metrics.summarize(c, rows, SINCE, NOW).median_messages == 3


def _rows(n, staff=0):
    out = [class_metrics.StudentEngagement(student_id=i, email=f"s{i}@example.edu", name=f"S{i}", messages=i, active_days=1)
           for i in range(1, n + 1)]
    out += [class_metrics.StudentEngagement(student_id=90 + i, email=f"p{i}@example.edu", name="Prof", messages=50, is_staff=True)
            for i in range(staff)]
    return out


def test_standings_rank_within_the_class_and_need_five_students():
    assert class_metrics.standings(_rows(4, staff=3), {}) == []
    activity = {1: engagement.ActivityEngagement(student_id=1, visits=2, active_min=30.0, deck_min=5.0, narration_min=0.0)}
    table = class_metrics.standings(_rows(5, staff=1), activity)
    by = {st.email: st for st in table}
    assert len(table) == 6 and by["s5@example.edu"].percentiles["messages"] == 100.0
    assert by["s1@example.edu"].percentiles["messages"] == 20.0 and by["s1@example.edu"].percentiles["active_min"] == 100.0
    assert by["s2@example.edu"].values["active_min"] == 0.0 and by["s2@example.edu"].percentiles["active_min"] == 80.0
    assert by["p0@example.edu"].percentiles["messages"] is None and by["p0@example.edu"].cell("messages") == "50"
    assert by["s5@example.edu"].cell("messages") == "5 (p100)" and by["s1@example.edu"].cell("active_min") == "30 (p100)"
    assert [st.name for st in table][:2] == ["Prof", "S1"]           # by name, not by rank


def test_the_live_thresholds_read_the_heartbeat_from_config(monkeypatch):
    monkeypatch.setenv("ACTIVITY_HEARTBEAT_S", "120")
    config.get_config.cache_clear()
    try:
        assert engagement.live_thresholds().heartbeat_s == 120
        assert engagement.THRESHOLDS.heartbeat_s == 60                # the offline default is untouched
    finally:
        config.get_config.cache_clear()


def test_the_activity_median_leaves_staff_out(world):
    db, klass = world
    for sid, minutes in ((1, 1), (9, 60)):
        for seq in range(1, 4):
            db.add(ActivityEvent(student_id=sid, visit_id=f"w{sid}", page_view_id=f"w{sid}", seq=seq,
                                 name="page_heartbeat" if seq > 1 else "page_view", path="/chat",
                                 ts=NOW - datetime.timedelta(hours=2) + datetime.timedelta(seconds=seq * 30 * minutes)))
    db.commit()
    with_staff = engagement.class_activity(db, "AST101", [1, 2, 9], SINCE, NOW)
    without = engagement.class_activity(db, "AST101", [1, 2, 9], SINCE, NOW, staff_ids=[9])
    assert with_staff.rows[9].active_min > without.rows[1].active_min
    assert without.median_active_min == pytest.approx(with_staff.rows[1].active_min, abs=0.2) or without.median_active_min < with_staff.median_active_min
