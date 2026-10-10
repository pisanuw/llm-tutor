"""Plan step 2.6 (findings C1, C12, C11, A19): database access off the loop.

C1: get_db built a new Engine (and so a new connection) per request, and
async handlers ran blocking database calls on the event loop, so one slow
query stalled every other request, SSE token streams included. C12: the 500
handler made its alert and restore HTTP calls on the loop. C11: /healthz was
unthrottled. A19: the classifier and the judge wrote ledger rows through the
turn's session from executor threads.
Done when: a 2 s DB stall in one request does not delay another request's
SSE stream (tested with a slow SQLite function).
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import Base, LearningGoal, Message, Student, TurnEvent
from tests.helpers import applied, make_session

STALL = 2.0


def _slow(seconds):
    time.sleep(seconds)
    return 1


def _stalling_engine(url, **kw):
    """An engine whose connections know slow(seconds), a SQLite function
    that blocks the calling thread, so a trigger can stall a real write."""
    engine = create_engine(url, connect_args={"check_same_thread": False}, **kw)

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function("slow", 1, _slow)

    Base.metadata.create_all(engine)
    return engine


def _max_gap(stamps):
    return max((b - a for a, b in zip(stamps, stamps[1:])), default=0.0)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def test_the_app_engine_is_shared_and_pooled(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.db import models

    models.dispose_app_engines()
    try:
        assert models.app_engine() is models.app_engine()
        gen = get_db()
        session = next(gen)
        assert session.get_bind() is models.app_engine()
        gen.close()

        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com:5432/postgres?pgbouncer=true")
        pg = models.app_engine()
        assert pg is models.app_engine() and pg.url.get_backend_name() == "postgresql"
        assert "pgbouncer" not in pg.url.query
        assert pg.pool.size() == 3 and pg.pool._max_overflow == 4
        assert pg.pool._pre_ping is True and pg.pool._recycle == 300
        monkeypatch.setenv("DB_POOL_SIZE", "2")
        assert models.get_engine().pool.size() == 2       # scripts get a fresh engine
    finally:
        models.dispose_app_engines()
    assert models._APP_ENGINES == {}


# ---------------------------------------------------------------------------
# Done when: a stalled request does not delay another request's SSE stream
# ---------------------------------------------------------------------------

@pytest.fixture
def stalling_app(tmp_path, monkeypatch):
    from tutor.auth.deps import get_current_student, get_db
    from tutor.main import app
    from tutor.routers import chat as chat_router

    engine = _stalling_engine(f"sqlite:///{tmp_path / 'stall.db'}")
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add(Student(id=1, email="stu@example.edu", name="Stu"))
        s.add(Message(student_id=1, role="assistant", content="A reply to rate.", course="AST101"))
        s.commit()
        # Saving feedback stalls for STALL seconds inside SQLite.
        s.execute(text(f"CREATE TRIGGER stall AFTER INSERT ON message_feedback "
                       f"BEGIN SELECT slow({STALL}); END"))
        s.commit()

    def _db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_student] = lambda: SimpleNamespace(id=1, email="stu@example.edu")
    monkeypatch.setattr(chat_router, "_turn_session", lambda: Session())
    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_a_db_stall_in_one_request_does_not_delay_another_sse_stream(stalling_app, monkeypatch):
    import tutor.agent.turn as turn_mod

    stamps: list[float] = []

    async def ticking_turn(**_):
        for i in range(30):
            stamps.append(time.monotonic())
            yield {"type": "token", "text": f"t{i} "}
            await asyncio.sleep(0.05)
        yield {"type": "done", "full_text": "done"}

    monkeypatch.setattr(turn_mod, "run_turn", ticking_turn)

    async def scenario():
        transport = httpx.ASGITransport(app=stalling_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            stream = asyncio.create_task(
                client.post("/chat/message", data={"message": "hi", "course": "AST101"}))
            await asyncio.sleep(0.2)                         # tokens are flowing
            t0 = time.monotonic()
            feedback = await client.post("/chat/feedback",
                                         data={"text": "A reply to rate.", "rating": "1"})
            stalled = time.monotonic() - t0
            return feedback, stalled, await stream

    feedback, stalled, stream = asyncio.run(scenario())
    assert feedback.status_code == 200 and stream.status_code == 200
    assert stalled >= STALL * 0.9                            # the stall really happened
    assert len(stamps) == 30
    assert _max_gap(stamps) < 0.5                            # and the stream never paused for it


def _turn_patches(stream_round):
    from tutor.agent.strategist import NO_MOVE

    envelope = Envelope(max_rung=5, goals=["G1"], exam_lockdown=[], must_ground=True,
                        allow_exercise=True, escalate_review=False, reason="band")
    bundle = EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)
    return [
        patch("tutor.agent.preclassify.classify", return_value=Classification(intent=Intent.QUESTION)),
        patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE),
        patch("tutor.agent.mastery.get_active_exam", return_value=None),
        patch("tutor.agent.envelope.assemble_envelope", return_value=bundle),
        patch("tutor.agent.turn_stages.actor_client.stream_round", stream_round),
        patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"),
    ]


def _one_round(text_="Find the Sun first."):
    async def fake(system, messages, tools):
        yield ("chunk", text_)
        final = MagicMock(stop_reason="end_turn", content=[], usage=None, model=None)
        yield ("final", final)
    return fake


def test_a_stalled_write_inside_a_turn_does_not_block_the_loop():
    engine = _stalling_engine("sqlite://", poolclass=StaticPool)
    db = sessionmaker(bind=engine)()
    db.add(Student(id=1, email="stu@example.edu", name="Stu"))
    db.add(LearningGoal(id="G1", title="Seasons", description="", course="AST101", lecture_num=3))
    db.commit()
    # The provisional chat event's insert stalls for STALL seconds.
    db.execute(text(f"CREATE TRIGGER stall AFTER INSERT ON turn_events "
                    f"WHEN NEW.kind = 'chat' BEGIN SELECT slow({STALL}); END"))
    db.commit()
    agent._INFLIGHT.clear()
    ticks: list[float] = []

    async def ticker(done: asyncio.Event):
        while not done.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)
        ticks.append(time.monotonic())   # a blocked loop shows as the last gap

    async def scenario():
        done = asyncio.Event()
        tick = asyncio.create_task(ticker(done))
        t0 = time.monotonic()
        events = [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message="why do we have seasons?",
            lecture_num=None, course="AST101")]
        took = time.monotonic() - t0
        done.set()
        await tick
        return events, took

    ps = _turn_patches(_one_round())
    with applied(ps):
        events, took = asyncio.run(scenario())
    assert events[-1]["type"] == "done"
    assert took >= STALL * 0.9
    assert _max_gap(ticks) < 0.5
    db.close()


# ---------------------------------------------------------------------------
# Handlers: no blocking database work left on the event loop
# ---------------------------------------------------------------------------

# async handlers allowed to take a session: they stream the turn, which runs
# its own database work through the executor.
_STREAMING = {("api.py", "chat"), ("chat.py", "chat_message")}


def test_async_route_handlers_keep_database_work_off_the_loop():
    offenders = []
    for path in sorted(pathlib.Path("tutor").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            routed = any("router." in ast.unparse(d) or "app.get" in ast.unparse(d)
                         or "app.post" in ast.unparse(d) for d in node.decorator_list)
            takes_db = any(a.arg == "db" for a in node.args.args + node.args.kwonlyargs)
            if not (routed and takes_db) or (path.name, node.name) in _STREAMING:
                continue
            if "run_in_threadpool" not in ast.unparse(node):
                offenders.append(f"{path}:{node.name}")
    assert offenders == []


# ---------------------------------------------------------------------------
# The 500 handler
# ---------------------------------------------------------------------------

def test_the_500_handler_alerts_off_the_loop(monkeypatch):
    from sqlalchemy.exc import OperationalError

    import tutor.main as main_mod
    from tutor import alerts, selfheal

    app = main_mod.app
    seen: dict = {}

    def slow_restore(reason=""):
        seen["thread"] = threading.current_thread().name
        time.sleep(1.0)
        return "initiated"

    monkeypatch.setattr(selfheal, "restore_supabase_if_down", slow_restore)
    monkeypatch.setattr(alerts, "alert_exception",
                        lambda key, path, exc, note="": seen.setdefault("alert", key))

    async def boom():
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    async def ping():
        return {"ok": True}

    app.add_api_route("/__test_boom", boom, methods=["GET"])
    app.add_api_route("/__test_ping", ping, methods=["GET"])

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            t0 = time.monotonic()
            failed = await client.get("/__test_boom")
            failed_in = time.monotonic() - t0
            t1 = time.monotonic()
            pinged = await client.get("/__test_ping")
            ping_in = time.monotonic() - t1
            await main_mod._last_alert_job
            return failed, failed_in, pinged, ping_in

    try:
        failed, failed_in, pinged, ping_in = asyncio.run(scenario())
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", "") not in ("/__test_boom", "/__test_ping")]
    assert failed.status_code == 500 and pinged.status_code == 200
    assert failed_in < 0.5 and ping_in < 0.5      # neither waited on the 1 s restore
    assert seen["alert"] == "db-down"
    assert seen["thread"] != threading.main_thread().name


# ---------------------------------------------------------------------------
# /healthz limits
# ---------------------------------------------------------------------------

class _CountingSession:
    def __init__(self):
        self.calls = 0

    def execute(self, *a, **kw):
        self.calls += 1
        return SimpleNamespace(scalar=lambda: 1)

    def close(self):
        pass


@pytest.fixture
def healthz_client():
    from fastapi.testclient import TestClient

    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers import status as status_router

    status_router._reset_for_tests()
    session = _CountingSession()
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app), session, status_router
    finally:
        app.dependency_overrides.clear()
        status_router._reset_for_tests()


def test_healthz_answers_a_busy_address_from_the_last_result(healthz_client, monkeypatch):
    client, session, status_router = healthz_client
    monkeypatch.setattr(status_router, "PER_IP_PER_MINUTE", 2)
    assert [client.get("/healthz").status_code for _ in range(2)] == [200, 200]
    third = client.get("/healthz")
    assert third.status_code == 200 and third.json()["cached"] is True
    assert session.calls == 2                                # no third connection


def test_healthz_throttles_an_address_with_no_recent_result(healthz_client, monkeypatch):
    client, session, status_router = healthz_client
    monkeypatch.setattr(status_router, "PER_IP_PER_MINUTE", 0)
    r = client.get("/healthz")
    assert r.status_code == 429 and r.json() == {"status": "throttled"}
    assert session.calls == 0


def test_healthz_never_runs_more_than_the_slot_count_at_once(healthz_client):
    client, session, status_router = healthz_client
    assert client.get("/healthz").status_code == 200         # a result to fall back on
    for _ in range(status_router.PROBE_SLOTS):
        assert status_router._slots.acquire(blocking=False)  # every slot busy
    try:
        r = client.get("/healthz")
    finally:
        for _ in range(status_router.PROBE_SLOTS):
            status_router._slots.release()
    assert r.status_code == 200 and r.json()["cached"] is True
    assert session.calls == 1


# ---------------------------------------------------------------------------
# A19: the classifier and the judge never touch the session
# ---------------------------------------------------------------------------

class _Usage:
    input_tokens = 300
    output_tokens = 20
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _client_returning(body):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text=body)]
    resp.usage = _Usage()
    client.messages.create.return_value = resp
    return client


def test_classify_and_judge_report_usage_instead_of_writing():
    from tutor.agent import judge as judge_mod
    from tutor.agent import preclassify

    db = MagicMock()
    sink: list = []
    preclassify.classify("what is a light-year?", "AST101", db=db, student_id=1, goal_catalog=[],
                         client=_client_returning('{"intent": "QUESTION"}'), usage_sink=sink)
    envelope = Envelope(max_rung=5, goals=[], exam_lockdown=[], must_ground=False,
                        allow_exercise=True, escalate_review=False, reason="band")
    judge_mod.judge(envelope, "a draft", [], db=db, student_id=1, usage_sink=sink,
                    client=_client_returning('{"verdict": "allow", "rule": "", "reason": ""}'))
    assert [c["kind"] for c in sink] == ["classify", "judge"]
    assert db.method_calls == [] and db.add.call_count == 0


def test_the_turn_writes_the_classifier_ledger_row(tmp_path):
    db = make_session()
    db.add(Student(id=1, email="stu@example.edu", name="Stu"))
    db.commit()
    agent._INFLIGHT.clear()
    got: dict = {}

    def classify(message, course, **kw):
        got.update(kw)
        kw["usage_sink"].append({"usage": _Usage(), "model": "claude-haiku-4-5-20251001",
                                 "kind": "classify", "latency_ms": 5})
        return Classification(intent=Intent.QUESTION)

    ps = _turn_patches(_one_round())
    ps[0] = patch("tutor.agent.preclassify.classify", side_effect=classify)
    with applied(ps):
        async def go():
            return [e async for e in agent.run_turn(
                student=db.get(Student, 1), db=db, user_message="why do we have seasons?",
                lecture_num=None, course="AST101")]
        asyncio.run(go())
    assert "db" not in got and "student_id" not in got
    row = db.query(TurnEvent).filter_by(kind="classify").one()
    assert row.in_tokens == 300 and row.cost_usd > 0
    db.close()


# ---------------------------------------------------------------------------
# A turn that breaks its session still releases the student's claim
# ---------------------------------------------------------------------------

def test_a_turn_that_breaks_its_session_releases_the_claim():
    db = make_session()
    db.add(Student(id=1, email="stu@example.edu", name="Stu"))
    db.commit()
    agent._INFLIGHT.clear()
    student = db.get(Student, 1)

    async def broken_body(student, db, *args):
        db.expire(student)
        db.add(TurnEvent(student_id=None, kind="broken"))    # NOT NULL: the flush fails
        db.flush()
        yield {"type": "token", "text": "never"}

    async def go():
        return [e async for e in agent.run_turn(
            student=student, db=db, user_message="hi", lecture_num=None, course="AST101")]

    with patch.object(agent, "_run_turn_body", broken_body), pytest.raises(IntegrityError):
        asyncio.run(go())
    assert agent._INFLIGHT == {}
    db.rollback()
    db.close()


# ---------------------------------------------------------------------------
# A turn holds no pooled connection while it waits on a model
# ---------------------------------------------------------------------------

def test_a_turn_holds_no_connection_during_its_model_calls(tmp_path):
    """Each database step commits on its own (turn_stages.dbstep.db_step), so the small
    pool is not tied up for the length of a reply. The strategist's ledger reads
    overlap the classifier call by design (preflight.classify, latency plan B2),
    so that call is checked once the reads have handed the connection back,
    not at its first instant; the other two calls must see none at all."""
    from tutor.agent.strategist import NO_MOVE

    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    out = {"n": 0}
    event.listen(engine, "checkout", lambda *a: out.__setitem__("n", out["n"] + 1))
    event.listen(engine, "checkin", lambda *a: out.__setitem__("n", out["n"] - 1))
    db = sessionmaker(bind=engine)()
    db.add(Student(id=1, email="stu@example.edu", name="Stu"))
    db.add(LearningGoal(id="G1", title="Seasons", description="", course="AST101", lecture_num=3))
    db.commit()
    agent._INFLIGHT.clear()
    held: dict = {}

    def classify(message, course, **kw):
        # A real Haiku call outlasts the strategist reads by far; wait for the
        # release the way the call's own duration would, then look.
        deadline = time.monotonic() + 2
        while out["n"] and time.monotonic() < deadline:
            time.sleep(0.01)
        held["classify"] = out["n"]
        return Classification(intent=Intent.QUESTION)

    def choose_move(*a, **kw):
        held["strategist"] = out["n"]
        return NO_MOVE

    async def stream_round(system, messages, tools):
        held["actor"] = out["n"]
        yield ("chunk", "Find the Sun first.")
        final = MagicMock(stop_reason="end_turn", content=[], usage=None, model=None)
        yield ("final", final)

    ps = _turn_patches(stream_round)
    ps[0] = patch("tutor.agent.preclassify.classify", side_effect=classify)
    ps[1] = patch("tutor.agent.strategist.choose_move", side_effect=choose_move)
    with applied(ps):
        async def go():
            return [e async for e in agent.run_turn(
                student=db.get(Student, 1), db=db, user_message="why do we have seasons?",
                lecture_num=3, course="AST101")]
        events = asyncio.run(go())
    assert events[-1]["type"] == "done"
    assert held == {"classify": 0, "strategist": 0, "actor": 0}
    assert db.expire_on_commit is True                       # restored after the turn
    db.close()
    engine.dispose()
