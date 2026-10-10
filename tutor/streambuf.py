"""Per-turn event buffers: a dropped connection cannot lose a reply.

Capability item 6. Before this module, the chat route drove the agent
generator directly from the HTTP response, so a client disconnect (phone lock,
elevator, flaky wifi) cancelled generation mid-turn: the student lost the
answer, and the turn's persistence/telemetry never ran.

Now the route runs the agent in a background task that appends every event to
a TurnBuffer here, and the HTTP response merely reads the buffer. A disconnect
cancels only the reader; generation runs to completion, persists normally, and
the client can resume the stream from any offset via
``GET /chat/message/{turn_id}?offset=N``.

State is in-memory and per-process, the same single-instance trade-off as
alerts/selfheal (see the CLAUDE.md architecture note). Buffers are small (one
turn's events) and pruned by TTL + count cap.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from typing import Any, AsyncIterator, Optional

from tutor.utctime import utcnow

logger = logging.getLogger("tutor.streambuf")

TURN_TTL = datetime.timedelta(minutes=10)
MAX_BUFFERS = 100

_lock = threading.Lock()
_buffers: dict[str, "TurnBuffer"] = {}


class TurnBuffer:
    def __init__(self, turn_id: str, student_id: int, course: Optional[str] = None):
        self.turn_id = turn_id
        self.student_id = student_id
        self.course = course
        self.created = utcnow()
        self.events: list[dict[str, Any]] = []
        self.done = False
        self._cond = asyncio.Condition()
        # Strong reference to the producing task: asyncio only keeps weak refs,
        # so without this a long turn's task could be garbage-collected mid-run.
        self.task: Optional[asyncio.Task] = None

    async def append(self, event: dict[str, Any]) -> None:
        async with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    async def finish(self) -> None:
        async with self._cond:
            self.done = True
            self._cond.notify_all()

    async def _wait_past(self, idx: int) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: len(self.events) > idx or self.done)


def create(turn_id: str, student_id: int, course: Optional[str] = None) -> TurnBuffer:
    buf = TurnBuffer(turn_id, student_id, course)
    with _lock:
        _prune_locked()
        _buffers[turn_id] = buf
    return buf


def get(turn_id: str) -> Optional[TurnBuffer]:
    with _lock:
        return _buffers.get(turn_id)


def running_for(student_id: int, course: Optional[str] = None,
                exclude: Optional[str] = None) -> Optional[TurnBuffer]:
    """The student's reply still being written (in this course), newest
    first: what a reloaded or second page reattaches to
    (R30)."""
    with _lock:
        live = [b for b in _buffers.values()
                if b.student_id == student_id and not b.done and b.turn_id != exclude
                and (course is None or b.course == course) and _writing(b)]
    # The oldest: a busy or capped turn's buffer is newer, and lives briefly.
    return min(live, key=lambda b: b.created, default=None)


def _writing(buf: "TurnBuffer") -> bool:
    """A reply is still being written: not a busy answer, not a refusal,
    and not a reply already saved (its ``done`` is in; the history shows it,
    and replaying it left stray badges; the Stage 4 review)."""
    for event in list(buf.events):
        kind = event.get("type") if isinstance(event, dict) else None
        if kind in ("busy", "done"):
            return False
    return True


def live_buffers() -> list["TurnBuffer"]:
    """Every buffer whose producer is still running (for the shutdown)."""
    with _lock:
        return [b for b in _buffers.values() if not b.done]


def _prune_locked() -> None:
    """Drop finished buffers past TTL; under the count cap, evict the oldest
    finished ones. A still-running turn is never evicted (its task holds the
    only strong reference to the reply)."""
    now = utcnow()
    expired = [
        tid for tid, b in _buffers.items()
        if b.done and now - b.created > TURN_TTL
    ]
    for tid in expired:
        del _buffers[tid]
    if len(_buffers) >= MAX_BUFFERS:
        finished = sorted(
            (b for b in _buffers.values() if b.done), key=lambda b: b.created
        )
        for b in finished[: max(0, len(_buffers) - MAX_BUFFERS + 1)]:
            del _buffers[b.turn_id]
    stuck = [b for b in _buffers.values() if not b.done and now - b.created > 3 * TURN_TTL]
    for b in stuck:  # pragma: no cover - defensive; run_turn has its own timeouts
        logger.warning("streambuf: turn %s still running after %s", b.turn_id, 3 * TURN_TTL)


async def stream(turn_id: str, start: int = 0) -> AsyncIterator[dict[str, Any]]:
    """Yield buffered events from ``start``, waiting for new ones until the
    producer finishes. Returns immediately for an unknown turn."""
    buf = get(turn_id)
    if buf is None:
        return
    idx = max(0, start)
    while True:
        await buf._wait_past(idx)
        while idx < len(buf.events):
            yield buf.events[idx]
            idx += 1
        if buf.done and idx >= len(buf.events):
            return


def _reset_for_tests() -> None:
    with _lock:
        _buffers.clear()
