"""Goal-to-content anchor tool (Session 13).

Two commands:

  python -m pipeline propose-anchors
      For every learning goal, propose a slide anchor (lecture + slide index),
      a book anchor (Quarto chapter + section id), and a one-line "why this
      matters" hook (via claude haiku). Writes
      curriculum/goal_anchors_proposed.json.

  python -m pipeline apply-anchors [--dry-run]
      Applies the reviewed anchors to learning_goals.json in place.

Slide and book anchors are computed deterministically (word-overlap scoring)
then written to the proposed file for the instructor to review. The LLM is
only called for the hook field.

Book anchors need to know which book chapter goes with which lecture: the
optional ``curriculum/book-chapters.json`` says, per course, lecture number to
chapter (the chapter's .qmd stem, which Quarto renders to
``book/_book/<chapter>.html``)::

    {"AST101": {"1": "ast101-01-the-sky", "2": "ast101-02-moon-phases"},
     "AST102": {"1": "ast102-01-measuring-stars"}}

Without the file, or without an entry for a lecture, that goal gets no book
anchor, and the coverage gate counts slide anchors alone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from tutor.courses import IDENTITY
from tutor.paths import CURRICULUM_DIR

ROOT = Path(__file__).parent.parent
GOALS_PATH = CURRICULUM_DIR / "learning_goals.json"
ANCHORS_PATH = CURRICULUM_DIR / "goal_anchors_proposed.json"
BOOK_CHAPTERS_PATH = CURRICULUM_DIR / "book-chapters.json"
BOOK_DIR = ROOT / "book" / "_book"

console = Console()


# ---------------------------------------------------------------------------
# Lecture → book chapter mapping
# ---------------------------------------------------------------------------

def load_book_chapters(path: Path | None = None) -> dict[str, dict[int, str]]:
    """``{course: {lecture: chapter}}`` from ``curriculum/book-chapters.json``;
    empty when the file is missing. Lecture keys may be strings ("1") or
    numbers; a key or a chapter that is not usable is skipped."""
    path = path or BOOK_CHAPTERS_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[yellow]{escape(str(path))} is not readable JSON ({escape(str(exc))}); "
                      "no book anchors[/yellow]")
        return {}
    out: dict[str, dict[int, str]] = {}
    for course, table in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(table, dict):
            continue
        for lecture, chapter in table.items():
            try:
                num = int(lecture)
            except (TypeError, ValueError):
                continue
            if isinstance(chapter, str) and chapter.strip():
                out.setdefault(str(course), {})[num] = chapter.strip()
    return out


def _chapter_for(course: str, lecture: int,
                 chapters: dict[str, dict[int, str]] | None = None) -> str | None:
    """The book chapter for a lecture of a course, or None (no book anchor)."""
    table = (load_book_chapters() if chapters is None else chapters).get(course, {})
    return table.get(lecture)


# ---------------------------------------------------------------------------
# Slide parsing
# ---------------------------------------------------------------------------

def _parse_slides(course: str, lecture: int) -> list[dict]:
    """Return [{index, title, body}] for content.md of a lecture (1-indexed)."""
    lec_dir = ROOT / "output" / course / f"lecture-{lecture:02d}"
    md = lec_dir / "content.md"
    if not md.exists():
        return []
    text = md.read_text()
    slides = []
    for m in re.finditer(
        r"^## Slide (\d+): (.+?)$\n(.*?)(?=^## Slide |\Z)",
        text, re.MULTILINE | re.DOTALL,
    ):
        body = m.group(3)
        # collect title + bullet lines
        lines = [m.group(2)]
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                lines.append(stripped[2:])
        slides.append({
            "index": int(m.group(1)),
            "title": m.group(2).strip(),
            "text": " ".join(lines),
        })
    return slides


# ---------------------------------------------------------------------------
# Book section parsing
# ---------------------------------------------------------------------------

def _parse_book_sections(chapter: str) -> list[dict]:
    """Return [{section_id, title}] for a book chapter HTML file."""
    html_path = BOOK_DIR / f"{chapter}.html"
    if not html_path.exists():
        return []
    html = html_path.read_text()
    sections = []
    for m in re.finditer(
        r'<h[2-4][^>]*\bid="([^"]+)"[^>]*>(.*?)</h[2-4]>',
        html, re.DOTALL,
    ):
        sid = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if sid in ("toc-title",) or not title:
            continue
        sections.append({"section_id": sid, "title": title})
    return sections


# ---------------------------------------------------------------------------
# Keyword overlap scoring
# ---------------------------------------------------------------------------

_STOP = frozenset(
    "a an the and or of in on at to for is are was be been being "
    "that this with it from by its we you can will they their how "
    "when what why which who where".split()
)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z0-9_+#*/-]{2,}", text)} - _STOP


def _score(goal_tokens: set[str], candidate: str) -> int:
    return len(goal_tokens & _tokens(candidate))


def _best_slide(goal_tokens: set[str], slides: list[dict]) -> dict | None:
    if not slides:
        return None
    scored = [(s, _score(goal_tokens, s["text"])) for s in slides]
    best = max(scored, key=lambda x: x[1])
    return best[0]


def _best_section(goal_tokens: set[str], sections: list[dict]) -> dict | None:
    if not sections:
        return None
    scored = [(s, _score(goal_tokens, s["title"])) for s in sections]
    best = max(scored, key=lambda x: x[1])
    return best[0]


# ---------------------------------------------------------------------------
# LLM hook pass
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, timeout: int = 300) -> str | None:
    """Haiku's reply to ``prompt``, or None on a failure (printed). Through the
    pipeline's one hardened CLI helper (plan step 6.8, finding E14): no tools,
    no API key in its environment. The prompt is built from LLM-written JSON,
    which is why tools stay off."""
    from pipeline.generate.script_generator import PLAIN_SYSTEM, _generate_via_cli

    try:
        return _generate_via_cli(PLAIN_SYSTEM, prompt, model="haiku", timeout=timeout).strip()
    except FileNotFoundError:
        console.print("[red]`claude` CLI not found: is it installed?[/red]")
        return None
    except (RuntimeError, OSError) as exc:
        console.print(f"[red]claude call failed: {escape(str(exc))}[/red]")
        return None


def _propose_hooks(goals: list[dict]) -> dict[str, str]:
    """Return {goal_id: hook} for all goals in one haiku call."""
    goal_list = "\n".join(
        f'  {g["id"]}: "{g["title"]}" — {g.get("description", "")}'
        for g in goals
    )
    prompt = (
        f"For each {IDENTITY.subject} learning goal below, write a one-sentence hook "
        "(≤12 words) explaining why a student should care about this skill. "
        "Be concrete and direct — no generic 'it is important' statements.\n\n"
        f"Goals:\n{goal_list}\n\n"
        "Return ONLY valid JSON:\n"
        '{"hooks": {"goal-id": "hook sentence", ...}}'
    )
    console.print("[dim]  Calling claude haiku for hooks...[/dim]")
    raw = _call_claude(prompt, timeout=300)
    if raw is None:
        return {}
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return {k: str(v)[:120] for k, v in data.get("hooks", {}).items()}
    except json.JSONDecodeError:
        console.print("[yellow]could not parse hooks JSON[/yellow]")
        return {}


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------

@click.command("propose-anchors")
@click.option("--goals", "goals_path", default=str(GOALS_PATH), show_default=True)
@click.option("--output", "output_path", default=str(ANCHORS_PATH), show_default=True)
@click.option("--no-hooks", is_flag=True, default=False,
              help="Skip the LLM hook pass (faster, no claude call)")
def propose_anchors_cmd(goals_path: str, output_path: str, no_hooks: bool):
    """Propose slide, book, and hook anchors for every learning goal."""
    gpath = Path(goals_path)
    opath = Path(output_path)

    data = json.loads(gpath.read_text())
    goals = data["goals"]

    console.print(f"\n[bold]Proposing anchors for {len(goals)} goals...[/bold]\n")

    # Pre-load all slide and section data
    slide_cache: dict[tuple, list] = {}
    section_cache: dict[str, list] = {}
    book_chapters = load_book_chapters()

    proposed: list[dict] = []
    missing_slide = 0
    missing_book = 0

    for g in goals:
        course = g["course"]
        lecture = int(g["lecture"])
        gid = g["id"]
        tokens = _tokens(g["title"] + " " + g.get("description", ""))

        # Slide anchor
        key = (course, lecture)
        if key not in slide_cache:
            slide_cache[key] = _parse_slides(course, lecture)
        slides = slide_cache[key]
        best_slide = _best_slide(tokens, slides)
        if best_slide:
            slide_anchor = {
                "lecture": lecture,
                "slide": best_slide["index"],
                "title": best_slide["title"],
            }
        else:
            slide_anchor = None
            missing_slide += 1

        # Book anchor
        chapter = _chapter_for(course, lecture, book_chapters)
        if chapter:
            if chapter not in section_cache:
                section_cache[chapter] = _parse_book_sections(chapter)
            sections = section_cache[chapter]
            best_sec = _best_section(tokens, sections)
            if best_sec:
                book_anchor = {
                    "chapter": chapter,
                    "section_id": best_sec["section_id"],
                    "title": best_sec["title"],
                }
            else:
                book_anchor = None
                missing_book += 1
        else:
            book_anchor = None
            missing_book += 1

        proposed.append({
            "id": gid,
            "slide_anchor": slide_anchor,
            "book_anchor": book_anchor,
            "hook": None,
        })

    console.print(f"  Slide anchors: {len(proposed) - missing_slide}/{len(proposed)}")
    console.print(f"  Book anchors:  {len(proposed) - missing_book}/{len(proposed)}")

    # Hook pass
    if not no_hooks:
        hooks = _propose_hooks(goals)
        console.print(f"  Hooks:         {len(hooks)}/{len(goals)}")
        for p in proposed:
            p["hook"] = hooks.get(p["id"])
    else:
        console.print("  Hooks:         skipped (--no-hooks)")

    out = {
        "note": (
            "Proposed anchors from pipeline/anchor_goals.py. "
            "Review, edit, then run: python -m pipeline apply-anchors"
        ),
        "anchors": proposed,
    }
    opath.write_text(json.dumps(out, indent=2))
    console.print(f"\nWritten to [bold]{opath}[/bold]")

    # Coverage check: the book counts only when a lecture-to-chapter map exists.
    with_slide = sum(1 for p in proposed if p["slide_anchor"])
    with_book = sum(1 for p in proposed if p["book_anchor"])
    pct_slide = with_slide / len(proposed) * 100 if proposed else 0.0
    pct_book = with_book / len(proposed) * 100 if proposed else 0.0
    gate = pct_slide >= 90 and (pct_book >= 90 or not book_chapters)
    status = "[green]PASS[/green]" if gate else "[red]FAIL[/red]"
    if book_chapters:
        console.print(
            f"\nExit gate (>=90% with slide + book anchor): {status} "
            f"(slide {pct_slide:.0f}%, book {pct_book:.0f}%)\n"
        )
    else:
        console.print(
            f"\nExit gate (>=90% with a slide anchor; no {BOOK_CHAPTERS_PATH.name}, so no book): "
            f"{status} (slide {pct_slide:.0f}%)\n"
        )


@click.command("apply-anchors")
@click.option("--anchors", "anchors_path", default=str(ANCHORS_PATH), show_default=True)
@click.option("--goals", "goals_path", default=str(GOALS_PATH), show_default=True)
@click.option("--dry-run", is_flag=True, default=False)
def apply_anchors_cmd(anchors_path: str, goals_path: str, dry_run: bool):
    """Apply reviewed anchors to learning_goals.json."""
    apath = Path(anchors_path)
    gpath = Path(goals_path)

    if not apath.exists():
        console.print(f"[red]Anchors file not found: {apath}[/red]")
        raise SystemExit(1)

    data = json.loads(gpath.read_text())
    anchors_data = json.loads(apath.read_text())
    anchor_map = {a["id"]: a for a in anchors_data["anchors"]}

    updated = 0
    for g in data["goals"]:
        a = anchor_map.get(g["id"])
        if a is None:
            continue
        if a.get("slide_anchor") is not None:
            g["slide_anchor"] = a["slide_anchor"]
        if a.get("book_anchor") is not None:
            g["book_anchor"] = a["book_anchor"]
        if a.get("hook") is not None:
            g["hook"] = a["hook"]
        updated += 1

    if dry_run:
        sample = next((g for g in data["goals"] if g.get("slide_anchor")), None)
        console.print(f"Dry run: would update {updated} goals. Sample:")
        if sample:
            console.print(f"  {sample['id']}")
            console.print(f"    slide_anchor: {sample.get('slide_anchor')}")
            console.print(f"    book_anchor:  {sample.get('book_anchor')}")
            console.print(f"    hook:         {sample.get('hook')}")
        return

    gpath.write_text(json.dumps(data, indent=2))
    console.print(f"[green]Applied anchors to {updated} goals.[/green]")

    # Final coverage report (the book counts only when a chapter map exists)
    with_slide = sum(1 for g in data["goals"] if g.get("slide_anchor"))
    with_book = sum(1 for g in data["goals"] if g.get("book_anchor"))
    n = len(data["goals"]) or 1
    gate = with_slide / n >= 0.9 and (with_book / n >= 0.9 or not load_book_chapters())
    status = "[green]PASS[/green]" if gate else "[red]FAIL[/red]"
    console.print(
        f"Exit gate: {status} "
        f"(slide {with_slide}/{n} = {with_slide/n*100:.0f}%, "
        f"book {with_book}/{n} = {with_book/n*100:.0f}%)"
    )
