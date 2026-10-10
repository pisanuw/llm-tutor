"""Warm the actor's prompt cache when a student opens the chat
(B1).

The persona prefix is about 44K tokens. When its cache entry has expired,
the first round of the next turn writes it again, which adds seconds before
the first word. Opening the chat page is the moment before a message: one
``max_tokens=0`` call then writes the entry while the student reads and
types, and the real turn reads it.

At most one warm-up per WARM_EVERY_SECONDS across the process, and none while
an actor call has touched the cache more recently than that (the 1-hour
entry is still alive). Each warm-up is ledgered as a ``cache_warm``
TurnEvent against the student who opened the page, so the spend caps and the
daily report count it.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session as DBSession

from tutor.agent import llm_response
from tutor.agent import state as state_mod
from tutor.agent import usage as usage_mod
from tutor.agent.turn_stages import actor_client
from tutor.config import get_config

# Under the 1-hour TTL, so an entry is rewarmed shortly before it would
# expire rather than just after.
WARM_EVERY_SECONDS = 50 * 60

_lock = threading.Lock()
log = logging.getLogger("tutor.cache")


def due() -> bool:
    cfg = get_config()
    if not cfg["cache_prewarm"] or cfg["use_claude_cli"]:
        return False
    return actor_client.seconds_since_cache_touch() >= WARM_EVERY_SECONDS


def warm(db: DBSession, student_id: int) -> bool:
    """Warm the cache if it is due. True when a warm-up call was made."""
    with _lock:
        if not due():
            return False
        actor_client.note_cache_touch()   # claim it: a second page load skips
    try:
        served = actor_client.prewarm()
    except Exception:
        log.warning("prompt cache warm-up failed", exc_info=True)
        return False
    try:
        counts = llm_response.usage_counts(served.usage)
        state_mod.record_turn_event(
            db, student_id=student_id, kind="cache_warm",
            **usage_mod.cost_fields(served.model or actor_client.actor_model(), counts),
        )
        db.commit()
    except Exception:
        db.rollback()
        log.warning("prompt cache warm-up: could not ledger its cost", exc_info=True)
    return True


def _reset_for_tests() -> None:
    actor_client._last_cache_touch = 0.0
