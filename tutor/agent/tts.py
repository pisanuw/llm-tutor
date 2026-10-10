"""ElevenLabs text-to-speech with markdown stripping and disk caching.

The instructor's cloned voice lives on ElevenLabs' servers and is referenced by
voice_id (not exported as a file). `synthesize` strips markdown/code from an agent
reply, hashes the cleaned prose, serves a cached MP3 if present, otherwise calls
ElevenLabs and caches the result. Returns None when voice is not configured so the
feature degrades gracefully (mirrors how optional auth providers are handled).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path

import httpx

from tutor.config import get_config

_API = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_MODEL = "eleven_turbo_v2_5"   # fast + cheap; good for short conversational replies
MAX_CHARS = 1500               # quota guardrail: never synth more than this per reply
# Half the default (128 kbps) size, so the on-disk cache grows half as fast; the
# lecture narration ships at 48 kbps mono and reads fine.
_OUTPUT_FORMAT = "mp3_44100_64"


def _clean_for_speech(md: str) -> str:
    """Strip markdown/code so the voice reads prose, not syntax."""
    # After an exam pass the reply quotes the student's held-back question
    # (turn.py: "Back to your question: “...”"). The cloned voice never reads
    # student text aloud (plan step 3.5, finding B22).
    md = re.sub(r"Back to your question: “.*?”(?=\s*(?:\n\n|$))",
                "Back to your question.", md, flags=re.S)
    md = re.sub(r"```.*?```", " (code shown on screen) ", md, flags=re.S)  # fenced code
    md = re.sub(r"`([^`]+)`", r"\1", md)              # inline code
    md = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", md)      # images
    md = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)  # links -> visible text
    md = re.sub(r"^\s{0,3}#{1,6}\s*", "", md, flags=re.M)  # headings
    md = re.sub(r"^\s*([-*_])(\s*\1){2,}\s*$", "", md, flags=re.M)  # horizontal rules
    # An identifier's underscore is a word break ("sift_down" reads "sift
    # down"), not emphasis to delete, which ran the words together.
    md = re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " ", md)
    md = re.sub(r"[*_>|#]", "", md)                   # leftover md punctuation
    return re.sub(r"\s+", " ", md).strip()


def speech_text(text: str) -> str:
    """The prose the voice reads for ``text``: markdown stripped, cut to the quota."""
    return _clean_for_speech(text)[:MAX_CHARS].strip()


def cache_path(text: str) -> Path | None:
    """Where the clip for ``text`` is, or would be, cached; None when the voice
    is off or there is nothing to say.

    The name is the voice id and the spoken text, hashed, so a reply the app
    serves verbatim (a Teach-me opener) can be rendered ahead of time under
    the name `synthesize` will look for: tutor/prewarm_tts.py.
    """
    cfg = get_config()
    voice = cfg["elevenlabs_voice_id"]
    if not cfg["elevenlabs_api_key"] or not voice:
        return None  # feature disabled — no ElevenLabs credentials
    speech = speech_text(text)
    if not speech:
        return None
    digest = hashlib.sha256(f"{voice}:{speech}".encode()).hexdigest()
    return cfg["tts_cache_dir"] / f"{digest}.mp3"


def synthesize(text: str, *, require_cache: bool = False) -> bytes | None:
    """Return MP3 bytes for `text`, or None if voice is disabled or text is empty.

    A clip that cannot be cached is still returned (the speaker button gets its
    audio), unless ``require_cache``: tutor/prewarm_tts.py renders only to
    fill the cache and stops on the first failed write, since every further
    call would bill for nothing.
    """
    path = cache_path(text)
    if path is None:
        return None
    try:
        if path.exists():
            return path.read_bytes()
    except OSError:
        pass                                # an unreadable cache entry: synthesize again

    cfg = get_config()
    resp = httpx.post(
        _API.format(voice_id=cfg["elevenlabs_voice_id"]),
        params={"output_format": _OUTPUT_FORMAT},
        headers={"xi-api-key": cfg["elevenlabs_api_key"], "accept": "audio/mpeg"},
        json={
            "text": speech_text(text),
            "model_id": _MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=60,
    )
    resp.raise_for_status()
    # The clip is paid for and in hand: caching it is best effort. Each call
    # writes its own temp file, so two students pressing the speaker on the
    # same opener no longer share (and delete) one, and a full or read-only
    # disk costs the cache, not the audio (R10).
    tmp_path = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(resp.content)
        tmp_path.replace(path)
    except OSError:
        logging.getLogger("tutor.tts").warning("tts: could not cache a clip", exc_info=True)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if require_cache:
            raise
    return resp.content
