"""Requests that wait on something slow give their pooled connection back
first (R1 and R2).

FastAPI closes a yield dependency's session only after the whole response is
sent, so a streaming reply, a resumed stream, a voice synthesis, a photo
transcription and a review grade each held one of production's seven pooled
connections for as long as they ran: a handful of students asking at once
filled the pool, and every other request waited ten seconds and failed.
Measured here with a file SQLite engine, a real pool and the session
dependency's own shape.
"""
from __future__ import annotations

import asyncio
import datetime
import threading
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from tutor.db.models import Base, LearningGoal, Message, Student, StudentProgress, WhitelistEntry
from tutor.utctime import utcnow
from tests.helpers import login_cookie

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def pooled(tmp_path, monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers import chat as chat_mod

    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}", connect_args={"check_same_thread": False},
                           poolclass=QueuePool, pool_size=4, max_overflow=4)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as seed:
        seed.add_all([
            Student(id=1, email="ada@example.edu", name="Ada"),
            WhitelistEntry(email="ada@example.edu", added_by="test"),
            Message(student_id=1, role="assistant", content="A reply to speak.", course="AST101"),
            LearningGoal(id="G1", title="Daily motion", description="", course="AST101", lecture_num=1),
        ])
        seed.flush()
        seed.add(StudentProgress(student_id=1, goal_id="G1", status="unlocked", mastery_score=0.7,
                                 fsrs_stability=2.0, fsrs_difficulty=5.0, fsrs_reps=1,
                                 fsrs_due=utcnow() - datetime.timedelta(hours=2),
                                 last_evidence_at=utcnow() - datetime.timedelta(days=3)))
        seed.commit()
        token = login_cookie(seed, 1)

    def real_get_db():                          # the shape of tutor.auth.deps.get_db
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = real_get_db
    monkeypatch.setattr(chat_mod, "_turn_session", factory)
    try:
        yield app, engine, token
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _client(app, token):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    client.cookies.set("session_token", token)
    return client


def _held_while(app, token, engine, request, entered, release):
    """Start ``request(client)``, wait until the slow part is entered, read
    the pool, let it finish; return (checked out during, the response)."""
    async def scenario():
        async with _client(app, token) as client:
            task = asyncio.create_task(request(client))
            await entered()
            during = engine.pool.checkedout()
            release()
            return during, await asyncio.wait_for(task, 10)
    return asyncio.run(scenario())


def _gate():
    entered, release = threading.Event(), threading.Event()

    async def wait_entered():
        assert await asyncio.to_thread(entered.wait, 10), "the slow part never started"
        await asyncio.sleep(0.05)
    return entered, release, wait_entered


def test_a_streaming_reply_holds_no_connection_while_it_runs(pooled, monkeypatch):
    import tutor.agent.turn as turn_mod

    app, engine, token = pooled
    entered, release, wait_entered = _gate()

    async def fake_run_turn(**_):
        yield {"type": "token", "text": "Partial "}
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        yield {"type": "done", "full_text": "Partial answer."}

    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)
    during, resp = _held_while(app, token, engine,
                               lambda c: c.post("/chat/message", data={"message": "hi", "course": "AST101"}),
                               wait_entered, release.set)
    assert resp.status_code == 200 and '"done"' in resp.text
    assert during == 0
    assert engine.pool.checkedout() == 0


def test_a_resumed_stream_holds_no_connection(pooled, monkeypatch):
    import tutor.agent.turn as turn_mod

    app, engine, token = pooled
    entered, release, _ = _gate()
    seen: dict = {}

    async def fake_run_turn(*, turn_id=None, **_):
        seen["turn_id"] = turn_id
        yield {"type": "token", "text": "Partial "}
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        yield {"type": "done", "full_text": "Partial answer."}

    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)

    async def scenario():
        async with _client(app, token) as client:
            first = asyncio.create_task(client.post("/chat/message", data={"message": "hi", "course": "AST101"}))
            assert await asyncio.to_thread(entered.wait, 10)
            resumed = asyncio.create_task(client.get(f"/chat/message/{seen['turn_id']}?offset=0"))
            await asyncio.sleep(0.2)                             # the resume is streaming
            during = engine.pool.checkedout()
            release.set()
            return during, await asyncio.wait_for(resumed, 10), await asyncio.wait_for(first, 10)

    during, resumed, first = asyncio.run(scenario())
    assert resumed.status_code == 200 and '"done"' in resumed.text and first.status_code == 200
    assert during == 0


def test_voice_photo_and_review_wait_without_a_connection(pooled, monkeypatch):
    from tutor.agent import mastery, vision

    app, engine, token = pooled

    entered, release, wait_entered = _gate()

    def slow_synthesis(text):
        entered.set()
        release.wait(10)
        return b"ID3audio"

    with patch("tutor.agent.tts.synthesize", slow_synthesis):
        during, resp = _held_while(app, token, engine,
                                   lambda c: c.post("/chat/tts", data={"text": "A reply to speak."}),
                                   wait_entered, release.set)
    assert resp.status_code == 200 and resp.content == b"ID3audio" and during == 0

    entered, release, wait_entered = _gate()

    def slow_vision(data, media_type):
        entered.set()
        release.wait(10)
        return {"ok": True, "code": "int main() {}"}

    monkeypatch.setattr(vision, "transcribe_image", slow_vision)
    during, resp = _held_while(app, token, engine,
                               lambda c: c.post("/chat/transcribe-photo", files={"image": ("p.png", PNG, "image/png")}),
                               wait_entered, release.set)
    assert resp.status_code == 200 and resp.json()["ok"] is True and during == 0

    entered, release, wait_entered = _gate()

    def slow_grader(goal, question, rubric, answer, *rest):
        assert goal.title == "Daily motion"                  # still readable once detached
        entered.set()
        release.wait(10)
        return {"score": 3, "feedback": "Right.", "usage": {"in_tokens": 10, "out_tokens": 5}}

    probe = {"id": "p1", "question": "Why do the stars rise and set?", "rubric": "the rotation of the Earth"}
    with patch("tutor.agent.mastery._question_pool", return_value=[probe]), \
         patch.object(mastery, "grade_probe", slow_grader):
        during, resp = _held_while(app, token, engine,
                                   lambda c: c.post("/review/grade", data={"goal_id": "G1", "probe_id": "p1",
                                                                           "answer": "the Earth turns"}),
                                   wait_entered, release.set)
    assert resp.status_code == 200, resp.text
    assert resp.json()["score"] == 3 and during == 0
    assert engine.pool.checkedout() == 0


def test_a_full_pool_is_one_alert_not_one_per_path(monkeypatch):
    """R2: a pool timeout went out under exc:TimeoutError:<path>, one email
    per path that waited."""
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from tutor import alerts, main

    keys: list[str] = []
    monkeypatch.setattr(alerts, "alert_exception", lambda key, path, exc, note="": keys.append(key))
    for path in ("/chat/quota", "/api/events", "/chat"):
        main._alert_unhandled(path, PoolTimeout("QueuePool limit reached"))
    assert keys == ["db-pool", "db-pool", "db-pool"]


def test_a_paused_upload_holds_no_connection(pooled):
    """The login check used to run before the body was read, so a slow or
    stalled upload held the connection it opened for as long as the body
    took (the review of Stage 1). The body is read first now."""
    app, engine, token = pooled
    body = b"text=" + b"A reply to speak." + b"&pad=" + b"x" * 5000
    paused = {}

    async def scenario():
        resume = asyncio.Event()
        chunks = [body[:20], body[20:]]
        sent = {"n": 0}

        async def receive():
            if sent["n"] == 0:
                sent["n"] = 1
                return {"type": "http.request", "body": chunks[0], "more_body": True}
            if sent["n"] == 1:
                await resume.wait()
                sent["n"] = 2
                return {"type": "http.request", "body": chunks[1], "more_body": False}
            await asyncio.sleep(3600)

        statuses = []

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/chat/tts", "raw_path": b"/chat/tts", "root_path": "",
                 "query_string": b"", "server": ("testserver", 80), "client": ("10.0.0.9", 50000), "app": app,
                 "headers": [(b"host", b"testserver"), (b"content-type", b"application/x-www-form-urlencoded"),
                             (b"content-length", str(len(body)).encode()),
                             (b"cookie", f"session_token={token}".encode())]}
        tasks = [asyncio.create_task(app(dict(scope), receive, send)) for _ in range(1)]
        await asyncio.sleep(0.3)                             # the first chunk is in, the rest is not
        paused["during"] = engine.pool.checkedout()
        resume.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        return statuses

    with patch("tutor.agent.tts.synthesize", lambda text: b"ID3"):
        statuses = asyncio.run(scenario())
    assert paused["during"] == 0
    assert statuses == [200]

