"""SSE resilience tests (ROADMAP capability item 6): the agent runs in a
background task writing to a per-turn buffer, so a client disconnect cannot
lose the reply, and the client can resume the stream from any offset."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tutor import streambuf
from tutor.db.models import Base, Student


@pytest.fixture(autouse=True)
def _fresh_buffers():
    streambuf._reset_for_tests()
    yield
    streambuf._reset_for_tests()


# ---------------------------------------------------------------------------
# streambuf unit behavior
# ---------------------------------------------------------------------------

def test_stream_delivers_in_order_and_to_late_readers():
    async def scenario():
        buf = streambuf.create("t1", student_id=1)
        for i in range(3):
            await buf.append({"type": "token", "text": str(i)})
        await buf.finish()
        # A reader arriving after completion still gets everything.
        got = [ev async for ev in streambuf.stream("t1")]
        assert [e["text"] for e in got] == ["0", "1", "2"]
        # Offsets skip already-seen events.
        tail = [ev async for ev in streambuf.stream("t1", start=2)]
        assert [e["text"] for e in tail] == ["2"]

    asyncio.run(scenario())


def test_stream_waits_for_live_producer():
    async def scenario():
        buf = streambuf.create("t2", student_id=1)

        async def produce():
            for i in range(3):
                await asyncio.sleep(0.01)
                await buf.append({"i": i})
            await buf.finish()

        task = asyncio.create_task(produce())
        got = [ev async for ev in streambuf.stream("t2")]
        await task
        assert [e["i"] for e in got] == [0, 1, 2]

    asyncio.run(scenario())


def test_reader_cancellation_does_not_cancel_producer():
    """Cancelling a stream() consumer (what a client disconnect does to the
    response generator) must leave the producer running to completion."""
    async def scenario():
        buf = streambuf.create("tc", student_id=1)

        async def produce():
            for i in range(5):
                await buf.append({"i": i})
                await asyncio.sleep(0.01)
            await buf.finish()

        ptask = asyncio.create_task(produce())
        got: list[dict] = []

        async def consume():
            async for ev in streambuf.stream("tc"):
                got.append(ev)

        ctask = asyncio.create_task(consume())
        while len(got) < 2:
            await asyncio.sleep(0.005)
        ctask.cancel()
        try:
            await ctask
        except asyncio.CancelledError:
            pass
        await ptask
        assert buf.done and len(buf.events) == 5

    asyncio.run(scenario())


def test_stream_unknown_turn_is_empty():
    async def scenario():
        return [ev async for ev in streambuf.stream("nope")]

    assert asyncio.run(scenario()) == []


# ---------------------------------------------------------------------------
# Route behavior
# ---------------------------------------------------------------------------

def _sse_events(text: str) -> list[dict]:
    out = []
    for part in text.split("\n\n"):
        part = part.strip()
        if part.startswith("data: "):
            out.append(json.loads(part[6:]))
    return out


@pytest.fixture()
def app_client(monkeypatch):
    """TestClient with DB + auth overridden and a scripted agent."""
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db
    from tutor.routers import chat as chat_router

    from sqlalchemy.pool import StaticPool

    # StaticPool: every Session shares ONE in-memory connection, so the
    # background task's own session (chat._turn_session) sees the same schema
    # instead of a fresh empty :memory: database.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    s = Student(email="sse@example.com", name="SSE")
    session.add(s)
    session.commit()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s

    # The background task owns its own session; point it at the test engine.
    monkeypatch.setattr(chat_router, "_turn_session", lambda: Session())

    state = {"completed": False, "turn_id": None}

    async def fake_run_turn(**kwargs):
        state["turn_id"] = kwargs.get("turn_id")   # the stream's handle, passed in
        for i in range(5):
            yield {"type": "token", "text": f"tok{i} "}
        yield {"type": "done", "full_text": "tok0 tok1 tok2 tok3 tok4 "}
        state["completed"] = True

    import tutor.agent.turn as turn_mod
    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)

    client = TestClient(app, raise_server_exceptions=True)
    yield client, s, state
    app.dependency_overrides.clear()
    session.close()


def test_full_stream_includes_turn_handle(app_client):
    client, _s, state = app_client
    r = client.post("/chat/message", data={"message": "hi", "course": "AST101"})
    assert r.status_code == 200
    events = _sse_events(r.text)
    assert events[0]["type"] == "turn" and events[0]["id"]
    kinds = [e["type"] for e in events[1:]]
    assert kinds == ["token"] * 5 + ["done"]
    assert state["completed"] is True
    # The turn runs under the id the client was told, so its ledger row and
    # the browser's events share it (activity analytics).
    assert state["turn_id"] == events[0]["id"]


def test_resume_returns_the_remainder_without_duplicates(app_client):
    """A true mid-stream disconnect cannot be simulated here: TestClient's
    transport buffers the whole app response before returning it. The
    disconnect-survival property (cancelling the response reader leaves the
    producer running) is proven directly in
    test_reader_cancellation_does_not_cancel_producer; this test covers the
    other half of resilience, that a resume at offset N replays exactly the
    events the client has not seen."""
    client, _s, state = app_client
    r = client.post("/chat/message", data={"message": "hi", "course": "AST101"})
    events = _sse_events(r.text)
    turn_id = events[0]["id"]
    assert state["completed"] is True

    # Client saw 2 agent events before "losing" the connection.
    r2 = client.get(f"/chat/message/{turn_id}", params={"offset": 2})
    assert r2.status_code == 200
    tail = _sse_events(r2.text)
    assert [e["type"] for e in tail] == ["token", "token", "token", "done"]
    assert tail[0]["text"] == "tok2 "

    # An offset past the end yields nothing extra (idempotent reconnects).
    r3 = client.get(f"/chat/message/{turn_id}", params={"offset": 6})
    assert _sse_events(r3.text) == []


def test_resume_is_scoped_to_the_owning_student(app_client):
    client, _s, _state = app_client
    r = client.post("/chat/message", data={"message": "hi", "course": "AST101"})
    turn_id = _sse_events(r.text)[0]["id"]

    from tutor.main import app
    from tutor.auth.deps import get_current_student

    other = Student(email="other@example.com", name="Other")
    other.id = 999
    app.dependency_overrides[get_current_student] = lambda: other
    r2 = client.get(f"/chat/message/{turn_id}")
    assert r2.status_code == 404


def test_resume_unknown_turn_404(app_client):
    client, _s, _state = app_client
    assert client.get("/chat/message/deadbeef").status_code == 404
