"""Central logging configuration + a structured activity event stream.

Two responsibilities:

1. :func:`configure_logging` — make the ``tutor.*`` logger hierarchy actually
   emit, at a level set by the ``LOG_LEVEL`` env var (default INFO). Without
   this the app inherits uvicorn's root config and most of our ``.info()`` calls
   (cache usage, judge, eval details, startup) never surface. Idempotent: safe
   to call on every boot and from scripts.

2. :func:`activity` — a structured, one-JSON-line-per-event stream on the
   ``tutor.activity`` logger, for *later* analysis of student activity, the
   conversations students have with the tutor, and how the supervisor behaves.
   Events: ``login``, ``chat_turn``, ``exam`` (extend freely). Each line is a
   self-describing JSON object so it can be grepped now and shipped to a log
   store later. The durable record still lives in the DB (Message/TurnEvent);
   this is the tailable mirror.

Conversation content (student message + tutor reply) is included only when
``LOG_CONVERSATION_CONTENT`` is true (default: false) and passed via the
``content`` argument, so the privacy posture is one env flag, not scattered
call sites. Logs sit outside the retention policy and the erasure tools
(docs/retention-policy.md), so events carry ``student_id``, never an email:
:func:`activity` drops any ``email`` field.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Optional
from tutor.utctime import utcnow

# Per-field cap so a long transcript can't produce a multi-megabyte log line.
_CONTENT_CHARS = 8000

_ACTIVITY_LOGGER = "tutor.activity"
_ROOT_LOGGER = "tutor"

_log = logging.getLogger(_ACTIVITY_LOGGER)

# Activity verbosity tiers, from coarse to fine. Each activity() event is tagged
# with the tier at which it *first* appears; the configured ACTIVITY_LEVEL is the
# finest tier that still emits. So MINIMAL logs only major interactions (logins,
# completed/blocked/slow turns, errors), STANDARD adds routine per-turn detail,
# and VERBOSE adds fine-grained signals (turn starts, individual UI clicks).
#   ACTIVITY_LEVEL=minimal|standard|verbose   (default: standard)
_ACTIVITY_RANKS = {"minimal": 0, "standard": 1, "verbose": 2}
_DEFAULT_ACTIVITY_LEVEL = "standard"

# A completed turn slower than this many milliseconds is flagged (WARNING + a
# minimal-tier slow_turn event) so a "tutor is thinking forever" report has a
# searchable trail. Override with SLOW_TURN_MS.
_DEFAULT_SLOW_TURN_MS = 25000


def activity_level() -> str:
    """Configured activity verbosity name (minimal|standard|verbose)."""
    val = (os.getenv("ACTIVITY_LEVEL") or _DEFAULT_ACTIVITY_LEVEL).strip().lower()
    return val if val in _ACTIVITY_RANKS else _DEFAULT_ACTIVITY_LEVEL


def _level_emits(event_level: str) -> bool:
    """True when an event tagged ``event_level`` shows at the configured level."""
    want = _ACTIVITY_RANKS.get(event_level, _ACTIVITY_RANKS[_DEFAULT_ACTIVITY_LEVEL])
    have = _ACTIVITY_RANKS[activity_level()]
    return want <= have


def slow_turn_ms() -> int:
    """Latency (ms) above which a completed turn is flagged slow."""
    try:
        return int(os.getenv("SLOW_TURN_MS", str(_DEFAULT_SLOW_TURN_MS)))
    except (TypeError, ValueError):
        return _DEFAULT_SLOW_TURN_MS


def _truthy(val: Optional[str], default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def log_level() -> str:
    """The configured level name (upper-case), defaulting to INFO."""
    return (os.getenv("LOG_LEVEL") or "INFO").strip().upper()


def content_enabled() -> bool:
    """Whether activity() includes conversation text (LOG_CONVERSATION_CONTENT).

    Off unless set: a copy of every conversation in the host's log store is
    outside what the retention policy and the consent notice describe
    (finding C9, decision 7).
    """
    return _truthy(os.getenv("LOG_CONVERSATION_CONTENT"), default=False)


# Fields activity() never writes, whatever a call site passes: the log stream
# cannot be anonymized or erased, so it carries student_id instead.
_NEVER_LOGGED = frozenset({"email"})


def configure_logging() -> str:
    """Set the ``tutor`` level from LOG_LEVEL and attach stdout handlers.

    Returns the resolved level name so the caller can log a confirmation. Two
    independent handlers (both stdout, propagate=False so neither double-prints
    via the root/uvicorn config):
      - ``tutor``          : human-readable, level = LOG_LEVEL.
      - ``tutor.activity`` : bare JSON lines, always INFO (the event stream).
    """
    level_name = log_level()
    level = getattr(logging, level_name, logging.INFO)

    app = logging.getLogger(_ROOT_LOGGER)
    app.setLevel(level)
    if not any(getattr(h, "_tutor", False) for h in app.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        h._tutor = True  # type: ignore[attr-defined]
        app.addHandler(h)
    app.propagate = False

    act = logging.getLogger(_ACTIVITY_LOGGER)
    act.setLevel(logging.INFO)
    if not any(getattr(h, "_tutor_activity", False) for h in act.handlers):
        ah = logging.StreamHandler(sys.stdout)
        ah.setFormatter(logging.Formatter("%(message)s"))
        ah._tutor_activity = True  # type: ignore[attr-defined]
        act.addHandler(ah)
    act.propagate = False  # the bare-JSON handler is the only sink

    return level_name


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _CONTENT_CHARS:
        return value[:_CONTENT_CHARS] + f"…[+{len(value) - _CONTENT_CHARS} chars]"
    return value


def activity(
    event: str, *, level: str = "standard", content: Optional[dict] = None, **fields: Any
) -> None:
    """Emit one structured activity event as a JSON line (fail-soft).

    ``level`` is the verbosity tier at which this event first appears
    (``minimal``|``standard``|``verbose``); it is dropped when the configured
    ``ACTIVITY_LEVEL`` is coarser. ``fields`` are always-on metadata (ids,
    course, intent, verdict, cost…). ``content`` holds potentially-sensitive text
    (the student message, the tutor reply) and is included only when
    :func:`content_enabled`. Never raises — a logging failure must not break a
    turn.
    """
    try:
        if not _level_emits(level):
            return
        record: dict[str, Any] = {
            "event": event,
            "ts": utcnow().isoformat(timespec="seconds") + "Z",
        }
        for k, v in fields.items():
            if v is not None and k not in _NEVER_LOGGED:
                record[k] = v
        if content and content_enabled():
            record["content"] = {k: _clip(v) for k, v in content.items() if v is not None}
        _log.info(json.dumps(record, default=str, ensure_ascii=False))
    except Exception:  # observability must never take down the request path
        pass
