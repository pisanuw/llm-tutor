"""Combine PNG slide frames + narration audio into a lecture MP4 using ffmpeg.

Timing is derived from the ``<!-- ADVANCE SLIDE N -->`` markers in script.md
together with the audio file's actual per-segment durations.

Two modes
---------
1. **With audio** (``audio_path`` provided):
   Reads per-slide durations from a ``timing.json`` file (produced by the TTS
   step), then uses ffmpeg to stitch frames + audio into one video.

2. **Timed by script estimates** (no audio yet):
   Parses ``~X min`` timing hints from script.md headers and assigns each
   slide a proportional duration, producing a silent video for preview.

Output: ``lec_dir/lecture-NN.mp4``

Requirements: ffmpeg on PATH (brew install ffmpeg).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Timing extraction
# ---------------------------------------------------------------------------

# Matches "<!-- ADVANCE SLIDE 5 -->" and "<!-- ADVANCE SLIDE 9 (Command-Line Compilation) -->"
_MARKER_RE = re.compile(r"<!--\s*ADVANCE SLIDE\s+(\d+).*?-->")

# Matches "### Slide 5: Title (~8 min)" — captures slide number and minutes
_HEADER_RE = re.compile(r"#{2,3}\s+Slide\s+(\d+)\s*:[^\(]*\(~?\s*(\d+)\s*min")


def _parse_script_timing(script_text: str) -> dict[int, float]:
    """Return {slide_number: estimated_duration_seconds} from script timing hints.

    Falls back to equal distribution of 120 min across all slides.
    """
    timings: dict[int, float] = {}
    for m in _HEADER_RE.finditer(script_text):
        slide_num = int(m.group(1))
        minutes   = int(m.group(2))
        timings[slide_num] = minutes * 60.0

    if not timings:
        # Fallback: count how many slides are referenced via markers or headers
        slide_nums = sorted(set(int(m.group(1)) for m in _MARKER_RE.finditer(script_text)))
        if not slide_nums:
            return {}
        total = 120 * 60
        per_slide = total / len(slide_nums)
        return {n: per_slide for n in slide_nums}

    return timings


def _load_audio_timing(timing_json: Path) -> dict[int, float]:
    """Load per-slide durations from a TTS timing JSON file.

    Expected format::

        {"1": 45.2, "2": 120.5, ...}   # slide_num (str) -> seconds (float)
    """
    data = json.loads(timing_json.read_text())
    return {int(k): float(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _build_concat_input(frames: list[Path], durations: dict[int, float],
                         concat_file: Path) -> None:
    """Write an ffmpeg concat demuxer file mapping each frame to its duration."""
    lines: list[str] = []
    for frame in frames:
        # Extract slide number from filename "slide-05.png" -> 5
        slide_num = int(re.search(r"slide-(\d+)\.png", frame.name).group(1))
        dur = durations.get(slide_num, 30.0)  # default 30 s if not in timing
        lines.append(f"file '{frame.resolve()}'")
        lines.append(f"duration {dur:.3f}")
    # Repeat last frame (ffmpeg concat quirk)
    if frames:
        lines.append(f"file '{frames[-1].resolve()}'")
    concat_file.write_text("\n".join(lines) + "\n")


def _run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_video(
    lec_dir: Path,
    audio_path: Optional[Path] = None,
    timing_json: Optional[Path] = None,
    force: bool = False,
    fps: int = 1,
) -> Path:
    """Render one lecture video.

    Parameters
    ----------
    lec_dir:
        Lecture directory containing ``frames/`` and ``script.md``.
    audio_path:
        Path to the TTS narration audio file (mp3/wav/m4a). If omitted,
        a silent preview video is produced.
    timing_json:
        JSON file with per-slide durations ``{slide_num: seconds}``.
        Required when ``audio_path`` is provided.
        Auto-detected at ``lec_dir/timing.json`` if it exists.
    force:
        Re-render even if the output MP4 already exists.
    fps:
        Frame rate of the output video (1 fps is fine for slide presentations).

    Returns
    -------
    Path to the output MP4 file.
    """
    frames_dir = lec_dir / "frames"
    if not frames_dir.exists():
        raise FileNotFoundError(f"frames/ directory missing — run render-frames first: {lec_dir}")

    frames = sorted(frames_dir.glob("slide-*.png"))
    if not frames:
        raise FileNotFoundError(f"No slide-NN.png files in {frames_dir}")

    # Determine output path
    lec_num = re.search(r"lecture-(\d+)", lec_dir.name)
    tag = lec_num.group(0) if lec_num else lec_dir.name
    out_path = lec_dir / f"{tag}.mp4"

    if out_path.exists() and not force:
        return out_path

    # Resolve timing
    if timing_json is None:
        candidate = lec_dir / "timing.json"
        timing_json = candidate if candidate.exists() else None

    if timing_json and timing_json.exists():
        durations = _load_audio_timing(timing_json)
    else:
        script_path = lec_dir / "script.md"
        script_text = script_path.read_text() if script_path.exists() else ""
        durations = _parse_script_timing(script_text)
        if not durations:
            # Last-resort: distribute 120 min evenly
            per = 120 * 60 / len(frames)
            durations = {int(re.search(r"slide-(\d+)", f.name).group(1)): per
                         for f in frames}

    # Write ffmpeg concat file
    concat_file = lec_dir / "frames" / "_concat.txt"
    _build_concat_input(frames, durations, concat_file)

    if audio_path and Path(audio_path).exists():
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-i", str(audio_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            "-vf", f"fps={fps},scale=1920:1080",
            "-shortest",
            str(out_path),
        ])
    else:
        # Silent preview
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-vf", f"fps={fps},scale=1920:1080",
            "-an",
            str(out_path),
        ])

    return out_path


def render_course_videos(
    output_dir: Path,
    lecture: Optional[int] = None,
    audio_dir: Optional[Path] = None,
    force: bool = False,
) -> dict[int, Path]:
    """Render videos for all (or one) lectures in a course.

    ``audio_dir`` should contain files named ``lecture-NN.mp3`` (or .wav/.m4a).
    Returns ``{lecture_num: mp4_path}``.
    """
    nums = [lecture] if lecture else list(range(1, 21))
    results: dict[int, Path] = {}

    for num in nums:
        lec_dir = output_dir / f"lecture-{num:02d}"
        if not lec_dir.exists():
            continue
        audio: Optional[Path] = None
        if audio_dir:
            for ext in ("mp3", "wav", "m4a", "ogg"):
                candidate = audio_dir / f"lecture-{num:02d}.{ext}"
                if candidate.exists():
                    audio = candidate
                    break
        try:
            results[num] = render_video(lec_dir, audio_path=audio, force=force)
        except Exception as exc:
            print(f"  [warn] L{num:02d}: {exc}")

    return results
