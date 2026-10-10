"""Capacity-fallback tests (ROADMAP stability item 3): after the SDK's own
retries, a 429/5xx on the actor call retries once on the supervisor-tier
model instead of erroring, but never after student-visible output and never
for non-capacity errors."""

from __future__ import annotations

import asyncio

import anthropic
import httpx
import pytest

from tutor.agent import turn
from tutor.agent.models import SUPERVISOR
from tutor.agent.turn_stages import actor_client


def _status_error(status: int) -> anthropic.APIStatusError:
    resp = httpx.Response(
        status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return anthropic.APIStatusError("boom", response=resp, body=None)


def test_is_capacity_error_classification():
    assert actor_client.is_capacity_error(_status_error(429)) is True
    assert actor_client.is_capacity_error(_status_error(529)) is True
    assert actor_client.is_capacity_error(_status_error(500)) is True
    assert actor_client.is_capacity_error(_status_error(401)) is False
    assert actor_client.is_capacity_error(_status_error(400)) is False
    assert actor_client.is_capacity_error(RuntimeError("x")) is False


class _CreateClient:
    """messages.create fake: raises the queued errors, then succeeds."""

    def __init__(self, errors):
        self._errors = list(errors)
        self.calls: list[dict] = []

        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer._errors:
                    raise outer._errors.pop(0)
                return {"ok": True, "model": kwargs["model"]}

        self.messages = _Messages()


def test_create_with_fallback_switches_model_on_429():
    client = _CreateClient([_status_error(429)])
    out = actor_client.create_with_fallback(client, model="actor-model", max_tokens=5)
    assert out["model"] == SUPERVISOR.id
    assert [c["model"] for c in client.calls] == ["actor-model", SUPERVISOR.id]


def test_create_with_fallback_propagates_auth_errors():
    client = _CreateClient([_status_error(401)])
    with pytest.raises(anthropic.APIStatusError):
        actor_client.create_with_fallback(client, model="actor-model", max_tokens=5)
    assert len(client.calls) == 1


def test_create_with_fallback_gives_up_after_one_switch():
    client = _CreateClient([_status_error(529), _status_error(529)])
    with pytest.raises(anthropic.APIStatusError):
        actor_client.create_with_fallback(client, model="actor-model", max_tokens=5)
    assert [c["model"] for c in client.calls] == ["actor-model", SUPERVISOR.id]


class _FakeStream:
    """Async context manager mimicking client.messages.stream(...)."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()

    async def get_final_message(self):
        class _Final:
            usage = None
        return _Final()


class _StreamClient:
    """AsyncAnthropic fake whose stream() raises queued errors, then streams."""

    def __init__(self, errors, chunks):
        self._errors = list(errors)
        self._chunks = chunks
        self.models: list[str] = []

        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.models.append(kwargs["model"])
                if outer._errors:
                    raise outer._errors.pop(0)
                return _FakeStream(outer._chunks)

        self.messages = _Messages()

    async def aclose(self):
        pass


def _run_stream_round(client):
    async def collect():
        events = []
        async for ev in actor_client.stream_round("sys", [], None):
            events.append(ev)
        return events

    return asyncio.run(collect())


def test_stream_round_falls_back_on_overload(monkeypatch):
    client = _StreamClient([_status_error(529)], ["hi"])
    monkeypatch.setattr(actor_client, "async_client", lambda: client)
    events = _run_stream_round(client)
    assert ("chunk", "hi") in events
    assert client.models == [actor_client.actor_model(), SUPERVISOR.id]


def test_stream_round_does_not_fall_back_on_auth(monkeypatch):
    client = _StreamClient([_status_error(401)], ["hi"])
    monkeypatch.setattr(actor_client, "async_client", lambda: client)
    with pytest.raises(anthropic.APIStatusError):
        _run_stream_round(client)
    assert client.models == [actor_client.actor_model()]


def test_friendly_error_overloaded_wording():
    msg = turn.friendly_error(_status_error(529))
    assert "overloaded" in msg.lower() or "busy" in msg.lower()
