"""Photo-to-code transcription (capability item 7).

A student photographs handwritten, whiteboard, or on-paper code; the
supervisor-tier model (vision-capable, cheap) transcribes it to plain text,
which lands in the code box for the student to REVIEW and then submit through
the normal evaluate path.

Deliberate design: images never enter the supervised turn pipeline. The caps,
classifier, envelope, detector, and judge all reason over text; keeping vision
as a bounded pre-step means none of those layers change, the student sees
exactly what will be graded before grading happens (transcription mistakes are
visible and editable), and the cost is one cheap supervisor call bounded by a
per-student daily limit below, not by the turn spend caps.

Rate-limit state is in-memory and per-process, the same single-instance
trade-off as alerts/selfheal/streambuf (see the CLAUDE.md note).
"""

from __future__ import annotations

import base64
import collections
import datetime
import logging
import threading

from typing import Any, Optional

import anthropic

from tutor.agent.models import SUPERVISOR
from tutor.config import get_config
from tutor.courses import IDENTITY

logger = logging.getLogger("tutor.vision")

MAX_IMAGE_BYTES = 6 * 1024 * 1024
DAILY_PHOTO_LIMIT = 20
_WINDOW = datetime.timedelta(hours=24)

_PROMPT = (
    "Transcribe the code in this image exactly as written, preserving the "
    "indentation and line breaks. Output ONLY the code itself: no commentary, "
    "no explanation, no markdown fences. If the handwriting is ambiguous, "
    f"choose the most plausible valid {IDENTITY.code_language or 'code'} reading. If the image contains no "
    "code at all, output exactly NO_CODE."
)

_lock = threading.Lock()
_uses: dict[int, collections.deque[datetime.datetime]] = {}

# Magic-number prefixes for the types the model accepts (the client normalizes
# to JPEG, but be liberal in what the endpoint receives).
_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # bytes 8..12 checked in sniff()
]


def sniff(data: bytes) -> str | None:
    """Return the media type for supported image bytes, else None."""
    for magic, mt in _MAGIC:
        if data.startswith(magic):
            if mt == "image/webp" and data[8:12] != b"WEBP":
                return None
            return mt
    return None


def allow(student_id: int) -> bool:
    """Per-student daily photo budget; True consumes one slot."""
    from tutor.utctime import utcnow

    now = utcnow()
    with _lock:
        dq = _uses.setdefault(student_id, collections.deque())
        while dq and now - dq[0] > _WINDOW:
            dq.popleft()
        if len(dq) >= DAILY_PHOTO_LIMIT:
            return False
        dq.append(now)
        return True


def release(student_id: int) -> None:
    """Give back the slot ``allow`` took for a read that never reached the
    model (it failed before any usage): retrying a failed read used up the
    day's photos (the Stage 3 review)."""
    with _lock:
        dq = _uses.get(student_id)
        if dq:
            dq.pop()


def frees_at(student_id: int) -> Optional[datetime.datetime]:
    """When the oldest slot in the window frees, as naive UTC, or None."""
    with _lock:
        dq = _uses.get(student_id)
        return dq[0] + _WINDOW if dq else None


def _reset_for_tests() -> None:
    with _lock:
        _uses.clear()


def _client() -> anthropic.Anthropic:
    from tutor.agent.clients import supervisor_client

    # Image input takes longer to process than a text-only supervisor call.
    return supervisor_client(api_key=get_config()["anthropic_api_key"], timeout=45.0)


def _payload(data: bytes, media_type: str) -> Any:
    """Message payload for the vision call. Typed Any: the SDK's TypedDict
    union does not narrow from a dict literal under mypy, and this stays a
    plain data either way."""
    return [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(data).decode(),
                },
            },
            {"type": "text", "text": _PROMPT},
        ],
    }]


def transcribe_image(data: bytes, media_type: str) -> dict[str, object]:
    """One vision call; returns {"ok": True, "code": str} or {"ok": False,
    "error": str}. Never raises."""
    try:
        resp = _client().messages.create(
            model=SUPERVISOR.id,
            max_tokens=2000,
            messages=_payload(data, media_type),
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        usage = getattr(resp, "usage", None)
        logger.info(
            "photo transcription: in=%s out=%s",
            getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
        )
        # The caller ledgers the call: photo spend went uncounted, past the
        # quotas and the class cap (R26).
        from tutor.agent.llm_response import usage_counts

        counts = usage_counts(usage)
        # Strip fences the model sometimes adds despite the prompt.
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        if not text or text == "NO_CODE":
            return {"ok": False, "error": "No code found in the photo. Try a closer, sharper shot.",
                    "usage": counts}
        return {"ok": True, "code": text, "usage": counts}
    except Exception as exc:  # noqa: BLE001 - endpoint reports, never 500s
        logger.error("photo transcription failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": "Could not read the photo right now. Please try again."}
