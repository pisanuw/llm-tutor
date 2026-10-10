"""Narrate the course book, one MP3 per chapter, with ElevenLabs.

Input is the Quarto source under ``book/``: the chapter list in ``_quarto.yml``
(chapters, parts, appendices) and each chapter's ``.qmd``. Only prose is read:
code fences, tables, images, quizzes, and Quarto's div fences are dropped,
headings are read as sentences, and links, emphasis, and inline code are
unwrapped. Each chapter's prose is chunked under ElevenLabs' per-request
ceiling, synthesized through the lecture renderer's cached chunk call, and
concatenated to ``<out>/<slug>.mp3``, where ``<slug>`` is the chapter file's
stem (``ast101-01-the-sky``).

The clips are for signed-in students only: ``book/narration.js`` loads
``/lecture-audio/book/<slug>.mp3`` from the session-gated mount while the book
pages stay public. So ``<out>`` is ``<lecture audio dir>/book``: ``output/book``
locally, ``lecture-audio/book`` on the production disk (DEPLOY.md).

A dry run costs nothing and reports the characters per chapter; ``spend``
bills, chapter by chapter, and stops before a chapter that would pass the
budget. A chapter whose MP3 exists is skipped, so a stopped run resumes.
After a spend, ``book/narration-index.json`` lists the chapters that have a
clip; the page script reads it, so it ships with the clips.
Requires ffmpeg + ffprobe on PATH for the concatenation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from pipeline.render.audio_renderer import (
    _DEFAULT_MODEL, _chunk_text, _concat_mp3, _ffprobe_seconds, _synthesize_chunk,
)

_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
_CODE_FENCE = re.compile(r"^(`{3,}|~{3,}).*?^\1[^\n]*$", re.S | re.M)
_QUIZ_OPEN = re.compile(r"<div\b[^>]*\bclass=\"[^\"]*\bquiz\b[^\"]*\"[^>]*>", re.I)
_DIV_TAG = re.compile(r"<(/?)div\b[^>]*>", re.I)
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)(?:\s*\{[^}]*\})?\s*$")


def chapter_slugs(quarto_yml: Path) -> list[str]:
    """The chapters in reading order, as file stems: ``book.chapters`` (plain
    entries and parts) then ``book.appendices``."""
    book = yaml.safe_load(quarto_yml.read_text())["book"]
    slugs: list[str] = []

    def walk(entries) -> None:
        for entry in entries or []:
            if isinstance(entry, str):
                slugs.append(Path(entry).stem)
            elif isinstance(entry, dict):
                walk(entry.get("chapters"))

    walk(book.get("chapters"))
    walk(book.get("appendices"))
    return slugs


def _inline(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)              # images
    text = re.sub(r"\[\^[^\]]+\]", "", text)                       # footnote marks
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)           # links -> text
    text = re.sub(r"<[^>]+>", "", text)                            # stray html tags
    text = re.sub(r"`([^`]+)`", r"\1", text)                       # inline code
    text = re.sub(r"\$([^$]+)\$", r"\1", text)                     # inline math
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)                 # bold
    text = re.sub(r"(?<!\w)[*_](?=\S)(.+?)(?<=\S)[*_](?!\w)", r"\1", text)  # emphasis
    text = re.sub(r"\s*\{[#.][^}]*\}", "", text)                   # {#id} / {.class}
    return text.strip()


def _drop_quizzes(body: str) -> str:
    """Remove each ``<div class="quiz">`` with everything inside it: the
    question, the options, and the explanation nest their own divs, so the
    block ends at the close tag that balances its open tag, not the first."""
    out: list[str] = []
    pos = 0
    while True:
        start = _QUIZ_OPEN.search(body, pos)
        if not start:
            out.append(body[pos:])
            return "".join(out)
        out.append(body[pos:start.start()])
        depth, end = 1, start.end()
        for tag in _DIV_TAG.finditer(body, start.end()):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                end = tag.end()
                break
        else:
            end = len(body)
        pos = end


def narration_text(qmd: str) -> str:
    """The spoken prose of one chapter: paragraphs separated by blank lines."""
    body = _FRONT_MATTER.sub("", qmd)
    body = _CODE_FENCE.sub("", body)
    body = _drop_quizzes(body)
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(":::") or line.startswith("|") or _FOOTNOTE_DEF.match(line):
            flush()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            title = _inline(heading.group(1))
            if title:
                paragraphs.append(title if title[-1] in ".!?:" else title + ".")
            continue
        line = re.sub(r"^>\s?", "", line)                          # blockquote
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)          # list marker
        spoken = _inline(line)
        if spoken:
            current.append(spoken)
    flush()
    return "\n\n".join(p for p in paragraphs if p)


def chapters(book_dir: Path, only: list[str] | None = None) -> list[dict]:
    """``[{slug, chars, text}]`` for every chapter (or ``only`` those slugs)."""
    out = []
    for slug in chapter_slugs(book_dir / "_quarto.yml"):
        if only and slug not in only:
            continue
        path = book_dir / f"{slug}.qmd"
        if not path.exists():
            continue
        text = narration_text(path.read_text())
        out.append({"slug": slug, "chars": len(text), "text": text})
    return out


def render_book_audio(
    book_dir: Path,
    out_dir: Path,
    *,
    api_key: str,
    voice_id: str,
    cache_dir: Path,
    only: list[str] | None = None,
    spend: bool = False,
    budget: int | None = None,
    model: str | None = None,
    verbose: bool = True,
) -> dict:
    """Narrate the book into ``out_dir/<slug>.mp3``. Without ``spend`` nothing is
    synthesized. Returns ``{chapters, rendered, skipped, billed_chars, stopped}``:
    ``chapters`` carries each chapter's slug, chars, and whether its clip exists.
    """
    model = model or _DEFAULT_MODEL
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = chapters(book_dir, only)
    for row in rows:                      # every row says whether its clip exists,
        row["exists"] = (out_dir / f"{row['slug']}.mp3").exists()   # even past a budget stop
    rendered = skipped = billed = 0
    stopped = ""
    if not spend:
        return {"chapters": rows, "rendered": 0, "skipped": 0, "billed_chars": 0,
                "stopped": "", "index": []}
    for row in rows:
        target = out_dir / f"{row['slug']}.mp3"
        if row["exists"] or not row["text"]:
            skipped += 1
            continue
        if budget is not None and billed + row["chars"] > budget:
            stopped = f"budget of {budget:,} characters reached before {row['slug']}"
            break
        parts: list[Path] = []
        for i, chunk in enumerate(_chunk_text(row["text"])):
            audio = _synthesize_chunk(chunk, api_key=api_key, voice_id=voice_id, model=model,
                                      cache_dir=cache_dir)
            part = out_dir / f".{row['slug']}-{i}.mp3"
            part.write_bytes(audio)
            parts.append(part)
        _concat_mp3(parts, target)
        for part in parts:
            part.unlink(missing_ok=True)
        row["exists"] = True
        row["seconds"] = _ffprobe_seconds(target)
        rendered += 1
        billed += row["chars"]
        if verbose:
            print(f"  {row['slug']}: {row['chars']:,} chars -> {target.name} ({row['seconds'] / 60:.1f} min)")
    if stopped and verbose:
        print(f"  {stopped}")
    index = write_index(book_dir, out_dir) if spend else []
    return {"chapters": rows, "rendered": rendered, "skipped": skipped,
            "billed_chars": billed, "stopped": stopped, "index": index}


def write_index(book_dir: Path, out_dir: Path) -> list[str]:
    """Record which chapters have a clip in ``book/narration-index.json``, a
    public file the book build ships beside the pages. ``narration.js`` reads
    it before probing the gated clip, so a chapter without narration shows
    nothing, to a signed-in reader or not (the gate answers 401 before it
    looks for the file, so the probe alone cannot tell). Commit it with the
    clips."""
    index = sorted(p.stem for p in out_dir.glob("*.mp3"))
    (book_dir / "narration-index.json").write_text(json.dumps(index, indent=0) + "\n")
    return index
