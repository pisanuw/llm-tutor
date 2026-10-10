"""Stage 1 of the bulletproofing plan: the 500s and operator
alerts a student could cause, each pinned where the input sweep
(tests/test_input_sweep.py) cannot reach it.
"""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError
from starlette.applications import Starlette
from starlette.routing import Mount

import tutor.config as config
from tutor import alerts
from tutor.db.models import MagicLink, Message, MessageFeedback, RequestError, Student, WhitelistEntry
from tutor.utctime import utcnow
from tests.helpers import login_cookie, make_session


@pytest.fixture
def signed_in(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session(
        Student(id=1, email="ada@example.edu", name="Ada"),
        WhitelistEntry(email="@example.edu", added_by="test"),
        Message(id=7, student_id=1, role="assistant", content="A reply to rate.", course="AST101"),
    )
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: sent.append(key) or "queued")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session_token", login_cookie(db, 1))
    try:
        yield client, db, sent, app
    finally:
        app.dependency_overrides.clear()
        db.close()


# ---------------------------------------------------------------------------
# R12: the suite refuses what Postgres refuses; R8: NUL never reaches it
# ---------------------------------------------------------------------------

def test_the_suite_refuses_what_postgres_refuses_and_nul_is_stripped():
    db = make_session(Student(id=1, email="ada@example.edu", name="Ada"))
    db.add(MagicLink(email="a" * 300 + "@example.edu", token="t", code="123456", expires_at=utcnow()))
    with pytest.raises(DataError, match="character varying"):
        db.commit()
    db.rollback()
    with pytest.raises(DataError, match="integer out of range"):
        db.get(Student, 3_000_000_000)
    db.rollback()
    # A NUL in student text is removed on its way to the database (R8), on a
    # write and in a WHERE, so neither raises.
    db.add(Message(student_id=1, role="user", content="my code a\x00b", course="AST101"))
    db.commit()
    assert db.query(Message).filter(Message.content == "my code a\x00b").one().content == "my code ab"


# ---------------------------------------------------------------------------
# R3, R4, R5: the boundary refuses or ignores what would overflow
# ---------------------------------------------------------------------------

def test_a_long_address_is_the_valid_email_message_not_a_500(signed_in):
    client, db, sent, _ = signed_in
    r = client.post("/auth/magic-link", data={"email": "a" * 300 + "@example.edu"}, headers={"accept": "text/html"})
    assert r.status_code == 400 and "Enter a valid email address" in r.text
    assert db.query(MagicLink).count() == 0 and not [k for k in sent if k.startswith("exc:")]


def test_an_id_past_int4_is_not_found(signed_in):
    client, db, sent, _ = signed_in
    assert client.get("/feedback/3000000000", headers={"accept": "text/html"}).status_code == 404
    r = client.post("/chat/feedback", data={"message_id": "3000000000", "text": "A reply to rate.", "rating": "1"})
    assert r.status_code == 200 and r.json()["message_id"] == 7      # fell back to the text match
    assert not [k for k in sent if k.startswith("exc:")]


def test_a_goal_of_the_wrong_shape_never_reaches_the_disk():
    from tutor.agent import teachme

    assert teachme.valid_goal_id("AST101-L01-daily-motion")
    for bad in ("../AST102/AST102-L01-x", "a" * 300, "AST101-L01-x/../../y", "", "AST101-L1-x"):
        assert not teachme.valid_goal_id(bad)
        assert teachme.get_opener(bad, "AST101") is None


# ---------------------------------------------------------------------------
# R6: a deck path too long for the filesystem is the designed 404
# ---------------------------------------------------------------------------

def test_a_deck_name_too_long_for_the_filesystem_is_a_404(tmp_path):
    from tutor.main import DeckStaticFiles

    (tmp_path / "AST102" / "lecture-01").mkdir(parents=True)
    (tmp_path / "AST102" / "lecture-01" / "index.html").write_text("<html><body>deck</body></html>")
    app = Starlette(routes=[Mount("/slides", DeckStaticFiles(directory=str(tmp_path)))])
    client = TestClient(app, raise_server_exceptions=True)
    assert client.get("/slides/AST102/lecture-01/index.html").status_code == 200
    assert client.get("/slides/AST102/" + "a" * 300 + "/index.html").status_code == 404


# ---------------------------------------------------------------------------
# R9: a double click on a thumb saves one rating
# ---------------------------------------------------------------------------

def test_a_thumb_that_loses_the_insert_race_updates_the_row(signed_in, monkeypatch):
    from tutor.agent import state

    client, db, sent, _ = signed_in
    real = state.record_message_feedback
    calls = {"n": 0}

    def other_click_wins_first(session, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # The other click's insert lands between this one's read and write.
            session.add(MessageFeedback(message_id=kw["message_id"], student_id=1, rating=1))
            session.flush()
            session.add(MessageFeedback(message_id=kw["message_id"], student_id=1, rating=kw["rating"]))
            session.flush()                              # IntegrityError: message_id is unique
        return real(session, **kw)

    monkeypatch.setattr(state, "record_message_feedback", other_click_wins_first)
    r = client.post("/chat/feedback", data={"message_id": "7", "text": "A reply to rate.", "rating": "-1"})
    assert r.status_code == 200, r.text
    assert calls["n"] == 2 and r.json()["rating"] == -1
    assert db.query(MessageFeedback).count() == 1 and not sent


# ---------------------------------------------------------------------------
# R10: the voice cache never costs the audio
# ---------------------------------------------------------------------------

@pytest.fixture
def voice(tmp_path, monkeypatch):
    from tutor.agent import tts

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    monkeypatch.setenv("TTS_CACHE_DIR", str(tmp_path / "cache"))
    config.get_config.cache_clear()
    posted = {"n": 0}

    def fake_post(*a, **k):
        posted["n"] += 1
        return SimpleNamespace(content=b"ID3clip", raise_for_status=lambda: None)

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    try:
        yield tts, posted
    finally:
        config.get_config.cache_clear()


def test_two_students_synthesizing_one_opener_both_get_the_audio(voice):
    tts, _posted = voice
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def speak():
        barrier.wait()
        try:
            assert tts.synthesize("The same opener for everyone.") == b"ID3clip"
        except BaseException as exc:        # noqa: BLE001 - collected for the assert
            errors.append(exc)

    for _round in range(10):
        path = tts.cache_path("The same opener for everyone.")
        if path is not None and path.exists():
            path.unlink()
        threads = [threading.Thread(target=speak) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []


def test_a_cache_that_cannot_be_written_still_returns_the_audio(voice, monkeypatch):
    tts, posted = voice
    from pathlib import Path

    def no_space(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", no_space)
    assert tts.synthesize("A reply with no room to cache it.") == b"ID3clip"
    assert posted["n"] == 1


# ---------------------------------------------------------------------------
# R11: leaving mid-upload is not a student error
# ---------------------------------------------------------------------------

def test_a_disconnect_while_a_form_is_parsed_writes_no_row(signed_in):
    client, db, sent, app = signed_in
    token = client.cookies["session_token"]
    body = b"goal_id=G1&probe_id=p1&answer=" + b"x" * 1000
    messages: list[dict] = []

    async def scenario():
        chunks = [{"type": "http.request", "body": body[:100], "more_body": True},
                  {"type": "http.disconnect"}]

        async def receive():
            return chunks.pop(0) if chunks else {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/review/grade", "raw_path": b"/review/grade", "root_path": "",
            "query_string": b"", "server": ("testserver", 80), "client": ("10.0.0.9", 50000), "app": app,
            "headers": [(b"host", b"testserver"),
                        (b"content-type", b"application/x-www-form-urlencoded"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cookie", f"session_token={token}".encode())],
        }
        await app(scope, receive, send)

    asyncio.run(scenario())
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 400
    assert db.query(RequestError).count() == 0 and not sent
    assert json.dumps([m.get("status") for m in messages])     # the response went out


# ---------------------------------------------------------------------------
# The review of Stage 1
# ---------------------------------------------------------------------------

def test_a_message_of_only_nul_is_no_message_and_history_skips_empty_rows(signed_in, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import tutor.agent.turn as turn_mod
    from tutor.agent import memory
    from tutor.routers import chat as chat_mod

    client, db, sent, _ = signed_in
    ran = []

    async def fake_run_turn(**kw):
        ran.append(kw["user_message"])
        yield {"type": "done", "full_text": "ok"}

    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)
    monkeypatch.setattr(chat_mod, "_turn_session", lambda: sessionmaker(bind=db.get_bind())())
    assert client.post("/chat/message", data={"message": " \x00 \x00", "course": "AST101"}).status_code == 204
    assert client.post("/chat/message", data={"message": "a\x00b", "course": "AST101"}).status_code == 200
    assert ran == ["ab"]
    db.add(Message(student_id=1, role="user", content="", course="AST101"))
    db.add(Message(student_id=1, role="user", content="real question", course="AST101"))
    db.commit()
    history = memory.load_history(db, 1, "AST101")
    assert all(m["content"].strip() for m in history) and history[-1]["content"] == "real question"


def test_the_guard_allows_what_postgres_allows():
    """A long value compared in a WHERE, and a LIMIT or OFFSET past int4, run
    on Postgres; the guard used to refuse them."""
    from sqlalchemy import select, update

    db = make_session(Student(id=1, email="ada@example.edu", name="Ada"))
    db.execute(update(MagicLink).where(MagicLink.token == "x" * 400).values(used=True))
    db.execute(select(Message.id).limit(2**31).offset(2**31))
    db.rollback()


def test_a_misconception_probe_id_past_int4_is_unknown(signed_in):
    client, db, sent, _ = signed_in
    for probe in ("mc-3000000000", "mc-" + "9" * 300, "mc-abc"):
        r = client.post("/review/grade", data={"goal_id": "G1", "probe_id": probe, "answer": "x"})
        assert r.status_code == 404, (probe, r.status_code)
    assert not [k for k in sent if k.startswith("exc:")]


def test_admin_ids_past_int4_are_not_found(signed_in, monkeypatch):
    from tutor.routers.admin import issue_admin_session

    client, db, sent, _ = signed_in
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()
    try:
        client.cookies.set("admin_session", issue_admin_session())
        r = client.get("/admin?tab=conversations&student=ada@example.edu&msg=3000000000")
        assert r.status_code == 200
        r = client.post("/admin/feedback/3000000000/seen", data={"seen": "1"}, follow_redirects=False)
        assert r.status_code == 404
        assert not [k for k in sent if k.startswith("exc:")]
    finally:
        config.get_config.cache_clear()


def test_a_nul_in_any_static_path_is_not_found(signed_in):
    client, db, sent, _ = signed_in
    for path in ("/slides/AST101/lecture-01%00x/index.html", "/slides/AST101/a%00b", "/static/a%00b.js",
                 "/explorables/a%00b", "/book/a%00b.html", "/lecture-audio/AST101/a%00b.mp3"):
        r = client.get(path, headers={"accept": "text/html"})
        assert r.status_code in (404, 401), (path, r.status_code)
    assert not [k for k in sent if k.startswith("exc:")]


def test_the_prewarm_stops_when_the_cache_cannot_be_written(voice, monkeypatch, capsys):
    """synthesize now returns the audio when the cache write fails (R10); the
    prewarm, which renders only to fill the cache, must still stop at once
    (the review of Stage 1)."""
    tts, posted = voice
    from pathlib import Path

    def no_space(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", no_space)
    with pytest.raises(OSError):
        tts.synthesize("An opener to prewarm.", require_cache=True)
    assert posted["n"] == 1

