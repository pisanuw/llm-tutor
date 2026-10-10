"""Predict-your-score tests on code evaluation (ROADMAP pedagogy item 11).
The prediction rides the chat form, joins server-side to the first eval
event of the turn, never enters the model pipeline (blind by construction),
and surfaces to the client as a synthetic prediction event. Distinct from
tests/test_calibration.py, which covers the Engram confidence system."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tutor import streambuf
from tutor.db.models import Base, ScorePrediction, Student


@pytest.fixture(autouse=True)
def _fresh_buffers():
    streambuf._reset_for_tests()
    yield
    streambuf._reset_for_tests()


def _sse_events(text: str) -> list[dict]:
    out = []
    for part in text.split("\n\n"):
        part = part.strip()
        if part.startswith("data: "):
            out.append(json.loads(part[6:]))
    return out


def _make_client(monkeypatch, events):
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db
    from tutor.routers import chat as chat_router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    s = Student(email="cal@example.com", name="Cal")
    session.add(s)
    session.commit()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s
    monkeypatch.setattr(chat_router, "_turn_session", lambda: Session())

    async def fake_run_turn(**kwargs):
        for ev in events:
            await asyncio.sleep(0)
            yield ev

    import tutor.agent.turn as turn_mod
    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)
    return TestClient(app, raise_server_exceptions=True), session


EVAL_TURN = [
    {"type": "token", "text": "Looking at your code... "},
    {"type": "eval", "score": 2, "correct": False},
    {"type": "token", "text": "here is feedback"},
    {"type": "done", "full_text": "Looking at your code... here is feedback"},
]


def _teardown():
    from tutor.main import app
    app.dependency_overrides.clear()


def test_prediction_recorded_and_reported(monkeypatch):
    client, session = _make_client(monkeypatch, EVAL_TURN)
    try:
        r = client.post("/chat/message",
                        data={"message": "grade this", "course": "AST101",
                              "predicted_score": "3"})
        events = _sse_events(r.text)
        pred = [e for e in events if e["type"] == "prediction"]
        assert pred == [{"type": "prediction", "predicted": 3, "actual": 2}]
        # The prediction note arrives right after the eval badge event.
        kinds = [e["type"] for e in events]
        assert kinds.index("prediction") == kinds.index("eval") + 1

        rows = session.query(ScorePrediction).all()
        assert len(rows) == 1
        row = rows[0]
        assert (row.predicted, row.actual, row.correct, row.course) == (3, 2, False, "AST101")
        assert row.context.startswith("grade this")
    finally:
        _teardown()


def test_no_prediction_means_no_row(monkeypatch):
    client, session = _make_client(monkeypatch, EVAL_TURN)
    try:
        r = client.post("/chat/message", data={"message": "grade this", "course": "AST101"})
        assert not [e for e in _sse_events(r.text) if e["type"] == "prediction"]
        assert session.query(ScorePrediction).count() == 0
    finally:
        _teardown()


@pytest.mark.parametrize("bad", ["9", "-1", "abc", "2.5", ""])
def test_invalid_predictions_ignored(monkeypatch, bad):
    client, session = _make_client(monkeypatch, EVAL_TURN)
    try:
        r = client.post("/chat/message",
                        data={"message": "grade this", "course": "AST101",
                              "predicted_score": bad})
        assert not [e for e in _sse_events(r.text) if e["type"] == "prediction"]
        assert session.query(ScorePrediction).count() == 0
    finally:
        _teardown()


def test_prediction_without_eval_records_nothing(monkeypatch):
    turn = [
        {"type": "token", "text": "just chatting"},
        {"type": "done", "full_text": "just chatting"},
    ]
    client, session = _make_client(monkeypatch, turn)
    try:
        r = client.post("/chat/message",
                        data={"message": "hello", "course": "AST101",
                              "predicted_score": "4"})
        assert not [e for e in _sse_events(r.text) if e["type"] == "prediction"]
        assert session.query(ScorePrediction).count() == 0
    finally:
        _teardown()


def test_only_first_eval_joins(monkeypatch):
    turn = [
        {"type": "eval", "score": 1, "correct": False},
        {"type": "eval", "score": 4, "correct": True},
        {"type": "done", "full_text": "x"},
    ]
    client, session = _make_client(monkeypatch, turn)
    try:
        r = client.post("/chat/message",
                        data={"message": "grade", "course": "AST102",
                              "predicted_score": "1"})
        pred = [e for e in _sse_events(r.text) if e["type"] == "prediction"]
        assert pred == [{"type": "prediction", "predicted": 1, "actual": 1}]
        assert session.query(ScorePrediction).count() == 1
    finally:
        _teardown()
