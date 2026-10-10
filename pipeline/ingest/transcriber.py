"""Transcribe lecture recordings using local Whisper model.

Local Whisper transcribes a recording of any length in one pass. The 10-minute
chunking that used to run first answered the hosted API's 25 MB upload cap,
which does not apply here; it split sentences at the cuts and shifted every
later timestamp by each chunk's trailing silence (plan step 5.8, E25).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SUPPORTED_FORMATS = {".mp4", ".m4a", ".wav", ".webm", ".mp3", ".mpeg", ".mpga"}


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    source_file: str
    language: str
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments)

    @property
    def duration_seconds(self) -> float:
        if not self.segments:
            return 0.0
        return self.segments[-1].end


def transcribe_file(
    audio_path: Path,
    model_size: str = "medium",
    language: str = "en",
    verbose: bool = False,
) -> Transcript:
    try:
        import whisper
    except ImportError as e:
        raise ImportError("openai-whisper is required: pip install openai-whisper") from e

    if verbose:
        print(f"  Loading Whisper model '{model_size}'...")
    model = whisper.load_model(model_size)

    if verbose:
        print(f"  Transcribing {audio_path.name}...")
    result = model.transcribe(str(audio_path), language=language)
    return Transcript(
        source_file=audio_path.name,
        language=result.get("language", language),
        segments=[TranscriptSegment(start=seg["start"], end=seg["end"], text=seg["text"])
                  for seg in result["segments"]],
    )


def save_transcript(transcript: Transcript, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(transcript.source_file).stem
    out_path = output_dir / f"{stem}.json"
    payload = {
        "source_file": transcript.source_file,
        "language": transcript.language,
        "duration_seconds": transcript.duration_seconds,
        "full_text": transcript.full_text,
        "segments": [asdict(s) for s in transcript.segments],
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return out_path


def batch_transcribe(
    recordings_dir: Path,
    output_dir: Path,
    model_size: str = "medium",
    language: str = "en",
    skip_existing: bool = True,
    verbose: bool = False,
) -> list[Path]:
    recordings_dir = Path(recordings_dir)
    if not recordings_dir.exists():
        print(f"  Recordings directory not found: {recordings_dir}")
        return []

    audio_files = [
        p for p in recordings_dir.iterdir()
        if p.suffix.lower() in SUPPORTED_FORMATS
    ]
    if not audio_files:
        print(f"  No audio/video files found in {recordings_dir}")
        return []

    written: list[Path] = []
    for audio_path in sorted(audio_files):
        out_path = output_dir / f"{audio_path.stem}.json"
        if skip_existing and out_path.exists():
            if verbose:
                print(f"  Skipping (already transcribed): {audio_path.name}")
            written.append(out_path)
            continue
        transcript = transcribe_file(audio_path, model_size=model_size, language=language, verbose=verbose)
        out = save_transcript(transcript, output_dir)
        written.append(out)
        if verbose:
            print(f"  {audio_path.name}: {len(transcript.segments)} segments, {transcript.duration_seconds:.0f}s")

    return written
