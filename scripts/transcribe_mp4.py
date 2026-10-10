#!/usr/bin/env python3
"""Transcribe MP4 files using the whisper CLI tool.

Usage:
    python scripts/transcribe_mp4.py <input_dir> [output_dir] [options]

Examples:
    python scripts/transcribe_mp4.py source-materials/recordings/AST101-2026Fall
    python scripts/transcribe_mp4.py source-materials/recordings/AST101-2026Fall \
        knowledge-base/transcripts/AST101-2026Fall --format json
    python scripts/transcribe_mp4.py source-materials/recordings/AST102-2026Fall --model large --format txt

The index reads JSON (``--format json``) from a folder under
knowledge-base/transcripts/ named ``<course id>-<term>``, the course id one
from courses.yaml, so the second example is ready for ``python -m pipeline
index-transcripts``.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SUPPORTED = {".mp4", ".m4a", ".wav", ".webm", ".mp3", ".mpeg", ".mpga"}
DEFAULT_MODEL = "medium"
DEFAULT_FORMAT = "txt"


def find_whisper() -> str:
    whisper = shutil.which("whisper")
    if not whisper:
        sys.exit("whisper CLI not found. Install with: pip install openai-whisper")
    return whisper


def transcribe_dir(
    input_dir: Path,
    output_dir: Path,
    model: str = DEFAULT_MODEL,
    fmt: str = DEFAULT_FORMAT,
    language: str = "en",
    skip_existing: bool = True,
) -> None:
    whisper_bin = find_whisper()
    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in input_dir.iterdir() if p.suffix.lower() in SUPPORTED)
    if not files:
        print(f"No audio/video files found in {input_dir}")
        return

    print(f"Found {len(files)} file(s) in {input_dir}")
    print(f"Output dir: {output_dir}  model: {model}  format: {fmt}")
    print()

    done = 0
    skipped = 0
    for i, mp4 in enumerate(files, 1):
        out_file = output_dir / f"{mp4.stem}.{fmt}"
        if skip_existing and out_file.exists():
            print(f"[{i}/{len(files)}] SKIP  {mp4.name}")
            skipped += 1
            continue

        print(f"[{i}/{len(files)}] Transcribing  {mp4.name} ...", flush=True)
        cmd = [
            whisper_bin,
            str(mp4),
            "--model", model,
            "--language", language,
            "--output_dir", str(output_dir),
            "--output_format", fmt,
            "--verbose", "True",
        ]
        # Stream output so progress is visible (not captured); whisper writes
        # its tqdm progress bar to stderr and segment text to stdout.
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print(f"  ERROR (exit {proc.returncode})")
        else:
            size = out_file.stat().st_size if out_file.exists() else 0
            print(f"  -> {out_file.name}  ({size:,} bytes)")
            done += 1

    print()
    print(f"Done: {done} transcribed, {skipped} skipped, {len(files) - done - skipped} failed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe MP4s with Whisper CLI")
    parser.add_argument("input_dir", help="Directory containing video/audio files")
    parser.add_argument(
        "output_dir",
        nargs="?",
        help="Where to write transcripts (default: <input_dir>/transcripts/)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
                        help="Whisper model size (default: medium)")
    parser.add_argument("--format", dest="fmt", default=DEFAULT_FORMAT,
                        choices=["txt", "json", "srt", "vtt", "tsv"],
                        help="Output format (default: txt)")
    parser.add_argument("--language", default="en", help="Language code (default: en)")
    parser.add_argument("--no-skip", action="store_true",
                        help="Re-transcribe even if output already exists")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        sys.exit(f"Input directory not found: {input_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else input_dir / "transcripts"

    transcribe_dir(
        input_dir=input_dir,
        output_dir=output_dir,
        model=args.model,
        fmt=args.fmt,
        language=args.language,
        skip_existing=not args.no_skip,
    )


if __name__ == "__main__":
    main()
