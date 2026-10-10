"""Convert content.md to a standalone Reveal.js HTML presentation.

Each lecture produces one file: output/<course>/lecture-NN/index.html
Replaces the PNG frame approach entirely — smaller files, real text, accessible.

Slide types handled:
  title             — centered title card
  agenda            — bullet list of today's topics
  content           — title + bullets + optional body paragraph
  code              — title + bullets + syntax-highlighted code block
  diagram-placeholder — content layout with a placeholder box
  summary / quiz    — same layout as content
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("pipeline.render")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

@dataclass
class Slide:
    number: int = 0
    heading: str = ""
    type: str = "content"
    title: str = ""
    bullets: list[str] = field(default_factory=list)
    body: str = ""
    code: str = ""
    notes: str = ""


def parse_slides(text: str) -> list[Slide]:
    """Split content.md on --- separators and parse each slide block.

    Slides are numbered by position, 1 to N, once, here (plan step 5.8,
    finding E24): the deck's badges and narration clips, the script's
    ADVANCE markers, and the PNG frames all count that way. A header whose
    own number disagrees ("## Slide 5" in fourth place) is logged, since
    the model skipped or repeated a number.
    """
    raw_blocks = re.split(r"(?m)^---\s*$", text)
    slides: list[Slide] = []
    for block in raw_blocks:
        block = block.strip()
        if not block or "## Slide" not in block:
            continue
        slides.append(_parse_block(block))
    off = [(i, s.number) for i, s in enumerate(slides, 1) if s.number != i]
    if off:
        where, headed = off[0]
        log.warning("content.md: %d slide header(s) out of sequence (slide %d is headed %d); "
                    "numbering by position", len(off), where, headed)
    for i, s in enumerate(slides, 1):
        s.number = i
    return slides


_parse_slides = parse_slides   # the old private name


def _unquote(val: str) -> str:
    """Drop one layer of matching YAML quotes.

    The generator writes `title: "Eclipses"` and `  - "**Umbra** - the full shadow"`.
    The quotes are YAML syntax, not content, but they used to be kept verbatim
    and reached students on the slide (thousands of them across a course's decks).
    """
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        inner = val[1:-1]
        # only when the quote really wrapped the value, not "a" and "b"
        if val[0] not in inner:
            return inner
    return val


def _parse_block(block: str) -> Slide:
    s = Slide()
    lines = block.splitlines()
    i = 0

    # First line: ## Slide N: Title
    if i < len(lines):
        m = re.match(r"^##\s+Slide\s+(\d+):\s*(.*)", lines[i])
        if m:
            s.number = int(m.group(1))
            s.heading = m.group(2).strip()
        i += 1

    # Parse key: value pairs; handle multi-line block scalars (|)
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\w+):\s*(.*)", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()

        if key == "type":
            s.type = _unquote(val)
        elif key == "title":
            s.title = _unquote(val)
        elif key == "notes":
            s.notes = _unquote(val)
        elif key == "bullets":
            i += 1
            while i < len(lines) and lines[i].startswith("  - "):
                s.bullets.append(_unquote(lines[i][4:]))
                i += 1
            continue
        elif key in ("body", "code") and val == "|":
            # block scalar: collect indented lines
            i += 1
            collected = []
            while i < len(lines) and (lines[i].startswith("  ") or lines[i] == ""):
                collected.append(lines[i][2:] if lines[i].startswith("  ") else "")
                i += 1
            blob = "\n".join(collected).strip()
            if key == "body":
                s.body = blob
            else:
                s.code = blob
            continue
        elif key in ("body", "code") and val:
            # Written inline ("body: one sentence") instead of as a block: the
            # prompt asks for "|", but the text is kept either way (E26).
            if key == "body":
                s.body = _unquote(val)
            else:
                s.code = _unquote(val)
        i += 1

    if not s.title:
        s.title = s.heading
    return s


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

# Reveal.js 5.2.1, vendored and served by the app (plan step 6.6, finding E13):
# a deck makes no third-party request, and a CDN outage or a new major version
# cannot change a deck under a student. The files and their hashes are listed
# in tutor/web/static/vendor/VENDOR.md.
_REVEAL = "/static/vendor/reveal"
_REVEAL_CSS = f"{_REVEAL}/dist/reveal.css"
_REVEAL_THEME = f"{_REVEAL}/dist/theme/white.css"
_REVEAL_JS = f"{_REVEAL}/dist/reveal.esm.js"
_HIGHLIGHT_CSS = f"{_REVEAL}/plugin/highlight/monokai.css"
_HIGHLIGHT_JS = f"{_REVEAL}/plugin/highlight/highlight.esm.js"
_NOTES_JS = f"{_REVEAL}/plugin/notes/notes.esm.js"

# A neutral theme: headings and links in the primary, a title slide's
# subtitle in the accent.
_THEME_PRIMARY = "#3730a3"   # deep indigo
_THEME_ACCENT  = "#b45309"   # warm amber

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="{reveal_css}">
<link rel="stylesheet" href="{reveal_theme}">
<link rel="stylesheet" href="{highlight_css}">
<style>
  :root {{
    --r-background-color: #fff;
    --r-heading-color: {primary};
    --r-link-color: {primary};
  }}
  .reveal h1, .reveal h2 {{ color: {primary}; }}
  .reveal .title-slide h1 {{ font-size: 1.8em; }}
  .reveal .title-slide p  {{ color: {accent}; font-size: 1em; }}
  /* tighter base size gives more room on dense slides */
  .reveal {{ font-size: 38px; }}
  .reveal ul {{ text-align: left; }}
  .reveal li {{ margin: 0.15em 0; font-size: 0.9em; }}
  /* allow dense slides to scroll rather than clip */
  .reveal .slides section {{ overflow-y: auto; }}
  /* hide speaker notes from the slide view */
  .reveal .slides section aside {{ display: none !important; }}
  .reveal pre {{ width: 100%; box-shadow: none; }}
  .reveal code {{ font-size: 0.65em; line-height: 1.4; }}
  .reveal pre.diagram {{
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.42em;
    line-height: 1.35;
    text-align: left;
    white-space: pre;
    overflow-x: auto;
    background: #f7f7fa;
    border-left: 4px solid {primary};
    border-radius: 3px;
    padding: 0.6em 0.8em;
    margin: 0.5em 0 0 0;
    box-shadow: none;
  }}
  @media (max-width: 600px) {{
    .reveal pre.diagram {{ font-size: 0.34em; }}
  }}
  /* The deck lays out at 800px tall; a drawing has to share that with the
     bullets above it, so cap it rather than let it push off the slide. */
  .diagram-svg {{ margin: 0.4em auto 0; max-width: 78%; }}
  .diagram-svg svg {{ width: 100%; height: auto; max-height: 300px; }}
  .reveal section:has(li:nth-child(5)) .diagram-svg svg {{ max-height: 210px; }}
  .reveal section:has(li:nth-child(7)) .diagram-svg svg {{ max-height: 150px; }}
  /* bullets and a drawing have to share 800px: tighten the text a notch */
  .reveal section:has(.diagram-svg) li {{ font-size: 0.8em; line-height: 1.25; margin: 0.08em 0; }}
  .reveal section:has(.diagram-svg) h2 {{ font-size: 1.25em; margin-bottom: 0.25em; }}
  .placeholder-box {{
    border: 2px dashed #aaa;
    padding: 2em;
    margin: 1em auto;
    color: #888;
    font-style: italic;
    text-align: center;
    max-width: 80%;
  }}
  .slide-num {{
    position: absolute; bottom: 0.5em; right: 1em;
    font-size: 0.45em; color: #bbb;
  }}
  .deck-narrate {{
    position: fixed; bottom: 12px; left: 12px; z-index: 60;
    background: {primary}; color: #fff; border: none; border-radius: 6px;
    padding: 8px 14px; font-size: 15px; cursor: pointer; opacity: 0.9;
    font-family: system-ui, -apple-system, sans-serif;
  }}
  .deck-narrate:hover {{ opacity: 1; }}
</style>
</head>
<body>
<div class="reveal">
  <div class="slides">
{sections}
  </div>
</div>
{audio_ui}
<script type="module">
  import Reveal from '{reveal_js}';
  import Highlight from '{highlight_js}';
  import Notes from '{notes_js}';
  Reveal.initialize({{
    // Each deck lives in its own iframe (the /chat slide panel) or its own tab,
    // so it should always run full-page to fill its viewport. The old
    // `window.self !== window.top` test flipped to embedded mode inside the
    // iframe, which drops Reveal's `reveal-full-page` sizing and renders blank.
    embedded: false,
    hash: true,
    height: 800,
    slideNumber: 'c/t',
    plugins: [Highlight, Notes],
    highlight: {{ highlightOnLoad: true }},
  }}).then(() => {{
    // Self-heal sizing. The /chat slide panel sizes this iframe only AFTER
    // Reveal's one-shot init runs, because its Tailwind CDN runtime applies the
    // flex layout late. Reveal would then stay scaled to a 0/last-known height
    // and render blank ("black panel"). A ResizeObserver re-fits the deck on
    // every viewport size change, so it lays out correctly whenever the
    // container finally gets real dimensions — no parent cooperation needed.
    window.deck = Reveal;
    const refit = () => Reveal.layout();
    try {{ new ResizeObserver(refit).observe(document.documentElement); }}
    catch (e) {{ window.addEventListener('resize', refit); }}
    refit();
{audio_init}
  }});
</script>
</body>
</html>
"""


# Injected only when per-slide narration MP3s exist for a lecture. The player is
# self-contained (no third-party Reveal plugin): a user click starts slide 1's
# clip (satisfying the browser autoplay-with-sound gesture requirement), and each
# clip's `ended` event advances to the next slide, which plays its own clip.
_AUDIO_UI = (
    '<audio id="deck-narration" preload="none"></audio>\n'
    '<button id="deck-narrate-btn" class="deck-narrate" type="button">🔊 Play narration</button>'
)

_AUDIO_INIT = """
    (function () {
      const audio = document.getElementById('deck-narration');
      const btn = document.getElementById('deck-narrate-btn');
      if (!audio || !btn) return;
      let auto = false;
      const srcFor = () => {
        const s = Reveal.getCurrentSlide();
        return s ? s.getAttribute('data-audio-src') : null;
      };
      const playCurrent = () => {
        const src = srcFor();
        if (!src) return;
        if (audio.getAttribute('src') !== src) audio.setAttribute('src', src);
        audio.play().catch(() => {});
      };
      const setLabel = () => {
        btn.textContent = !auto ? '🔊 Play narration'
          : (audio.paused ? '▶ Resume narration' : '⏸ Pause narration');
      };
      btn.addEventListener('click', () => {
        if (!auto) { auto = true; playCurrent(); }
        else if (audio.paused) { audio.play().catch(() => {}); }
        else { audio.pause(); }
        setLabel();
      });
      audio.addEventListener('play', setLabel);
      audio.addEventListener('pause', setLabel);
      audio.addEventListener('ended', () => {
        if (!auto) return;
        if (Reveal.isLastSlide()) { auto = false; setLabel(); return; }
        Reveal.next();
      });
      Reveal.on('slidechanged', () => { if (auto) playCurrent(); setLabel(); });
      setLabel();
    })();"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md(s: str) -> str:
    """Escape, then convert the inline markdown the generator writes.

    Bullets arrive as `**Umbra** - the full shadow`; the asterisks used to reach
    the slide. Order matters: escape first, so a `<` in the text stays text,
    then introduce the only tags we allow (strong, em, code).
    """
    out = _esc(s)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    # non-greedy, and the span may contain a lone * ("**L* is the star's luminosity**")
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    return out


def _diagram_svg(lec_dir: Path, number: int) -> str:
    """A hand-drawn diagram for this slide, if one was authored.

    `diagrams/slide-NN.svg` beside content.md is inlined in place of the body.
    Most slides need no drawing: their body is already ASCII layout or a
    comparison table that reads fine as text. The few that say "draw the Earth,
    Moon and Sun at first quarter" cannot be rendered from the instruction, so the drawing lives here,
    outside content.md, and survives a regeneration of the content.
    """
    f = lec_dir / "diagrams" / f"slide-{number:02d}.svg"
    if not f.exists():
        return ""
    return f'      <div class="diagram-svg">{f.read_text().strip()}</div>'


def _body_html(body: str) -> str:
    """The body block, with its whitespace intact.

    Most bodies are ASCII layout -- aligned tables, step-by-step traces,
    two-column comparisons. They used to go into a <p>, where HTML collapses
    every run of spaces and the alignment is lost, or (on diagram, code and
    title slides) to be dropped entirely. A <pre> keeps them as written.
    """
    body = body.strip()
    if not body:
        return ""
    # A single line with no column alignment is prose, not a drawing.
    if "\n" not in body and not re.search(r"  +\S", body):
        return f"      <p>{_md(body)}</p>"
    return f'      <pre class="diagram">{_esc(body)}</pre>'


# "[DIAGRAM: the Moon's orbit seen from above]", "[TABLE: Phase | Moonrise | ...]":
# instructions the generator wrote to itself about a figure to draw. They are
# not content, and some of them were reaching students as bullet points.
_INSTRUCTION_BULLET = re.compile(r"^\[(?:DIAGRAM|TABLE|LABEL|CHART|FIGURE)\b", re.I)


def _bullets_html(bullets: list[str]) -> str:
    bullets = [b for b in bullets if not _INSTRUCTION_BULLET.match(b.strip())]
    if not bullets:
        return ""
    items = "\n".join(f"        <li>{_md(b)}</li>" for b in bullets)
    return f"      <ul>\n{items}\n      </ul>"


# highlight.js class names for the usual spellings of ``code_language``.
_HIGHLIGHT_NAMES = {
    "c++": "cpp", "cpp": "cpp", "c#": "csharp", "csharp": "csharp", "c": "c",
    "python": "python", "java": "java", "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript", "go": "go", "rust": "rust",
    "ruby": "ruby", "r": "r", "sql": "sql", "matlab": "matlab", "kotlin": "kotlin",
    "swift": "swift", "scala": "scala", "haskell": "haskell", "php": "php",
}


_SHELL_COMMAND = re.compile(r"(?m)^\s*(?:g\+\+|gcc|clang\+\+|clang|make|cmake|javac|python3?|pip|npm|cargo|go)\s")


def _highlight_language(code: str) -> str:
    """The highlight.js class for a code slide: a shell transcript (a ``$``
    prompt, or a ``#`` comment over a compiler or build command) as bash,
    otherwise the course's ``code_language`` from courses.yaml, or plain text
    when it names none."""
    head = code.lstrip()
    if head.startswith("$ ") or (head.startswith("#") and _SHELL_COMMAND.search(code)):
        return "bash"
    from tutor.courses import IDENTITY

    name = IDENTITY.code_language.strip().lower()
    if not name:
        return "plaintext"
    return _HIGHLIGHT_NAMES.get(name, re.sub(r"[^a-z0-9]+", "", name) or "plaintext")


def _slide_html(s: Slide, audio: bool = False, audio_base: str = "audio",
                lec_dir: Path | None = None) -> str:
    num_tag = f'<span class="slide-num">{s.number}</span>'
    # When per-slide narration audio exists, tag the section so the sync player
    # can find its clip. Harmless when the file is absent (play() no-ops).
    # audio_base is an absolute app URL in production (/lecture-audio/<course>/
    # lecture-NN/audio) so the clips can be served off the persistent disk,
    # decoupled from the ephemeral deck; defaults to a relative path.
    a = f' data-audio-src="{audio_base}/slide-{s.number:02d}.mp3"' if audio else ""
    svg = _diagram_svg(lec_dir, s.number) if lec_dir else ""
    figure = svg or _body_html(s.body)

    if s.type == "title":
        sub = "<br>".join(_esc(b) for b in s.bullets) if s.bullets else ""
        body = f"""\
    <section class="title-slide"{a}>
      <h1>{_esc(s.title)}</h1>
      {"<p>" + sub + "</p>" if sub else ""}
      {figure}
      {num_tag}
      <aside class="notes">{_esc(s.notes)}</aside>
    </section>"""
        return body

    if s.type == "code":
        lang = _highlight_language(s.code)
        code_block = (
            f'      <pre><code class="language-{lang}" data-trim data-noescape>'
            f"{_esc(s.code)}</code></pre>"
            if s.code else ""
        )
        return f"""\
    <section{a}>
      <h2>{_esc(s.title)}</h2>
      {_bullets_html(s.bullets)}
      {code_block}
      {figure}
      {num_tag}
      <aside class="notes">{_esc(s.notes)}</aside>
    </section>"""

    if s.type == "diagram-placeholder":
        # The body is the diagram: most such slides carry ASCII art or
        # a comparison table that the old placeholder box discarded in favour
        # of the slide's own title. An empty body leaves the bullets to stand
        # on their own rather than an empty dashed box.
        return f"""\
    <section{a}>
      <h2>{_esc(s.title)}</h2>
      {_bullets_html(s.bullets)}
      {figure}
      {num_tag}
      <aside class="notes">{_esc(s.notes)}</aside>
    </section>"""

    # content / agenda / summary / quiz
    body_p = figure
    return f"""\
    <section{a}>
      <h2>{_esc(s.title)}</h2>
      {_bullets_html(s.bullets)}
      {body_p}
      {num_tag}
      <aside class="notes">{_esc(s.notes)}</aside>
    </section>"""


def render_lecture(content_md: Path, out_path: Path, lecture_title: str = "") -> int:
    """Convert content.md -> index.html. Returns slide count.

    If ``audio/slide-01.mp3`` exists beside the output (produced by
    ``render-audio``), the deck embeds a self-contained narration player that
    plays each slide's clip and auto-advances when it ends.
    """
    text = content_md.read_text(errors="ignore")
    slides = parse_slides(text)
    if not slides:
        return 0
    has_audio = (out_path.parent / "audio" / "slide-01.mp3").exists()
    # Absolute URL so prod serves clips off the persistent disk via the
    # /lecture-audio mount (see tutor/main.py), independent of the ephemeral deck.
    # Path shape: output/<course>/lecture-NN/index.html -> /lecture-audio/<course>/lecture-NN/audio
    course = out_path.parent.parent.name
    lecture = out_path.parent.name
    audio_base = f"/lecture-audio/{course}/{lecture}/audio"
    sections = "\n".join(
        _slide_html(s, audio=has_audio, audio_base=audio_base, lec_dir=out_path.parent)
        for s in slides
    )
    title = lecture_title or (slides[0].title if slides else "Lecture")
    html = _TEMPLATE.format(
        title=_esc(title),
        sections=sections,
        reveal_css=_REVEAL_CSS,
        reveal_theme=_REVEAL_THEME,
        reveal_js=_REVEAL_JS,
        highlight_css=_HIGHLIGHT_CSS,
        highlight_js=_HIGHLIGHT_JS,
        notes_js=_NOTES_JS,
        primary=_THEME_PRIMARY,
        accent=_THEME_ACCENT,
        audio_ui=_AUDIO_UI if has_audio else "",
        audio_init=_AUDIO_INIT if has_audio else "",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    return len(slides)


def render_course(output_dir: Path, course_name: str = "", verbose: bool = True) -> dict[int, int]:
    """Render all lectures in output_dir that have a content.md."""
    results: dict[int, int] = {}
    for lec_dir in sorted(output_dir.glob("lecture-*")):
        content = lec_dir / "content.md"
        if not content.exists():
            continue
        m = re.search(r"lecture-(\d+)", lec_dir.name)
        num = int(m.group(1)) if m else 0
        count = render_lecture(content, lec_dir / "index.html",
                               f"{course_name} Lecture {num:02d}")
        results[num] = count
        if verbose:
            print(f"  L{num:02d}: {count} slides -> {lec_dir / 'index.html'}")
    return results
