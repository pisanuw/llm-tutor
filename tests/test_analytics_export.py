"""The export to the local store (activity analytics, phase 2): a SQLite
copy of what the analysis needs, pseudonymous by default, incremental, the
same rows over the admin HTTP route.
"""
from __future__ import annotations

import datetime
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_session
from tutor.analytics import store
from tutor.analytics.__main__ import main
from tutor.db.models import (
    ActivityEvent, ClassEnrollment, CourseClass, GradedEvent, LearningGoal, LoginAttempt, Message,
    MessageFeedback, PathEvent, RequestError, Session as LoginSession, Student, StudentDay, StudentProgress,
    Track, TurnEvent, WhitelistEntry,
)

T0 = datetime.datetime(2026, 10, 1, 17, 0, 0)


def _seed(db):
    """Two students in one class, a third outside it, with a little of
    everything the store copies."""
    db.add_all([
        Student(id=1, email="ann@example.edu", name="Ann", created_at=T0),
        Student(id=2, email="bo@example.edu", name="Bo", created_at=T0),
        Student(id=3, email="cy@example.edu", name="Cy", created_at=T0),
        Student(id=9, email="prof@example.edu", name="Prof", created_at=T0, is_instructor=True),
        WhitelistEntry(email="ann@example.edu", added_by="t"),
        LearningGoal(id="AST101-L03-sea", title="Seasons", description="", course="AST101", lecture_num=3),
        Track(id="AST101-lecture-order", course="AST101", title="Lecture order", goal_ids_json='["AST101-L03-sea"]'),
    ])
    db.flush()
    klass = CourseClass(id=5, name="AST 101 A", course="AST101", term="Au26", instructor_id=9)
    db.add(klass)
    db.flush()
    db.add_all([ClassEnrollment(class_id=5, student_id=1), ClassEnrollment(class_id=5, student_id=2)])
    for sid in (1, 2, 3):
        db.add(LoginSession(student_id=sid, jwt_jti=f"j{sid}", expires_at=T0 + datetime.timedelta(days=7),
                            method="magic", created_at=T0))
        q = Message(student_id=sid, role="user", course="AST101", lecture_num=3, content="what causes the seasons?",
                    created_at=T0 + datetime.timedelta(minutes=sid))
        a = Message(student_id=sid, role="assistant", course="AST101", lecture_num=3,
                    content="The tilt of Earth's axis.", created_at=T0 + datetime.timedelta(minutes=sid, seconds=9))
        db.add_all([q, a])
        db.flush()
        db.add(MessageFeedback(message_id=a.id, student_id=sid, rating=1, comment="secret note"))
        db.add(TurnEvent(student_id=sid, kind="chat", intent="QUESTION", rung_used=2, cost_usd=0.01,
                         latency_ms=4000, turn_id=f"t{sid}", ts=T0 + datetime.timedelta(minutes=sid),
                         goal_ids_json='["AST101-L03-sea"]',
                         envelope_json=json.dumps({"move": "worked_example", "max_rung": 2}),
                         verdicts_json=json.dumps({"verdict": "allow", "judge_reason": "fine",
                                                   "timings": {"classify_ms": 900}})))
        db.add(GradedEvent(student_id=sid, goal_id="AST101-L03-sea", success=1.0, source="exercise", ts=T0))
        db.add(StudentProgress(student_id=sid, goal_id="AST101-L03-sea", status="seen", seen=True))
        db.add(PathEvent(student_id=sid, course="AST101", goal_id="AST101-L03-sea", modality="teach", ts=T0))
        for seq, name in enumerate(("page_view", "question_sent", "reply_done"), start=1):
            db.add(ActivityEvent(student_id=sid, visit_id=f"v{sid}", page_view_id=f"p{sid}", seq=seq, name=name,
                                 ts=T0 + datetime.timedelta(minutes=sid, seconds=seq), course="AST101",
                                 lecture_num=3, label="Send" if name == "page_view" else None,
                                 turn_id=f"t{sid}" if name == "reply_done" else None,
                                 detail_json='{"kind":"typed"}' if name == "question_sent" else None))
    # Review step 0.9: the sign-in trail, an error page and the daily rollup.
    db.add(LoginAttempt(email="ann@example.edu", student_id=1, method="magic", outcome="code_invalid", host="1.2.3.4",
                        user_agent="Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15",
                        detail="wrong code", created_at=T0))
    db.add(LoginAttempt(email="stranger@gmail.com", student_id=None, method="magic", outcome="denied",
                        user_agent="Mozilla/5.0 Proofpoint URLDefense", created_at=T0))
    db.add(RequestError(ts=T0, status=404, method="GET", path="/slides/AST101/lecture-99/index.html",
                        referrer_path="/chat", student_id=1, kind="deck", agent="browser", ref="abcd1234"))
    db.add(StudentDay(student_id=1, day=datetime.date(2026, 10, 1), course="AST101", questions=1, turns=1,
                      cost_usd=0.01, active_min=3.5, deck_min=0, narration_min=0, visits=1, slides_seen=0,
                      lectures_opened=0, error_outcomes=0, busy_outcomes=0, detector_cuts=0, blocks=0,
                      slow_turns=0, thumbs_up=1, thumbs_down=0, logins=1, failed_logins=1, user_agents=1,
                      request_errors=1, hours_mask=1 << 10, computed_at=T0))
    db.commit()


@pytest.fixture
def db():
    s = make_session()
    _seed(s)
    yield s
    s.close()


@pytest.fixture
def decks(tmp_path, monkeypatch):
    """A rendered-looking output tree with one deck and its timing file."""
    from tutor import config

    out = tmp_path / "output"
    lec = out / "AST101" / "lecture-03"
    lec.mkdir(parents=True)
    (lec / "index.html").write_text("<html></html>")
    (lec / "content.md").write_text("## Slide 1: Seasons\n\n## Slide 2: Axial tilt\n\n## Slide 3: The Sun's path\n")
    (lec / "timing.json").write_text(json.dumps({"1": 30.5, "2": 41.0, "3": 12.5}))
    cfg = dict(config.get_config())
    cfg["output_dir"], cfg["lecture_audio_dir"] = out, tmp_path / "audio"
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    return out


def _export(db, path, **kw):
    options = store.ExportOptions(**kw)
    con = store.open_store(path)
    try:
        counts = store.export(con, store.db_reader(db, options), options)
    finally:
        con.close()
    return counts


def _q(path, sql):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def test_the_store_is_pseudonymous_and_holds_the_tables(db, tmp_path, decks):
    path = tmp_path / "store.sqlite"
    counts = _export(db, path)
    assert counts["students"] == 4 and counts["activity_events"] == 9 and counts["lectures"] == 1
    students = _q(path, "SELECT * FROM students ORDER BY id")
    assert students[0]["pseudonym"] == "S1" and students[0]["email"] is None and students[0]["name"] is None
    messages = _q(path, "SELECT * FROM messages ORDER BY id")
    assert messages[0]["chars"] == len("what causes the seasons?") and messages[0]["content"] is None
    assert messages[0]["created_at"] == "2026-10-01 17:01:00"           # ISO text SQLite can sort and diff
    feedback = _q(path, "SELECT * FROM message_feedback")
    assert feedback[0]["rating"] == 1 and feedback[0]["comment"] is None   # the note leaves only with --with-content
    turn = _q(path, "SELECT * FROM turn_events WHERE student_id = 1")[0]
    assert (turn["turn_id"], turn["verdict"], turn["move"]) == ("t1", "allow", "worked_example")
    assert json.loads(turn["timings_json"]) == {"classify_ms": 900}
    assert "verdicts_json" not in turn                                   # the judge's reason stays behind
    lecture = _q(path, "SELECT * FROM lectures")[0]
    assert (lecture["course"], lecture["lecture_num"], lecture["title"], lecture["slides_total"]) == ("AST101", 3, "Seasons", 3)
    assert lecture["narration_total_s"] == 84.0 and json.loads(lecture["narration_s_json"])["2"] == 41.0
    events = _q(path, "SELECT * FROM activity_events WHERE student_id = 2 ORDER BY seq")
    assert [e["name"] for e in events] == ["page_view", "question_sent", "reply_done"]
    assert events[2]["turn_id"] == "t2" and events[1]["detail_json"] == '{"kind":"typed"}'
    meta = {r["key"]: json.loads(r["value"]) for r in _q(path, "SELECT * FROM export_meta")}
    assert meta["options"]["with_emails"] is False and "last_export" in meta


def test_the_flags_add_emails_and_content(db, tmp_path, decks):
    path = tmp_path / "store.sqlite"
    _export(db, path, with_emails=True, with_content=True)
    assert _q(path, "SELECT email, name FROM students WHERE id = 1")[0] == {"email": "ann@example.edu", "name": "Ann"}
    assert _q(path, "SELECT content FROM messages WHERE id = 1")[0]["content"] == "what causes the seasons?"


def test_a_second_export_adds_only_new_rows_and_refreshes_the_tail(db, tmp_path, decks):
    path = tmp_path / "store.sqlite"
    _export(db, path)
    # A new event, and a turn finished after the first export.
    db.add(ActivityEvent(student_id=1, visit_id="v1", page_view_id="p1", seq=4, name="page_leave", ts=T0))
    te = db.query(TurnEvent).filter_by(turn_id="t1").one()
    te.verdicts_json = json.dumps({"verdict": "revise"})
    db.commit()
    counts = _export(db, path)
    assert counts["activity_events"] == 10          # the tail is re-read, nothing duplicated
    assert _q(path, "SELECT COUNT(*) AS n FROM activity_events")[0]["n"] == 10
    assert _q(path, "SELECT verdict FROM turn_events WHERE turn_id = 't1'")[0]["verdict"] == "revise"


def test_a_class_export_holds_only_its_roster(db, tmp_path, decks):
    path = tmp_path / "store.sqlite"
    _export(db, path, class_id=5)
    assert [r["id"] for r in _q(path, "SELECT id FROM students ORDER BY id")] == [1, 2]
    assert {r["student_id"] for r in _q(path, "SELECT student_id FROM activity_events")} == {1, 2}
    assert [r["id"] for r in _q(path, "SELECT id FROM classes")] == [5]
    assert _q(path, "SELECT COUNT(*) AS n FROM learning_goals")[0]["n"] == 1   # the curriculum always comes


def test_since_keeps_only_rows_from_that_day_on(db, tmp_path, decks):
    db.add(Message(student_id=1, role="user", course="AST101", content="old", created_at=T0 - datetime.timedelta(days=40)))
    db.commit()
    path = tmp_path / "store.sqlite"
    _export(db, path, since=T0 - datetime.timedelta(days=1))
    assert _q(path, "SELECT COUNT(*) AS n FROM messages")[0]["n"] == 6


def test_the_rows_survive_the_json_wire(db):
    rows = store.read_rows(db, "turn_events")
    wired = [store.from_json(json.loads(json.dumps(store.jsonable(r)))) for r in rows]
    assert wired == rows


# ---------------------------------------------------------------------------
# Over HTTP: the admin route and the CLI's --from-url
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(db, monkeypatch):
    import tutor.config as config
    from tutor.auth.deps import get_db
    from tutor.main import app

    monkeypatch.setenv("ADMIN_TOKEN", "tkn-export")
    config.get_config.cache_clear()
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_the_admin_route_pages_a_table_as_json_lines(admin, decks):
    headers = {"X-Admin-Token": "tkn-export"}
    first = admin.get("/admin/export/analytics/activity_events", params={"limit": 4}, headers=headers)
    assert first.status_code == 200 and first.headers["content-type"].startswith("application/x-ndjson")
    rows = [json.loads(line) for line in first.text.splitlines()]
    assert [r["id"] for r in rows] == [1, 2, 3, 4] and rows[0]["ts"] == "2026-10-01 17:01:01"
    rest = admin.get("/admin/export/analytics/activity_events", params={"since_id": 4}, headers=headers)
    assert len(rest.text.splitlines()) == 5
    students = admin.get("/admin/export/analytics/students", headers=headers)
    assert "ann@example.edu" not in students.text
    named = admin.get("/admin/export/analytics/students", params={"with_emails": "true"}, headers=headers)
    assert "ann@example.edu" in named.text
    assert admin.get("/admin/export/analytics/students").status_code == 403         # no token
    assert admin.get("/admin/export/analytics/secrets", headers=headers).status_code == 404
    assert admin.get("/admin/export/analytics/lectures", headers=headers).json()["slides_total"] == 3


def test_the_trail_the_errors_and_the_rollup_export_without_text(db, tmp_path, decks):
    """Review step 0.9 (F45): login_attempts without the address, host or
    browser string (the browser as a class), request_errors as recorded,
    student_days with the day as text."""
    out = tmp_path / "s.sqlite"
    _export(db, out)
    trail = _q(out, "SELECT * FROM login_attempts ORDER BY id")
    assert [dict(r) for r in trail] == [
        {"id": trail[0]["id"], "created_at": "2026-10-01 17:00:00", "student_id": 1, "method": "magic",
         "outcome": "code_invalid", "agent": "browser", "detail": "wrong code"},
        {"id": trail[1]["id"], "created_at": "2026-10-01 17:00:00", "student_id": None, "method": "magic",
         "outcome": "denied", "agent": "scanner", "detail": None},
    ]
    (err,) = _q(out, "SELECT * FROM request_errors")
    assert (err["status"], err["path"], err["student_id"], err["kind"], err["agent"]) == (404, "/slides/AST101/lecture-99/index.html", 1, "deck", "browser")
    (day,) = _q(out, "SELECT * FROM student_days")
    assert (day["day"], day["student_id"], day["questions"], day["active_min"], day["hours_mask"]) == ("2026-10-01", 1, 1, 3.5, 1024)
    # A class scope keeps the roster's rows only: the stranger's attempt goes.
    scoped = tmp_path / "c.sqlite"
    _export(db, scoped, class_id=5)
    assert [r["outcome"] for r in _q(scoped, "SELECT outcome FROM login_attempts")] == ["code_invalid"]


def test_the_http_reader_builds_the_same_store(admin, db, tmp_path, decks):
    direct, remote = tmp_path / "direct.sqlite", tmp_path / "remote.sqlite"
    _export(db, direct)
    options = store.ExportOptions()
    con = store.open_store(remote)
    try:
        store.export(con, store.http_reader("http://testserver", "tkn-export", options, client=admin), options)
    finally:
        con.close()
    for table in store.TABLE_NAMES:
        a = _q(direct, f"SELECT * FROM {table} ORDER BY 1, 2")
        b = _q(remote, f"SELECT * FROM {table} ORDER BY 1, 2")
        assert a == b, table


def test_the_cli_exports_the_configured_database(tmp_path, decks, capsys, monkeypatch):
    from tutor.db.models import get_engine, get_session_factory, init_db

    engine = get_engine()           # the suite's temporary SQLite (tests/conftest.py)
    init_db(engine)
    session = get_session_factory(engine)()
    try:
        if session.query(Student).count() == 0:
            _seed(session)
    finally:
        session.close()
    out = tmp_path / "cli.sqlite"
    assert main(["export", "--out", str(out), "--quiet"]) == 0
    err = capsys.readouterr().err
    assert "Target database -> sqlite" in err and "Wrote" in err
    assert _q(out, "SELECT COUNT(*) AS n FROM students")[0]["n"] >= 4
    for name in ("ADMIN_TOKEN", "TUTOR_ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert main(["export", "--out", str(out), "--from-url", "http://x", "--quiet"]) == 2   # no token
