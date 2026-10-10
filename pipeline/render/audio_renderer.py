"""Synthesize per-slide narration audio for a lecture with ElevenLabs.

Input is a lecture ``script.md`` (an instructor teaching script). Only the
spoken words are synthesized: the ``> "..."`` *What to say* blockquotes and the
``**Transition:** "..."`` connectors. Prep checklists, stage directions
(``*Pause*``), and slide markers are skipped.

Narration is bucketed per slide using the ``<!-- ADVANCE SLIDE N -->`` markers
(any trailing parenthetical is tolerated; anything before slide 1 — the opening —
is folded into slide 1). Each slide's prose is synthesized to
``lec_dir/audio/slide-NN.mp3``. The segments are then concatenated into
``lec_dir/audio/lecture-NN.mp3`` and their measured durations written to
``lec_dir/timing.json`` — exactly the two inputs ``render_video`` consumes, so
``render-video`` gets frame-accurate per-slide timing without relying on the
``~X min`` estimates in the script.

Credentials come from the environment (``ELEVENLABS_API_KEY`` /
``ELEVENLABS_VOICE_ID``), loaded via ``load_dotenv()`` like the rest of the
pipeline. This module deliberately does not import the ``tutor`` app; the two
deliverables share data dirs, not code.

Requires ffmpeg + ffprobe on PATH (brew install ffmpeg).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import httpx

_API = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_DEFAULT_MODEL = "eleven_turbo_v2_5"   # matches the app's TTS; fast + cheap
_MAX_CHARS = 4500                      # ElevenLabs per-request ceiling (safety margin under 5000)
_OUTPUT_FORMAT = "mp3_44100_64"        # half the default size, as the app's tts.py requests

# "<!-- ADVANCE SLIDE 9 (Command-Line Compilation) -->" — tolerate the parenthetical
_MARKER_RE = re.compile(r"<!--\s*ADVANCE SLIDE\s+(\d+).*?-->")


# ---------------------------------------------------------------------------
# Narration extraction
# ---------------------------------------------------------------------------

def _clean_for_speech(text: str) -> str:
    """Strip residual markdown so the voice reads prose, not syntax.

    The narration is already plain quoted prose, but occasionally embeds inline
    code (``.h``), links, or emphasis. Fenced code never appears inside a
    blockquote, so there is nothing to elide — just unwrap the light markup.
    """
    text = re.sub(r"`([^`]+)`", r"\1", text)              # inline code -> bare text
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)      # images
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links -> visible text
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)        # bold
    text = re.sub(r"(?<!\w)[*_](?=\w)|(?<=\w)[*_](?!\w)", "", text)  # stray emphasis
    return text.strip()


# A "**Bold header:**" at the start of a line (Talking Points, Code Demo, etc.)
_BOLD_HDR = re.compile(r"^\*\*([^*]+?):?\*\*\s*(.*)$")


def _extract_quote(text: str) -> str:
    """Return the spoken text of a quote, tolerating an optional ``> `` prefix and
    surrounding double quotes. Returns '' for stage directions (``*...*``) or empty."""
    text = text.lstrip(">").strip()
    if text.startswith("*") and text.endswith("*"):
        return ""  # stage direction, not spoken
    return text.strip('"').strip()


def parse_narration(script_text: str) -> dict[int, str]:
    """Return ``{slide_number: narration_text}`` for spoken words only.

    Spoken text is the quote under each ``**What to say...**`` header (whether
    written as a ``> "..."`` blockquote or a plain ``"..."`` line) plus each
    ``**Transition:** "..."`` connector. Talking Points, Code Demo, Student
    Interaction, and stage directions (``*...*``) are skipped. Content before the
    first ``ADVANCE SLIDE`` marker (the opening) folds into slide 1; slides with
    no spoken text are omitted.
    """
    parts = _MARKER_RE.split(script_text)
    # parts = [pre, num1, body1, num2, body2, ...]
    segments: list[tuple[int, str]] = [(1, parts[0])]
    for i in range(1, len(parts), 2):
        segments.append((int(parts[i]), parts[i + 1]))

    buckets: dict[int, list[str]] = {}
    for slide_num, block in segments:
        capturing = False  # inside a "What to say" section, awaiting its quote
        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                continue
            hdr = _BOLD_HDR.match(line)
            if hdr:
                label = hdr.group(1).strip().lower()
                if label.startswith("what to say"):
                    capturing = True
                    inline = _extract_quote(hdr.group(2))  # rare: quote on same line
                    if inline:
                        buckets.setdefault(slide_num, []).append(inline)
                        capturing = False
                elif label.startswith("transition"):
                    capturing = False
                    spoken = _extract_quote(hdr.group(2))
                    if spoken:
                        buckets.setdefault(slide_num, []).append(spoken)
                else:
                    capturing = False  # Talking Points / Code Demo / Student Interaction / etc.
                continue
            if line.startswith(">"):
                # Old-format blockquote narration (always spoken).
                spoken = _extract_quote(line)
                if spoken:
                    buckets.setdefault(slide_num, []).append(spoken)
            elif capturing:
                spoken = _extract_quote(line)
                if spoken:
                    buckets.setdefault(slide_num, []).append(spoken)
                capturing = False  # one quote paragraph per header

    return {
        n: "\n\n".join(_clean_for_speech(s) for s in lines)
        for n, lines in sorted(buckets.items())
    }


def _chunk_text(text: str, limit: int = _MAX_CHARS) -> list[str]:
    """Split ``text`` into <=``limit`` char chunks on paragraph, then sentence,
    boundaries so no ElevenLabs request exceeds the per-call ceiling."""
    if len(text) <= limit:
        return [text]
    units = re.split(r"\n\n+", text)
    # If any single paragraph still exceeds the limit, break it on sentences.
    fine: list[str] = []
    for unit in units:
        if len(unit) <= limit:
            fine.append(unit)
        else:
            fine.extend(re.findall(r".+?(?:[.!?](?:\s|$)|$)", unit, flags=re.S) or [unit])
    chunks: list[str] = []
    current = ""
    for unit in fine:
        candidate = f"{current}\n\n{unit}".strip() if current else unit
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# ElevenLabs synthesis (cached)
# ---------------------------------------------------------------------------

def _synthesize_chunk(text: str, *, api_key: str, voice_id: str, model: str,
                      cache_dir: Path) -> bytes:
    """Return MP3 bytes for one <=_MAX_CHARS chunk, cached by voice+model+text."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{voice_id}:{model}:{text}".encode()).hexdigest()
    cached = cache_dir / f"{digest}.mp3"
    if cached.exists():
        return cached.read_bytes()

    # Retry transient network/5xx failures; ElevenLabs occasionally disconnects
    # mid-stream on a long batch. 4xx (quota, bad request) fail fast.
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            resp = httpx.post(
                _API.format(voice_id=voice_id),
                params={"output_format": _OUTPUT_FORMAT},
                headers={"xi-api-key": api_key, "accept": "audio/mpeg"},
                json={
                    "text": text,
                    "model_id": model,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                timeout=120,
            )
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()  # client error — no point retrying
            if resp.status_code >= 500:
                resp.raise_for_status()
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                raise
            last_exc = exc
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    else:  # pragma: no cover - loop always breaks or raises
        raise last_exc  # type: ignore[misc]
    resp.raise_for_status()
    tmp = cached.with_suffix(".tmp")
    try:
        tmp.write_bytes(resp.content)
        tmp.replace(cached)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return resp.content


def _ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")


def _concat_quote(path: Path) -> str:
    """A path quoted for an ffmpeg concat list: inside single quotes, a quote
    is written '\\'' (plan step 5.8, E26: a folder with an apostrophe broke
    the concat)."""
    return "'" + str(path).replace("'", "'\\''") + "'"


def _concat_mp3(parts: list[Path], out_path: Path) -> None:
    """Concatenate MP3 segments (same codec) into ``out_path`` via the concat demuxer."""
    if len(parts) == 1:
        out_path.write_bytes(parts[0].read_bytes())
        return
    listing = out_path.with_suffix(".concat.txt")
    listing.write_text("".join(f"file {_concat_quote(p.resolve())}\n" for p in parts))
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out_path)])
    listing.unlink(missing_ok=True)


def _ffprobe_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}:\n{result.stderr[-500:]}")
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_lecture_audio(
    lec_dir: Path,
    *,
    api_key: str,
    voice_id: str,
    model: str | None = None,
    cache_dir: Path,
    force: bool = False,
    verbose: bool = True,
) -> dict:
    """Synthesize per-slide narration for one lecture.

    Writes ``lec_dir/audio/slide-NN.mp3`` per slide, a concatenated
    ``lec_dir/audio/lecture-NN.mp3``, and ``lec_dir/timing.json``
    (``{slide_num: seconds}``). Returns a summary dict.

    Slide MP3s already present are reused unless ``force`` is set. Chunk-level
    ElevenLabs responses are cached in ``cache_dir`` regardless, so re-runs after
    an interruption cost nothing for already-synthesized text. ``model``
    defaults to ``_DEFAULT_MODEL``, the one place the pipeline names it.
    """
    model = model or _DEFAULT_MODEL
    script_path = lec_dir / "script.md"
    if not script_path.exists():
        raise FileNotFoundError(f"script.md missing — run `script` first: {script_path}")

    narration = parse_narration(script_path.read_text())
    if not narration:
        raise ValueError(f"No spoken narration found in {script_path}")

    audio_dir = lec_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    lec_match = re.search(r"lecture-(\d+)", lec_dir.name)
    tag = lec_match.group(0) if lec_match else lec_dir.name

    segments: list[Path] = []
    timing: dict[str, float] = {}
    total_chars = 0
    synthesized = 0

    for slide_num, text in narration.items():
        seg_path = audio_dir / f"slide-{slide_num:02d}.mp3"
        if seg_path.exists() and not force:
            if verbose:
                print(f"  slide {slide_num:2d}: cached ({seg_path.name})")
        else:
            chunks = _chunk_text(text)
            total_chars += len(text)
            synthesized += 1
            chunk_bytes = [
                _synthesize_chunk(c, api_key=api_key, voice_id=voice_id,
                                  model=model, cache_dir=cache_dir)
                for c in chunks
            ]
            if len(chunk_bytes) == 1:
                seg_path.write_bytes(chunk_bytes[0])
            else:
                tmp_parts = []
                for i, b in enumerate(chunk_bytes):
                    tp = audio_dir / f".slide-{slide_num:02d}-{i}.mp3"
                    tp.write_bytes(b)
                    tmp_parts.append(tp)
                _concat_mp3(tmp_parts, seg_path)
                for tp in tmp_parts:
                    tp.unlink(missing_ok=True)
            if verbose:
                print(f"  slide {slide_num:2d}: {len(text):5d} chars, "
                      f"{len(chunks)} chunk(s) -> {seg_path.name}")

        segments.append(seg_path)
        timing[str(slide_num)] = round(_ffprobe_seconds(seg_path), 3)

    lecture_mp3 = audio_dir / f"{tag}.mp3"
    _concat_mp3(segments, lecture_mp3)

    timing_path = lec_dir / "timing.json"
    timing_path.write_text(json.dumps(timing, indent=2) + "\n")

    total_seconds = sum(timing.values())
    if verbose:
        mins = total_seconds / 60
        print(f"  concatenated -> {lecture_mp3.relative_to(lec_dir.parent.parent)} "
              f"({mins:.1f} min)")
        print(f"  timing -> {timing_path.relative_to(lec_dir.parent.parent)}")

    return {
        "lecture_mp3": lecture_mp3,
        "timing_json": timing_path,
        "segments": segments,
        "slides": len(narration),
        "synthesized_slides": synthesized,
        "billed_chars": total_chars,
        "total_seconds": total_seconds,
    }
