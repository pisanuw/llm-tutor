"""Cold-start hardening: the actor stream retries a connection failure that
happens before the first token, but never after (a post-token retry would
duplicate student-visible output).

The live smoke run (2026-06-12) hit transient connection timeouts on the
first, cold-cache turn of a session. _stream_round now retries once when the
connection drops before any token is produced. These tests fake the Anthropic
client at the _async_client seam so the retry loop is exercised deterministically
without a real API call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from tutor.agent.turn_stages import actor_client


def _conn_error() -> anthropic.APIConnectionError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(message="connection reset", request=req)


def _timeout_error() -> anthropic.APITimeoutError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=req)


class _WorkingStream:
    """An async-context-manager stream that yields chunks then a final."""

    def __init__(self, chunks, raise_at=None, exc=None):
        self._chunks = chunks
        self._raise_at = raise_at   # raise during iteration after N chunks
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    @property
    def text_stream(self):
        async def _gen():
            for i, c in enumerate(self._chunks):
                if self._raise_at is not None and i == self._raise_at:
                    raise self._exc
                yield c
        return _gen()

    async def get_final_message(self):
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = MagicMock(input_tokens=1, output_tokens=1, cache_read_input_tokens=0)
        return final


class _RaisingEnter:
    """A stream manager that raises on __aenter__ (connection never opened)."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc_info):
        return False


def _fake_client(behaviors):
    """A client whose messages.stream() returns the next scripted behavior."""
    state = {"n": 0}

    def _stream(**kwargs):
        b = behaviors[min(state["n"], len(behaviors) - 1)]
        state["n"] += 1
        return b

    client = MagicMock()
    client.messages.stream.side_effect = _stream
    client._calls = state
    return client


def _drain(behaviors):
    async def run():
        return [item async for item in actor_client.stream_round("SYS", [], [])]
    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value="claude-test"), \
         patch("tutor.agent.turn_stages.actor_client.async_client", return_value=_fake_client(behaviors)):
        return asyncio.run(run())


def test_retries_once_when_connection_drops_before_first_token():
    # Attempt 1 fails on connect; attempt 2 streams normally.
    events = _drain([
        _RaisingEnter(_conn_error()),
        _WorkingStream(["Hello, ", "world."]),
    ])
    chunks = [t for k, t in events if k == "chunk"]
    finals = [t for k, t in events if k == "final"]
    assert chunks == ["Hello, ", "world."]
    assert len(finals) == 1


def test_timeout_error_is_retried_too():
    # APITimeoutError subclasses APIConnectionError, so it is covered.
    events = _drain([
        _RaisingEnter(_timeout_error()),
        _WorkingStream(["ok"]),
    ])
    assert [t for k, t in events if k == "chunk"] == ["ok"]


def test_does_not_retry_after_a_token_was_emitted():
    # First chunk is delivered, then the connection drops mid-stream. Retrying
    # would re-deliver "partial" -> duplicate output, so it must propagate.
    behaviors = [
        _WorkingStream(["partial", "MORE"], raise_at=1, exc=_conn_error()),
        _WorkingStream(["SHOULD-NOT-APPEAR"]),
    ]

    async def run():
        out = []
        with pytest.raises(anthropic.APIConnectionError):
            async for item in actor_client.stream_round("SYS", [], []):
                out.append(item)
        return out

    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value="claude-test"), \
         patch("tutor.agent.turn_stages.actor_client.async_client", return_value=_fake_client(behaviors)):
        out = asyncio.run(run())

    chunks = [t for k, t in out if k == "chunk"]
    assert chunks == ["partial"]                 # only the pre-failure token
    assert "SHOULD-NOT-APPEAR" not in chunks      # no silent re-attempt


def test_gives_up_after_one_retry():
    # Two consecutive connect failures: the single retry is exhausted and the
    # error surfaces (run_turn turns it into a friendly error event).
    behaviors = [_RaisingEnter(_conn_error()), _RaisingEnter(_conn_error())]

    async def run():
        with pytest.raises(anthropic.APIConnectionError):
            async for _ in actor_client.stream_round("SYS", [], []):
                pass

    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value="claude-test"), \
         patch("tutor.agent.turn_stages.actor_client.async_client", return_value=_fake_client(behaviors)):
        asyncio.run(run())


def test_async_client_is_closed_after_stream():
    # The httpx pool must be closed inside the live loop (the sim's per-turn
    # asyncio.run() closes the loop on return), so GC never aclose()s a dead one.
    closed = {"n": 0}

    async def _close():
        closed["n"] += 1

    client = _fake_client([_WorkingStream(["a", "b"])])
    client.aclose = _close  # real AsyncAnthropic exposes aclose(); prefer it

    async def run():
        return [item async for item in actor_client.stream_round("SYS", [], [])]

    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value="claude-test"), \
         patch("tutor.agent.turn_stages.actor_client.async_client", return_value=client):
        events = asyncio.run(run())

    assert [t for k, t in events if k == "chunk"] == ["a", "b"]
    assert closed["n"] == 1


def test_async_client_is_closed_even_when_stream_errors():
    closed = {"n": 0}

    async def _close():
        closed["n"] += 1

    client = _fake_client([_RaisingEnter(_conn_error()), _RaisingEnter(_conn_error())])
    client.aclose = _close  # real AsyncAnthropic exposes aclose(); prefer it

    async def run():
        with pytest.raises(anthropic.APIConnectionError):
            async for _ in actor_client.stream_round("SYS", [], []):
                pass

    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value="claude-test"), \
         patch("tutor.agent.turn_stages.actor_client.async_client", return_value=client):
        asyncio.run(run())

    assert closed["n"] == 1  # closed despite the error propagating


def test_no_failure_passes_through_cleanly():
    # The happy path is unchanged: one attempt, chunks + final, no retry.
    client_box = {}

    def _capture(*a, **k):
        c = _fake_client([_WorkingStream(["a", "b"])])
        client_box["c"] = c
        return c

    async def run():
        return [item async for item in actor_client.stream_round("SYS", [], [])]

    with patch("tutor.agent.turn_stages.actor_client.actor_model", return_value="claude-test"), \
         patch("tutor.agent.turn_stages.actor_client.async_client", side_effect=_capture):
        events = asyncio.run(run())

    assert [t for k, t in events if k == "chunk"] == ["a", "b"]
    assert client_box["c"].messages.stream.call_count == 1
