"""What the student and the history see when a turn fails."""

from __future__ import annotations

import logging

import anthropic
import httpx

EMPTY_REPLY_MESSAGE = (
    "I couldn't produce an answer that time: the reply came back empty. Please send "
    "your message again; for a long file, you can paste just the part you want me to look at."
)
EMPTY_REPLY_NOTE = "[No reply: the answer came back empty.]"


def error_kind(exc) -> str:
    """Classify an agent failure for the student message and the history note:
    "unavailable" (bad key, auth, or credit), "overloaded" (429 or 5xx),
    "connection" (network or timeout), or "other"."""
    s = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status == 401 or any(k in s for k in ("authentication_error", "401", "invalid x-api-key", "api key", "credit")):
        return "unavailable"
    if (status == 429 or (isinstance(status, int) and status >= 500)
            or ("rate" in s and "limit" in s) or "overloaded" in s or "529" in s):
        return "overloaded"
    if isinstance(exc, (anthropic.APIConnectionError, httpx.TransportError)):
        return "connection"
    return "other"


# What history keeps when the actor stream fails mid-turn (the student already
# saw the friendly error event). The note names the real cause instead of
# always blaming the connection, marks a partial reply as incomplete, and
# keeps user <-> assistant role alternation, which a blank or missing reply
# would break on the next turn.
_ERROR_NOTES = {
    "unavailable": "the tutor was not available (the AI service was not set up or had no credit)",
    "overloaded": "the tutor was overloaded at the time",
    "connection": "the tutor lost its connection to the AI service",
    "other": "the tutor ran into a problem",
}


def stream_error_note(kind: str, partial: bool) -> str:
    reason = _ERROR_NOTES.get(kind, _ERROR_NOTES["other"])
    return f"[Reply incomplete: {reason}.]" if partial else f"[No reply: {reason}.]"


# Saved by the chat route when a turn crashes after the student's message was
# committed (plan step 2.5), so history never ends on an unanswered message.
CRASH_NOTE = stream_error_note("other", partial=False)


def friendly_error(exc, *, failure: str = "stream") -> str:
    """User-facing message for an agent failure; never leaks raw API errors.

    The log line carries the traceback (a crash mid-turn was undebuggable
    without it), and the failure is counted toward the burst alert in
    ``tutor.agent.failures`` (review step 0.8).
    """
    from tutor.agent import failures

    logging.getLogger("tutor").error("agent error: %s", exc,
                                     exc_info=exc if isinstance(exc, BaseException) else None)
    failures.note(failure)
    kind = error_kind(exc)
    if kind == "unavailable":
        return ("The tutor isn't available right now: the AI service hasn't been set up yet. "
                "You can still browse the slides and the 📖 textbook. Please check back later.")
    if kind == "overloaded":
        return "The tutor is briefly overloaded right now. Please wait a few seconds and try again."
    return "Sorry, I ran into a problem answering that. Please try again in a moment."
