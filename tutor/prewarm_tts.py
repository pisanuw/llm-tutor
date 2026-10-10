"""Render the voice for replies the app serves verbatim, ahead of time.

The chat's 🔊 button posts a reply's text to /chat/tts, which speaks it only
when it is one of the student's recent tutor replies, and ``synthesize`` in
tutor/agent/tts.py serves the clip from TTS_CACHE_DIR when one exists for that
voice and text, calling ElevenLabs only on a miss. A live reply is unique, so
that cache rarely hits. The Teach-me openers (curriculum/teachme/<course>/
<goal>.json) are not: /chat/teachme inserts the opener as the assistant message
verbatim, so a clip rendered here once is served to every student who presses
🔊 on that opener, and ElevenLabs bills the opener once instead of once per
student.

A dry run by default: it prints what would be rendered and what it would bill.
``--spend`` calls ElevenLabs (ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID, from
.env) and writes into TTS_CACHE_DIR (default tts-cache/) through the function
the app uses, so the file names match by construction. It lives in the package
because it runs inside the container, like tutor.migrate: over ssh into the
service, as the app user so the app can keep writing to the cache afterwards
(DEPLOY.md), or locally with --cache-dir. A run that stops (an ElevenLabs error, --budget) is resumed by
running it again: what is cached is skipped. A second --spend while one runs
refuses (exit 3): the two would render the same clips twice.

    python -m tutor.prewarm_tts                           # dry run: brief openers, every track
    python -m tutor.prewarm_tts --depth brief standard --course AST101
    python -m tutor.prewarm_tts --track AST101-lecture-order --spend
    python -m tutor.prewarm_tts --budget 120000 --spend   # stop after that many billed characters
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from tutor.courses import COURSES
from tutor.paths import CURRICULUM_DIR

ROOT = Path(__file__).resolve().parents[1]
TEACHME_DIR = CURRICULUM_DIR / "teachme"
DEPTHS = ("brief", "standard", "thorough")


@dataclass(frozen=True)
class Opener:
    course: str
    goal_id: str
    track: str
    depth: str
    text: str

    @property
    def label(self) -> str:
        return f"{self.course}/{self.goal_id} [{self.track}/{self.depth}]"


def collect(teachme_dir: Path, courses=(), depths=DEPTHS, tracks=()) -> list[Opener]:
    """Every opener under ``teachme_dir`` that passes the filters, in file order.
    An empty filter means everything."""
    found: list[Opener] = []
    for course_dir in sorted(p for p in teachme_dir.iterdir() if p.is_dir()):
        course = course_dir.name
        if courses and course not in courses:
            continue
        for path in sorted(course_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            goal_id = data.get("goal_id") or path.stem
            for track, by_depth in (data.get("variants") or {}).items():
                if tracks and track not in tracks:
                    continue
                for depth in depths:
                    text = (by_depth or {}).get(depth)
                    if text:
                        found.append(Opener(course, goal_id, track, depth, text))
    return found


def _hold_lock(cache_dir: Path):
    """An exclusive lock on the cache for the life of this process, or None
    when another run holds it. Two runs at once render the same clips twice:
    each checks the cache before a clip, and both find the next one missing.
    (On 2026-09-28 a second run doubled the bill: the first was alive but
    unseen by pgrep from another Render ssh session.)"""
    import fcntl

    cache_dir.mkdir(parents=True, exist_ok=True)
    fh = open(cache_dir / ".prewarm.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Pre-render the Teach-me openers into the chat's voice cache")
    p.add_argument("--course", action="append", choices=COURSES,
                   help="one course (repeatable; default: every course)")
    p.add_argument("--depth", nargs="+", choices=DEPTHS, default=["brief"],
                   help="opener depths to render (default: brief)")
    p.add_argument("--track", action="append",
                   help="a track id such as AST101-lecture-order (repeatable; default: every track)")
    p.add_argument("--budget", type=int, help="stop after this many billed characters")
    p.add_argument("--spend", action="store_true",
                   help="call ElevenLabs and write the cache; without it, a dry run")
    p.add_argument("--cache-dir", help="write here instead of TTS_CACHE_DIR")
    p.add_argument("--teachme-dir", default=str(TEACHME_DIR), help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.cache_dir:
        os.environ["TTS_CACHE_DIR"] = args.cache_dir

    from tutor.agent import tts
    from tutor.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    if not (cfg["elevenlabs_api_key"] and cfg["elevenlabs_voice_id"]):
        print("Voice is off: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID (in .env) first.",
              file=sys.stderr)
        return 2

    lock = None
    if args.spend:
        lock = _hold_lock(cfg["tts_cache_dir"])
        if lock is None:
            print(f"Another run holds {cfg['tts_cache_dir']} and would render the same clips "
                  "twice; wait until its log stops growing, then run again.", file=sys.stderr)
            return 3

    openers = collect(Path(args.teachme_dir), args.course or (), args.depth, args.track or ())
    todo: dict[Path, Opener] = {}          # one entry per distinct clip
    cached = silent = cut = 0
    for opener in openers:
        path = tts.cache_path(opener.text)
        if path is None:
            silent += 1
            continue
        if path.exists():
            cached += 1
            continue
        if len(tts._clean_for_speech(opener.text)) > tts.MAX_CHARS:
            cut += 1
        todo.setdefault(path, opener)
    chars = sum(len(tts.speech_text(o.text)) for o in todo.values())

    print(f"{len(openers)} openers ({', '.join(args.depth)}): {cached} already cached, "
          f"{len(todo)} to render, {chars:,} characters to bill "
          f"(about {chars // 2:,} to {chars:,} credits at 0.5 to 1 per character).")
    if cut:
        print(f"{cut} of them exceed the {tts.MAX_CHARS}-character quota and are spoken cut short, "
              f"as the app already does.")
    if silent:
        print(f"{silent} have nothing to say once the markup is stripped.")
    print(f"cache: {cfg['tts_cache_dir']}")
    if not args.spend:
        print("Dry run: nothing rendered. Add --spend to render.")
        return 0

    billed = rendered = 0
    for path, opener in todo.items():
        n = len(tts.speech_text(opener.text))
        if args.budget is not None and billed + n > args.budget:
            print(f"Budget of {args.budget:,} characters reached.")
            break
        try:
            tts.synthesize(opener.text, require_cache=True)
        except (httpx.HTTPError, OSError) as exc:
            # Stop at once: every further call bills, and a quota or key
            # problem does not fix itself. A re-run skips what is cached.
            print(f"Stopped at {opener.label}: {exc}", file=sys.stderr)
            print(f"Rendered {rendered} clips, {billed:,} characters billed, before the error.")
            return 1
        rendered += 1
        billed += n
        print(f"  {opener.label}: {n} chars -> {path.name}")
    print(f"Rendered {rendered} clips, {billed:,} characters billed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
