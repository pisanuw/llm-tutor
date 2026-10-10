"""The actor client: the only place the actor model is called, and its cost.

Two calls reach the model: :func:`stream_round`, one streaming round of the
actor loop, and :func:`generate_once`, the buffered revise call. Tests fake
these two seams; the tool loop, detector, judge, and refinement stay real.
tests/test_single_actor.py allows Anthropic clients to be built only here and
in tutor/agent/clients.py (the bounded supervisor factory).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import anthropic
import httpx

from tutor.agent import prompt_builder
from tutor.agent.models import SUPERVISOR as _FALLBACK_SPEC
from tutor.agent.tools import TOOL_DEFINITIONS
from tutor.config import get_config

# Output caps. Raised in plan step 2.4 (finding A5): a round that echoed a long
# submission into evaluate_code was cut off, so no evaluation ran and an empty
# reply was saved. The pasted code is now attached server-side
# (actor_loop.attach_submitted_code), and the caps leave room for the rest.
MAX_TOKENS = 4096

# Cold-start hardening (2026-06-12). The live smoke run hit transient
# connection timeouts on the first, cold-cache turn of a session, which with
# the ~159K-char system prompt legitimately runs 56-80s before the first
# token. So: a generous READ budget that comfortably clears a slow cold-cache
# turn (it bounds a hung connection, not a slow-but-progressing stream), but a
# tight CONNECT budget so a dead connection fails fast and is retried instead
# of stalling the student.
ACTOR_HTTP_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
ACTOR_STREAM_RETRIES = 1

# A connection/timeout failure that happens before the first token is safe to
# replay (no student-visible output yet); APITimeoutError subclasses
# APIConnectionError, so this catches both.
_RETRYABLE_STREAM_ERROR = anthropic.APIConnectionError

# Capacity fallback (stability item 3). The SDK already retries
# 429/5xx twice underneath; when those retries are exhausted the actor call is
# retried ONCE on the supervisor-tier model instead of erroring out. A
# slightly weaker answer beats "try again in a moment" for a student, and the
# supervisor tier has separate rate limits, so an actor-tier overload rarely
# takes both down. Only before the first student-visible token, and never for
# auth/permission errors (those fail identically on any model).
FALLBACK_MODEL = _FALLBACK_SPEC.id

_cache_log = logging.getLogger("tutor.cache")


def actor_model() -> str:
    return get_config()["claude_model"]


def is_capacity_error(exc: BaseException) -> bool:
    """True for rate-limit (429) and server/overloaded (>=500, incl. 529)
    API errors: the cases where the same request may succeed on the
    supervisor-tier model right now."""
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    status = getattr(exc, "status_code", None)
    return status is not None and (status == 429 or status >= 500)


def create_with_fallback(client, **kwargs):
    """messages.create with the one-shot capacity fallback. Non-streaming
    calls have no partial-output hazard, so the retry is unconditional on a
    capacity error."""
    try:
        return client.messages.create(**kwargs)
    except anthropic.APIStatusError as exc:
        if not is_capacity_error(exc):
            raise
        _cache_log.warning(
            "actor capacity error (HTTP %s); retrying this call on %s",
            getattr(exc, "status_code", "?"), FALLBACK_MODEL,
        )
        return client.messages.create(**dict(kwargs, model=FALLBACK_MODEL))


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=get_config()["anthropic_api_key"],
        timeout=ACTOR_HTTP_TIMEOUT,
    )


def async_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=get_config()["anthropic_api_key"],
        timeout=ACTOR_HTTP_TIMEOUT,
    )


# Every actor call declares the SAME tool list (plan step 2.3, finding A9): the
# tool definitions sit in front of the cached persona prefix, so a call that
# declared fewer tools (or none, like the revise call) paid a fresh ~40K-token
# cache write on exactly the expensive turns. A call that must not start a
# tool round says so with tool_choice "none", and the envelope's per-turn
# limits (tools_for_envelope) are enforced by dispatch_tool refusing the call.
_TOOL_CHOICE_NONE = {"type": "none"}
TOOLS_OFF = {"tools": TOOL_DEFINITIONS, "tool_choice": _TOOL_CHOICE_NONE}


async def stream_round(system, messages, tools):
    """One streaming actor call with tools declared (S15). Async generator:
    yields ("chunk", text) per text delta, then ("final", Message).

    Cold-start hardening: a connection/timeout failure that happens *before*
    the first token is retried once (the SDK's own retries sit underneath, so
    this is a last-resort net for the cold-start blip the live run hit). Once
    any token has been yielded a retry could duplicate student-visible output,
    so from that point the error propagates to the actor loop's friendly
    error path.
    """
    note_cache_touch()
    stream_client = async_client()
    # The full list whenever any tool is allowed (a stable cached prefix);
    # dispatch_tool refuses what the envelope forbids (plan step 2.3).
    kwargs = ({"tools": TOOL_DEFINITIONS, "tool_choice": {"type": "auto"}} if tools
              else dict(TOOLS_OFF))
    attempt = 0
    model = actor_model()
    used_fallback = False
    try:
        while True:
            produced = False
            try:
                async with stream_client.messages.stream(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=messages,
                    **kwargs,
                ) as stream:
                    async for chunk in stream.text_stream:
                        produced = True
                        yield ("chunk", chunk)
                    final = await stream.get_final_message()
                yield ("final", final)
                return
            except _RETRYABLE_STREAM_ERROR as exc:
                if produced or attempt >= ACTOR_STREAM_RETRIES:
                    raise
                attempt += 1
                _cache_log.warning(
                    "actor stream connection error before first token "
                    "(retry %d/%d): %s",
                    attempt, ACTOR_STREAM_RETRIES, exc,
                )
            except anthropic.APIStatusError as exc:
                # Capacity fallback: only before any student-visible token,
                # only once, and only for 429/5xx (see is_capacity_error).
                if produced or used_fallback or not is_capacity_error(exc):
                    raise
                used_fallback = True
                model = FALLBACK_MODEL
                _cache_log.warning(
                    "actor stream capacity error (HTTP %s) before first "
                    "token; falling back to %s for this call",
                    getattr(exc, "status_code", "?"), model,
                )
    finally:
        # Close the async client's httpx pool inside the live event loop. The
        # sim drives each turn in its own asyncio.run(), which closes the loop on
        # return; without this the client is left for the GC to close on an
        # already-dead loop ("Event loop is closed" teardown noise). In the
        # server it prevents a per-turn connection-pool leak.
        await _aclose_async_client(stream_client)


async def _aclose_async_client(async_actor) -> None:
    """Best-effort close of an AsyncAnthropic client. No-op for test doubles
    whose close() is not awaitable."""
    import inspect

    closer = getattr(async_actor, "aclose", None) or getattr(async_actor, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:  # teardown must never surface as a turn error
        pass


# ---------------------------------------------------------------------------
# Prompt caching
# ---------------------------------------------------------------------------
# The system prompt is ~159K chars and identical turn to turn. We send it as a
# content-block list with a cache_control breakpoint on the stable persona
# prefix; per-request context (student state, progress, and the policy
# envelope) goes in a trailing UNCACHED block so the cached prefix never
# changes. Order is fixed: cached prefix first, varying context last, or the
# cache never hits.

# The persona prefix is cached for an hour, not the default five minutes
# (B1): students come a few times a day and
# often read for more than five minutes between messages, so a 5-minute entry
# had always expired and every first message paid the cold write. A 1-hour
# write costs 2x input instead of 1.25x; a read refreshes either for free.
CACHE_TTL = "1h"

# When the actor last read or wrote the cache (time.monotonic()), so the
# warm-up (turn_stages/cache_warm.py) skips while the entry is still alive.
# Process memory: the single-instance note in CLAUDE.md applies.
_last_cache_touch = 0.0


def note_cache_touch() -> None:
    global _last_cache_touch
    _last_cache_touch = time.monotonic()


def seconds_since_cache_touch() -> float:
    return time.monotonic() - _last_cache_touch if _last_cache_touch else float("inf")


def prewarm() -> "Served":
    """Write the cached prefix without generating anything (``max_tokens=0``:
    the API runs the prefill, writes the cache, and bills no output). Same
    model, tools, tool choice, and system prefix as a first actor round, so
    the next real round reads what this wrote."""
    # No capacity fallback: warming the supervisor model would help no one.
    resp = client().messages.create(
        model=actor_model(),
        max_tokens=0,
        system=system_blocks(),
        messages=[{"role": "user", "content": "(warm-up)"}],
        tools=TOOL_DEFINITIONS,
        tool_choice={"type": "auto"},
    )
    log_cache_usage(getattr(resp, "usage", None), "warm")
    return Served(getattr(resp, "usage", None), getattr(resp, "model", None))


def system_blocks(per_request: str = "") -> list[dict]:
    """Build the system content-block list with a cache breakpoint.

    The persona prefix carries ``cache_control: ephemeral`` so it is reused
    across turns; ``per_request`` (if any) is appended as a separate uncached
    block. Returns a list suitable for the Anthropic ``system`` parameter.
    """
    blocks: list[dict] = [{
        "type": "text",
        "text": prompt_builder.build_system_prompt(),
        "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
    }]
    if per_request and per_request.strip():
        blocks.append({"type": "text", "text": per_request})
    return blocks


def log_cache_usage(usage, phase: str) -> None:
    """Log token usage including cache reads (P6: everything observable).

    A healthy cache shows cache_read_input_tokens > 0 after the first turn.
    If it stays 0, the cached prefix is changing between turns (a bug).
    """
    if usage is None:
        return
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    _cache_log.info(
        "phase=%s in=%s out=%s cache_read=%s cache_creation=%s",
        phase,
        getattr(usage, "input_tokens", 0),
        getattr(usage, "output_tokens", 0),
        cache_read,
        cache_creation,
    )


class Served:
    """A call's usage plus the model that actually served it (the capacity
    fallback can swap the actor for another model mid-turn; plan step 2.1)."""

    __slots__ = ("usage", "model")

    def __init__(self, usage, model):
        self.usage = usage
        self.model = model if isinstance(model, str) and model else None


def generate_once(system, messages) -> tuple[str, object]:
    """One non-streaming actor call; returns (text, usage). The revise call
    after a judge 'revise' uses it: the draft must be buffered before anything
    reaches the student. The usage is wrapped in ``Served`` so the ledger
    prices the model that answered."""
    note_cache_touch()
    sync_client = client()
    resp = create_with_fallback(
        sync_client,
        model=actor_model(),
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        **TOOLS_OFF,
    )
    log_cache_usage(getattr(resp, "usage", None), "buffered")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, Served(getattr(resp, "usage", None), getattr(resp, "model", None))


class UsageLedger:
    """The turn's actor calls (plan step 2.1): every call's tokens, priced
    with the model that actually served it (the capacity fallback can swap
    it), cache writes included. Tool-side model calls (the rubric evaluator)
    are recorded as their own TurnEvents."""

    def __init__(self) -> None:
        self.totals: dict = {"in": 0, "out": 0, "cache": 0, "cache_write": 0, "cost": 0.0}
        self.model = actor_model()   # the model named on the turn's event

    def add(self, usage, model: Optional[str] = None) -> None:
        if isinstance(usage, Served):
            model = model or usage.model
            usage = usage.usage
        if usage is None:
            return
        from tutor.agent.llm_response import usage_counts
        from tutor.agent.usage import estimate_cost_usd

        m = model if isinstance(model, str) and model else actor_model()
        c = usage_counts(usage)
        self.totals["in"] += c["in_tokens"]
        self.totals["out"] += c["out_tokens"]
        self.totals["cache"] += c["cache_read_tokens"]
        self.totals["cache_write"] += c["cache_write_tokens"]
        self.totals["cost"] += estimate_cost_usd(
            m, c["in_tokens"], c["out_tokens"], c["cache_read_tokens"], c["cache_write_tokens"],
            c["cache_write_1h_tokens"],
        )
        self.model = m
