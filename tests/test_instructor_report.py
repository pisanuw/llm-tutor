"""Weekly instructor report, Phase 3 of the instructor role: who is
eligible, what the email says, and the in-app preview."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from tutor import classes as classes_mod, instructor_report as report
from tutor.auth.access import set_instructor
from tutor.db.models import LearningGoal, Message, MessageFeedback, Student, StudentProgress
from tests.helpers import make_session

ADMIN = "instructor@example.edu"
NOW = datetime.datetime(2026, 9, 28, 12, 0, 0)
SINCE = NOW - datetime.timedelta(days=7)
LABEL = "Sep 21 – Sep 28, 2026"
BASE = "https://tutor.example.edu"


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
        Student(id=4, email="keen@example.edu", name="Kim Keen", login_count=4,
                last_login=NOW - datetime.timedelta(days=1)),
        Student(id=5, email="quiet@example.edu", name="Quinn Quiet", login_count=1,
                last_login=NOW - datetime.timedelta(days=40)),
        Student(id=6, email="newbie@example.edu", name="Nat New", login_count=0),
    ])
    s.add(LearningGoal(id="AST101-L04-telescope-aperture", title="Telescope aperture", description="",
                       course="AST101", lecture_num=4))
    s.commit()
    set_instructor(s, "teach@example.edu", True)
    set_instructor(s, "other@example.edu", True)
    yield s
    s.close()
    config.get_config.cache_clear()


@pytest.fixture()
def klass(db):
    c = classes_mod.create_class(db, name="AST 101 A", course="AST101",
                                 instructor_email="teach@example.edu",
                                 term="Autumn 2026", created_by=ADMIN)
    for e in ("keen@example.edu", "quiet@example.edu", "newbie@example.edu"):
        classes_mod.enroll(db, c.id, e)
    db.add_all([
        Message(student_id=4, role="user", content="aperture?", course="AST101",
                lecture_num=4, created_at=NOW - datetime.timedelta(days=2)),
        Message(student_id=4, role="user", content="and again?", course="AST101",
                lecture_num=4, created_at=NOW - datetime.timedelta(days=1)),
        Message(student_id=5, role="user", content="way back", course="AST101",
                lecture_num=4, created_at=NOW - datetime.timedelta(days=40)),
    ])
    db.add(StudentProgress(student_id=4, goal_id="AST101-L04-telescope-aperture", status="mastered",
                           seen=True, mastered_at=NOW - datetime.timedelta(days=3),
                           confirmed_at=NOW - datetime.timedelta(days=2)))
    db.commit()
    return c


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def test_only_reportable_classes_count(db, klass):
    teacher = db.get(Student, 2)
    assert [c.id for c in report.eligible_classes(db, teacher)] == [klass.id]
    assert [i.email for i in report.eligible_instructors(db)] == ["teach@example.edu"]

    # An empty class is not worth an email.
    empty = classes_mod.create_class(db, name="AST 101 B", course="AST101",
                                     instructor_email="teach@example.edu", created_by=ADMIN)
    assert [c.id for c in report.eligible_classes(db, teacher)] == [klass.id]
    classes_mod.enroll(db, empty.id, "keen@example.edu")
    assert len(report.eligible_classes(db, teacher)) == 2

    # The per-class switch and archiving both remove it.
    classes_mod.update_class(db, empty.id, weekly_report=False)
    assert [c.id for c in report.eligible_classes(db, teacher)] == [klass.id]
    classes_mod.update_class(db, klass.id, archived=True)
    assert report.eligible_classes(db, teacher) == []
    assert report.eligible_instructors(db) == []


def test_a_class_outside_its_quarter_is_not_reported(db, klass):
    classes_mod.update_class(db, klass.id, quarter_start=datetime.date(2026, 9, 28),
                             quarter_end=datetime.date(2026, 12, 20))
    teacher = db.get(Student, 2)
    assert report.eligible_classes(db, teacher, now=datetime.datetime(2026, 10, 4, 12)) == [klass]
    assert report.eligible_classes(db, teacher, now=datetime.datetime(2026, 12, 21, 12)) == []
    assert report.eligible_classes(db, teacher, now=datetime.datetime(2026, 9, 27, 12)) == []
    # The boundary days count; an evening in the class's zone is still the same day.
    assert report.eligible_classes(db, teacher, now=datetime.datetime(2026, 12, 21, 5)) == [klass]
    # A class with no dates has no limit.
    klass.quarter_start = klass.quarter_end = None
    db.commit()
    assert report.eligible_classes(db, teacher, now=datetime.datetime(2027, 6, 1)) == [klass]


def test_an_instructor_who_opted_out_is_skipped(db, klass):
    from tutor.auth.access import set_weekly_email

    assert [s.email for s in report.eligible_instructors(db)] == ["teach@example.edu"]
    assert set_weekly_email(db, "teach@example.edu", False)
    assert report.eligible_instructors(db) == []
    assert set_weekly_email(db, "teach@example.edu", True)
    assert [s.email for s in report.eligible_instructors(db)] == ["teach@example.edu"]
    assert not set_weekly_email(db, "nobody@example.edu", False)


def test_a_class_with_no_activity_sends_nothing_unless_asked(db, klass):
    teacher = db.get(Student, 2)
    quiet_since, quiet_until = NOW + datetime.timedelta(days=30), NOW + datetime.timedelta(days=37)
    assert report.gather(db, teacher, quiet_since, quiet_until, now=quiet_until) == []
    weeks = report.gather(db, teacher, quiet_since, quiet_until, now=quiet_until, include_quiet=True)
    assert len(weeks) == 1 and not weeks[0].has_activity
    assert report.gather(db, teacher, SINCE, NOW, now=NOW)[0].has_activity


def test_the_daily_store_adds_a_day_table_and_a_chart_attachment(db, klass, tmp_path):
    from tutor.analytics import store as store_mod

    path = tmp_path / "store.sqlite"
    con = store_mod.open_store(path)
    try:
        store_mod.write_rows(con, "students", [{"id": 4, "pseudonym": "S4", "created_at": SINCE,
                                                "login_count": 4, "is_instructor": False}])
        store_mod.write_rows(con, "messages", [
            {"id": 1, "student_id": 4, "role": "user", "course": "AST101", "lecture_num": 4,
             "created_at": NOW - datetime.timedelta(days=2), "chars": 9},
            {"id": 2, "student_id": 4, "role": "user", "course": "AST101", "lecture_num": 4,
             "created_at": NOW - datetime.timedelta(days=1), "chars": 9}])
        store_mod.set_meta(con, "last_export", NOW.isoformat())
    finally:
        con.close()
    teacher = db.get(Student, 2)
    weeks = report.gather(db, teacher, SINCE, NOW, now=NOW)
    report.attach_daily(weeks, path, SINCE, NOW, tz="UTC")
    w = weeks[0]
    rows = w.daily_rows()
    # Seven days from a noon start touch eight calendar days.
    assert [r["day"] for r in rows][0] == SINCE.date().isoformat() and len(rows) == 8
    assert sum(r["questions"] for r in rows) == 2 and sum(r["students"] for r in rows) == 2
    name, page = w.attachment
    assert name.startswith("tutor-daily-ast-101-a") and name.endswith(".html")
    assert b"<!DOCTYPE html>" in page and b"keen@example.edu" not in page        # ids only
    md = report.render_markdown(teacher, weeks, LABEL, BASE)
    assert "**By day:**" in md and "| day | students | questions |" in md and "Attached: tutor-daily" in md
    html = report.render_html(teacher, weeks, LABEL, BASE)
    assert "<table" in html and "tutor-daily-ast-101-a" in html
    assert "/instructor" in html and "Weekly email" in html             # the footer says where to opt out


def test_an_instructor_without_classes_is_skipped(db, klass):
    assert report.eligible_classes(db, db.get(Student, 3)) == []
    assert "other@example.edu" not in [i.email for i in report.eligible_instructors(db)]


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def test_gather_builds_the_named_lists(db, klass):
    weeks = report.gather(db, db.get(Student, 2), SINCE, NOW, now=NOW)
    assert len(weeks) == 1
    w = weeks[0]
    assert w.summary.students == 3 and w.summary.active == 1
    assert [r.email for r in w.never] == ["newbie@example.edu"]
    assert [r.email for r in w.dormant] == ["quiet@example.edu"]
    assert [r.email for r in w.most_active] == ["keen@example.edu"]
    assert w.confirmed_goals == [("Telescope aperture", 1)]


def test_markdown_leads_with_usage_and_names_who_to_chase(db, klass):
    weeks = report.gather(db, db.get(Student, 2), SINCE, NOW, now=NOW)
    md = report.render_markdown(db.get(Student, 2), weeks, LABEL, BASE)
    assert "Hi Terry," in md
    assert "**Used the tutor:** 1 of 3 students (33%)" in md
    assert "1 never signed in, 1 went quiet" in md
    assert "**Never signed in (1):** Nat New <newbie@example.edu>" in md
    assert "**Went quiet (1):** Quinn Quiet <quiet@example.edu>" in md
    assert "**Most active:** Kim Keen (2 questions)" in md
    assert "**Confirmed this week:** Telescope aperture (1)" in md
    assert f"{BASE}/instructor?class_id={klass.id}" in md
    assert "not used to grade students" in md


def test_the_standings_table_needs_five_students(db, klass):
    """Review step 0.9: a percentile per measure within the class, rank
    only, in the email once the class has five students."""
    weeks = report.gather(db, db.get(Student, 2), NOW - datetime.timedelta(days=7), NOW, now=NOW)
    (w,) = weeks
    assert w.standings == [] and w.standings_table() == []
    md = report.render_markdown(db.get(Student, 2), weeks, "this week", "https://x.test")
    assert "Where each student stands" not in md
    assert "- **Not using it:** 1 never signed in, 1 went quiet this week" in md
    assert "**Never signed in (1):** Nat New <newbie@example.edu>" in md
    db.add_all([Student(id=7, email="g@example.edu", name="Gia", login_count=1),
                Student(id=8, email="h@example.edu", name="Hal", login_count=1)])
    db.commit()
    classes_mod.enroll(db, klass.id, "g@example.edu")
    classes_mod.enroll(db, klass.id, "h@example.edu")
    weeks = report.gather(db, db.get(Student, 2), NOW - datetime.timedelta(days=7), NOW, now=NOW)
    (w,) = weeks
    table = w.standings_table()
    assert table[0] == ["student", "questions", "days used", "on screen (min)", "deck (min)", "narration (min)", "visits"]
    rows = {r[0]: r for r in table[1:]}
    assert rows["Kim Keen"][1] == "2 (p100)" and rows["Nat New"][1] == "0 (p80)"
    md = report.render_markdown(db.get(Student, 2), weeks, "this week", "https://x.test")
    assert "**Where each student stands this week** (percentile within the class; a rank, not a judgement):" in md
    assert "| Kim Keen | 2 (p100) |" in md
    assert "2 signed in and never asked" in md
    html = report.render_html(db.get(Student, 2), weeks, "this week", "https://x.test")
    assert "Where each student stands this week" in html and "2 (p100)" in html


def test_html_escapes_student_names_and_links(db, klass):
    """A student's display name reaches the email body, so it must be escaped."""
    db.get(Student, 6).name = "Nat <script>alert(1)</script>"
    db.commit()
    teacher = db.get(Student, 2)
    weeks = report.gather(db, teacher, SINCE, NOW, now=NOW)
    html = report.render_html(teacher, weeks, LABEL, BASE)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert f"{BASE}/instructor?class_id={klass.id}" in html
    assert "1 of 3" in html


def test_subject_carries_the_headline(db, klass):
    weeks = report.gather(db, db.get(Student, 2), SINCE, NOW, now=NOW)
    assert report.subject(weeks, LABEL) == (
        f"Astra weekly: 1 of 3 students used the tutor ({LABEL})"
    )
    assert "no classes to report" in report.subject([], LABEL)


def test_a_quiet_week_reports_only_when_asked(db, klass):
    """A class nobody touched sends nothing by default (2026-10-05); with
    include_quiet the "nobody used it" email is still there for --quiet-too."""
    since = NOW - datetime.timedelta(hours=6)
    assert report.gather(db, db.get(Student, 2), since, NOW, now=NOW) == []
    weeks = report.gather(db, db.get(Student, 2), since, NOW, now=NOW, include_quiet=True)
    md = report.render_markdown(db.get(Student, 2), weeks, LABEL, BASE)
    assert weeks and weeks[0].summary.active == 0
    assert "**Used the tutor:** 0 of 3 students (0%)" in md
    assert "Never signed in" in md


def test_named_lists_are_capped(db, klass):
    for i in range(20):
        email = f"extra{i}@example.edu"
        db.add(Student(email=email, name=f"Extra {i}"))
        db.flush()
        classes_mod.enroll(db, klass.id, email)
    db.commit()
    weeks = report.gather(db, db.get(Student, 2), SINCE, NOW, now=NOW)
    md = report.render_markdown(db.get(Student, 2), weeks, LABEL, BASE)
    assert "Never signed in (21):" in md
    assert "and 6 more" in md            # 21 named-capped at 15


def test_report_covers_every_class_largest_first(db, klass):
    second = classes_mod.create_class(db, name="AST 102 B", course="AST102",
                                      instructor_email="teach@example.edu", created_by=ADMIN)
    classes_mod.enroll(db, second.id, "keen@example.edu")
    # The AST102 class saw no AST102 activity, so it only appears with include_quiet.
    weeks = report.gather(db, db.get(Student, 2), SINCE, NOW, now=NOW)
    assert [w.summary.name for w in weeks] == ["AST 101 A"]
    weeks = report.gather(db, db.get(Student, 2), SINCE, NOW, now=NOW, include_quiet=True)
    assert [w.summary.name for w in weeks] == ["AST 101 A", "AST 102 B"]


# ---------------------------------------------------------------------------
# Preview page + CLI
# ---------------------------------------------------------------------------

def _client(db, viewer):
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_student] = lambda: viewer
    return TestClient(app, raise_server_exceptions=True)


def test_preview_renders_the_same_email(db, klass):
    from tutor.main import app

    client = _client(db, db.get(Student, 2))
    try:
        r = client.get("/instructor/report")
        assert r.status_code == 200
        assert "This week&#39;s email" in r.text or "This week's email" in r.text
        assert "students used the tutor" in r.text
        assert "newbie@example.edu" in r.text
        # A link back to the class, and no send button anywhere.
        assert "Open this class" in r.text
        assert "send" not in r.text.lower().split("resend")[0].split("<form")[0]
    finally:
        app.dependency_overrides.clear()


def test_preview_needs_the_instructor_role(db, klass):
    from tutor.main import app

    client = _client(db, db.get(Student, 4))
    try:
        assert client.get("/instructor/report").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_cli_prints_without_sending(db, klass, monkeypatch, capsys):
    import scripts.instructor_report as cli

    monkeypatch.setattr(cli, "_window", lambda days: (SINCE, NOW))
    monkeypatch.setattr("tutor.db.models.get_session_factory", lambda engine=None: (lambda: db))
    monkeypatch.setattr("tutor.db.models.get_engine", lambda db_path=None: None)

    assert cli.main(["--days", "7", "--no-email"]) == 0
    out = capsys.readouterr().out
    assert "teach@example.edu" in out
    assert "students used the tutor" in out
    assert "Never signed in (1)" in out


def test_cli_sends_the_attachment(db, klass, monkeypatch, tmp_path):
    import scripts.instructor_report as cli
    from tutor import mail
    from tutor.analytics import store as store_mod

    path = tmp_path / "store.sqlite"
    con = store_mod.open_store(path)
    store_mod.set_meta(con, "last_export", NOW.isoformat())
    con.close()
    monkeypatch.setattr(cli, "_window", lambda days: (SINCE, NOW))
    monkeypatch.setattr("tutor.db.models.get_session_factory", lambda engine=None: (lambda: db))
    monkeypatch.setattr("tutor.db.models.get_engine", lambda db_path=None: None)
    sent: list = []

    def fake_send(to, subject, *, text, html, tag, attachments=None, bcc=None):
        sent.append((to, subject, attachments, bcc))
        return True

    monkeypatch.setattr(mail, "send_report", fake_send)
    monkeypatch.delenv("REPORT_EMAIL", raising=False)
    assert cli.main(["--daily-store", str(path), "--tz", "UTC"]) == 0
    assert len(sent) == 1 and sent[0][0] == "teach@example.edu"
    (name, data), = sent[0][2]
    assert name.endswith(".html") and data.startswith(b"<!DOCTYPE html>")
    assert sent[0][3] == ADMIN                                  # the admin's blind copy, by default
    assert cli.main(["--no-admin-copy"]) == 0
    assert sent[-1][3] is None


def test_cli_filters_to_one_instructor(db, klass, monkeypatch, capsys):
    import scripts.instructor_report as cli

    monkeypatch.setattr(cli, "_window", lambda days: (SINCE, NOW))
    monkeypatch.setattr("tutor.db.models.get_session_factory", lambda engine=None: (lambda: db))
    monkeypatch.setattr("tutor.db.models.get_engine", lambda db_path=None: None)

    assert cli.main(["--instructor", "other@example.edu", "--no-email"]) == 0
    assert "has no reportable classes" in capsys.readouterr().out


def test_a_co_instructor_gets_the_class_in_their_email(db, klass):
    other = db.get(Student, 3)
    assert report.eligible_classes(db, other) == []
    classes_mod.add_instructor(db, klass.id, "other@example.edu")
    assert [c.id for c in report.eligible_classes(db, other)] == [klass.id]
    assert [i.email for i in report.eligible_instructors(db)] == ["other@example.edu", "teach@example.edu"]


# ---------------------------------------------------------------------------
# Thumbs up and down (2026-10-06)
# ---------------------------------------------------------------------------

def _rate(db, student_id, rating, comment=None, when=None, course="AST101", lecture=4):
    """A tutor reply to ``student_id`` rated ``rating`` at ``when``."""
    reply = Message(student_id=student_id, role="assistant", course=course, lecture_num=lecture,
                    content="A light-year measures distance <b>not</b> time.", created_at=when)
    db.add(reply)
    db.flush()
    db.add(MessageFeedback(message_id=reply.id, student_id=student_id, rating=rating,
                           comment=comment, created_at=when, updated_at=when))
    db.commit()
    return reply


def test_the_weeks_ratings_and_flagged_replies_reach_the_email(db, klass):
    two_days = NOW - datetime.timedelta(days=2)
    bad = _rate(db, 4, -1, "It said a light-year is a time <b>wrong</b>", two_days)
    _rate(db, 4, +1, None, two_days)
    _rate(db, 5, -1, "old", NOW - datetime.timedelta(days=20))            # before the window
    _rate(db, 4, -1, "AST102", two_days, course="AST102")                       # another course
    teacher = db.get(Student, 2)
    weeks = report.gather(db, teacher, SINCE, NOW, now=NOW)
    fb = weeks[0].feedback
    assert (fb.up, fb.down, fb.rated) == (1, 1, 2)
    assert [f.message_id for f in fb.flagged] == [bad.id]

    md = report.render_markdown(teacher, weeks, LABEL, BASE)
    assert "**Feedback on the tutor:** 👍 1 · 👎 1 (50% of rated replies were thumbs-down)" in md
    assert "**Replies students flagged as wrong (1):**" in md
    assert "Kim Keen <keen@example.edu>, Sep 26, Lecture 4: “It said a light-year is a time <b>wrong</b>”" in md
    assert "the reply: A light-year measures distance" in md
    assert f"{BASE}/instructor/transcript/view?class_id={klass.id}&student=keen%40example.edu&msg={bad.id}#msg-{bad.id}" in md
    assert "Replies students flagged as wrong:** 1" not in md             # the old count line is gone

    html = report.render_html(teacher, weeks, LABEL, BASE)
    assert "Feedback on the tutor:</b> 👍 1 · 👎 1" in html
    assert "light-year is a time &lt;b&gt;wrong&lt;/b&gt;" in html and "<b>wrong</b>" not in html
    assert f"transcript/view?class_id={klass.id}&amp;student=keen%40example.edu&amp;msg={bad.id}#msg-{bad.id}'>open</a>" in html


def test_a_week_without_ratings_says_so(db, klass):
    teacher = db.get(Student, 2)
    weeks = report.gather(db, teacher, SINCE, NOW, now=NOW)
    md = report.render_markdown(teacher, weeks, LABEL, BASE)
    assert "**Feedback on the tutor:** no ratings this week" in md
    assert "flagged as wrong" not in md
    assert "no ratings this week" in report.render_html(teacher, weeks, LABEL, BASE)


def test_the_flagged_list_is_capped_newest_first(db, klass):
    for i in range(report.FLAGGED_LIMIT + 2):
        _rate(db, 4, -1, f"reason {i}", NOW - datetime.timedelta(days=1, minutes=i))
    teacher = db.get(Student, 2)
    weeks = report.gather(db, teacher, SINCE, NOW, now=NOW)
    assert weeks[0].feedback.down == report.FLAGGED_LIMIT + 2
    md = report.render_markdown(teacher, weeks, LABEL, BASE)
    assert "“reason 0”" in md and f"“reason {report.FLAGGED_LIMIT - 1}”" in md
    assert f"“reason {report.FLAGGED_LIMIT}”" not in md
    assert f"and 2 more on the dashboard: {BASE}/instructor?class_id={klass.id}#flagged" in md
    assert "on the dashboard</a>" in report.render_html(teacher, weeks, LABEL, BASE)

