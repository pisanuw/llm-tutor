"""Browser activity events: the vocabulary, the cleaner, and POST /api/events
(activity analytics, step 1.3).

A signed-in page posts batches of what the student did; each batch is
cleaned against the vocabulary, capped per student, written in one
statement with the login session stamped on, and mirrored to the activity
stream at the verbose tier. Anything else a signed-in student sends gets a
204 and is dropped: a browser must never retry a batch.
"""
from __future__ import annotations

import datetime
import json
import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.helpers import login_cookie, make_session
from tutor import activity_events as ae
from tutor.db.models import ActivityEvent, Session as SessionModel, Student, WhitelistEntry
from tutor.utctime import utcnow

NOW = datetime.datetime(2026, 10, 5, 18, 0, 0)
NOW_MS = int(NOW.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _event(name="click", **fields):
    return {"name": name, "visit_id": "v1", "page_view_id": "p1", "seq": 1,
            "client_ts": NOW_MS, **fields}


# ---------------------------------------------------------------------------
# The cleaner
# ---------------------------------------------------------------------------

def test_a_clean_event_keeps_the_columns_and_the_declared_detail():
    rows = ae.clean_batch({"sent_at": NOW_MS, "events": [
        _event("slide_view", course="AST101", lecture_num=4, slide_num=7, value_ms=4200,
               prev_slide=6, how="next", path="/slides/AST101/lecture-04/index.html",
               turn_id="feedfacecafebeef", message_id=12, stray="dropped"),
    ]}, NOW)
    assert len(rows) == 1
    row = rows[0]
    assert (row["name"], row["visit_id"], row["page_view_id"], row["seq"]) == ("slide_view", "v1", "p1", 1)
    assert (row["course"], row["lecture_num"], row["slide_num"], row["value_ms"]) == ("AST101", 4, 7, 4200)
    assert (row["turn_id"], row["message_id"]) == ("feedfacecafebeef", 12)
    assert json.loads(row["detail_json"]) == {"how": "next", "prev_slide": 6}
    assert row["ts"] == NOW and row["client_ts"] == NOW


def test_a_click_keeps_its_link_a_missing_deck_and_a_script_error_are_rows():
    """Review step 0.2: the path a link opens joins a later 404 (F6), the
    slides panel's missing deck is recorded (F5), and a script failure
    leaves its name, file and line, never a message (F52)."""
    rows = ae.clean_batch({"sent_at": NOW_MS, "events": [
        _event("click", target="a", label="Slides (L03)", href="/slides/AST101/lecture-03/index.html"),
        _event("deck_missing", course="AST101", lecture_num=3, seq=2),
        _event("client_error", error="TypeError", source="/static/chat.js", line=1234, message="never", seq=3),
    ]}, NOW)
    assert [r["name"] for r in rows] == ["click", "deck_missing", "client_error"]
    assert json.loads(rows[0]["detail_json"]) == {"href": "/slides/AST101/lecture-03/index.html"}
    assert (rows[1]["course"], rows[1]["lecture_num"]) == ("AST101", 3)
    assert json.loads(rows[2]["detail_json"]) == {"error": "TypeError", "source": "/static/chat.js", "line": 1234}


def test_a_non_finite_number_is_dropped_not_a_500():
    """json accepts 1e400 as infinity and int() of it raises OverflowError,
    which used to escape clean_batch as a 500 and an alert email
    (finding F57)."""
    rows = ae.clean_batch(json.loads(
        '{"sent_at": %d, "events": [{"name": "page_view", "visit_id": "v1", "page_view_id": "p1",'
        ' "seq": 1, "client_ts": %d, "vw": 1e400, "vh": NaN, "value_ms": 1200}]}' % (NOW_MS, NOW_MS)
    ), NOW)
    assert len(rows) == 1
    assert rows[0]["value_ms"] == 1200
    assert not rows[0]["detail_json"] or json.loads(rows[0]["detail_json"]) == {}


def test_the_server_time_is_the_browsers_shifted_by_its_clock_offset():
    # The browser's clock runs an hour fast; the event was 5 s before the send.
    fast = NOW_MS + 3_600_000
    rows = ae.clean_batch({"sent_at": fast, "events": [_event(client_ts=fast - 5000)]}, NOW)
    assert rows[0]["ts"] == NOW - datetime.timedelta(seconds=5)
    assert rows[0]["client_ts"] == NOW + datetime.timedelta(hours=1) - datetime.timedelta(seconds=5)


@pytest.mark.parametrize("client_ts", [NOW_MS - 2 * 86_400_000, NOW_MS + 120_000])
def test_an_implausible_corrected_time_falls_back_to_now(client_ts):
    rows = ae.clean_batch({"sent_at": NOW_MS, "events": [_event(client_ts=client_ts)]}, NOW)
    assert rows[0]["ts"] == NOW


def test_without_sent_at_the_server_time_is_now():
    rows = ae.clean_batch({"events": [_event(client_ts=NOW_MS - 90_000)]}, NOW)
    assert rows[0]["ts"] == NOW and rows[0]["client_ts"] == NOW - datetime.timedelta(seconds=90)


def test_unknown_names_bad_ids_and_unfit_values_are_dropped_and_the_rest_kept():
    rows = ae.clean_batch({"sent_at": NOW_MS, "events": [
        _event("mouse_move"),                                   # not in the vocabulary
        {"name": "click", "visit_id": "../x", "page_view_id": "p1"},   # a bad id
        "not an event",
        _event("click", label="x" * 500, target=" send-btn ", path="javascript:alert(1)"[:200],
               value_ms=-5, lecture_num=250, seq=-1),
        _event("reply_done", outcome="meh", chars=12.0, value_ms=1500),
        _event("deck_open", has_audio="yes", context="tab", slides_total=True),
    ]}, NOW)
    assert [r["name"] for r in rows] == ["click", "reply_done", "deck_open"]
    click, done, deck = rows
    assert len(click["label"]) == 80 and click["target"] == "send-btn"
    assert click["path"] == "javascript:alert(1)"      # a string column is clipped, not judged
    assert click["value_ms"] is None and click["lecture_num"] is None and click["seq"] == 0
    assert json.loads(done["detail_json"]) == {"chars": 12} and done["value_ms"] == 1500
    assert json.loads(deck["detail_json"]) == {"context": "tab"}   # a string is not a bool or an int


def test_an_oversized_or_malformed_batch_is_empty():
    assert ae.clean_batch({"events": [_event()] * (ae.MAX_BATCH + 1)}, NOW) == []
    assert ae.clean_batch({"events": []}, NOW) == []
    assert ae.clean_batch({"events": "nope"}, NOW) == []
    assert ae.clean_batch([_event()], NOW) == []
    assert len(ae.clean_batch({"events": [_event()] * ae.MAX_BATCH}, NOW)) == ae.MAX_BATCH


def test_every_event_declares_only_known_spec_kinds():
    for name, specs in ae.EVENTS.items():
        for key, spec in specs.items():
            assert spec[0] in ("int", "str", "enum", "bool"), (name, key)
            assert key not in ae._COLUMN_SPECS, (name, key)   # a column is never also a detail


def test_the_per_student_cap_refuses_a_flood_and_recovers_next_minute():
    ae._reset_for_tests()
    with patch.object(ae, "RATE_PER_MINUTE", 10):
        assert ae.allow(1, 6, now=100.0) and ae.allow(1, 4, now=101.0)
        assert not ae.allow(1, 1, now=102.0)        # over the minute's budget
        assert ae.allow(2, 10, now=102.0)           # another student's budget
        assert ae.allow(1, 10, now=161.0)           # a new minute


# ---------------------------------------------------------------------------
# POST /api/events
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    s = make_session(Student(id=1, email="stu@example.edu", name="Stu"),
                     WhitelistEntry(email="stu@example.edu", added_by="test"))
    yield s
    s.close()


@pytest.fixture
def client(db):
    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)
    c.cookies.set("session_token", login_cookie(db))
    return c


def _batch(*events, sent_at=None):
    sent = int(utcnow().replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) if sent_at is None else sent_at
    return {"sent_at": sent, "events": list(events)}


def test_a_batch_becomes_rows_stamped_with_the_student_and_their_login(client, db):
    now_ms = int(utcnow().replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    resp = client.post("/api/events", json=_batch(
        {"name": "page_view", "visit_id": "v1", "page_view_id": "p1", "seq": 1, "client_ts": now_ms - 8000,
         "path": "/chat", "course": "AST101", "vw": 390, "vh": 844},
        {"name": "click", "visit_id": "v1", "page_view_id": "p1", "seq": 2, "client_ts": now_ms - 3000,
         "path": "/chat", "target": "send-btn", "label": "Send", "student_id": 99},
        {"name": "reply_done", "visit_id": "v1", "page_view_id": "p1", "seq": 3, "client_ts": now_ms,
         "turn_id": "abc123", "message_id": 7, "value_ms": 4100, "chars": 240, "outcome": "done"},
    ))
    assert resp.status_code == 204
    rows = db.query(ActivityEvent).order_by(ActivityEvent.seq).all()
    assert [r.name for r in rows] == ["page_view", "click", "reply_done"]
    login = db.query(SessionModel).one()
    assert {r.student_id for r in rows} == {1}            # never the client's claim
    assert {r.login_session_id for r in rows} == {login.id}
    assert rows[0].ts < rows[1].ts < rows[2].ts           # the browser's spacing survives
    assert (utcnow() - rows[0].ts).total_seconds() == pytest.approx(8, abs=2)
    assert json.loads(rows[0].detail_json) == {"vh": 844, "vw": 390}
    assert (rows[2].turn_id, rows[2].message_id, rows[2].value_ms) == ("abc123", 7, 4100)


def test_it_needs_a_signed_in_student(db):
    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    assert TestClient(app).post("/api/events", json=_batch(_event())).status_code == 401
    assert db.query(ActivityEvent).count() == 0


def test_a_tab_that_closes_mid_batch_is_a_lost_batch_not_an_error(client, db, monkeypatch):
    """The browser closed the connection while the leave beacon was still
    arriving (Starlette raises ClientDisconnect from request.body()); that
    became an unhandled 500 with an alert email and a reference the
    operator was told the student saw (production, 2026-10-07)."""
    from starlette.requests import ClientDisconnect, Request

    from tutor.db.models import RequestError

    async def gone(self):
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "body", gone)
    resp = client.post("/api/events", json=_batch(_event()))
    assert resp.status_code == 204
    assert db.query(ActivityEvent).count() == 0 and db.query(RequestError).count() == 0


def test_an_oversized_body_and_bad_json_are_refused_without_rows(client, db):
    big = _batch(*[dict(_event(), label="x" * 80) for _ in range(ae.MAX_BATCH)])
    big["events"][0]["path"] = "/" + "y" * 200
    body = json.dumps(big) + " " * ae.MAX_BODY_BYTES
    assert client.post("/api/events", content=body,
                       headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/api/events", content="{not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/events", json=_batch(*[_event()] * (ae.MAX_BATCH + 1))).status_code == 204
    assert client.post("/api/events", json={"events": [{"name": "nothing"}]}).status_code == 204
    assert db.query(ActivityEvent).count() == 0


def test_a_flooding_page_is_dropped_whole(client, db):
    with patch.object(ae, "RATE_PER_MINUTE", 3):
        assert client.post("/api/events", json=_batch(*[_event(seq=i) for i in range(3)])).status_code == 204
        assert client.post("/api/events", json=_batch(_event(seq=9))).status_code == 204
    assert db.query(ActivityEvent).count() == 3


def test_a_write_failure_is_logged_and_the_page_never_sees_it(client, db, caplog):
    with patch("tutor.agent.state.record_activity_events", side_effect=RuntimeError("pool gone")), \
         caplog.at_level(logging.WARNING, logger="tutor.activity_events"):
        assert client.post("/api/events", json=_batch(_event())).status_code == 204
    assert db.query(ActivityEvent).count() == 0
    assert any("was not written" in r.getMessage() for r in caplog.records)


def test_rows_mirror_to_the_activity_stream_at_verbose_only(client, monkeypatch):
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append   # type: ignore[method-assign]
    act = logging.getLogger("tutor.activity")
    act.addHandler(handler)
    try:
        monkeypatch.setenv("ACTIVITY_LEVEL", "standard")
        client.post("/api/events", json=_batch(_event(target="speak", path="/chat")))
        monkeypatch.setenv("ACTIVITY_LEVEL", "verbose")
        client.post("/api/events", json=_batch(_event(target="speak", path="/chat", seq=2, label="Play")))
    finally:
        act.removeHandler(handler)
    lines = [json.loads(r.getMessage()) for r in records]
    ui = [e for e in lines if e.get("event") == "ui_event"]
    assert len(ui) == 1
    assert (ui[0]["target"], ui[0]["seq"], ui[0]["student_id"], ui[0]["label"]) == ("speak", 2, 1, "Play")
    assert "email" not in ui[0] and "stu@example.edu" not in records[0].getMessage()
