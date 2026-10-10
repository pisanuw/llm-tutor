"""Render content.md slides to PNG image frames using Pillow.

Each slide in content.md becomes one 1920x1080 PNG file named
``slide-NN.png`` in the lecture's ``frames/`` subdirectory.

Fonts required (present on macOS by default):
  - /System/Library/Fonts/Helvetica.ttc        (body / titles)
  - /System/Library/Fonts/SFNSMono.ttf         (code slides)
  - /System/Library/Fonts/Supplemental/Courier New.ttf  (code fallback)
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 1920, 1080

# Colours: a neutral theme, the same two the Reveal decks use (html_renderer)
THEME_PRIMARY = (55,  48, 163)  # deep indigo #3730a3: title slides and title bars
THEME_ACCENT  = (180, 83,   9)  # warm amber #b45309: bullet markers
BG_LIGHT    = (255, 255, 255)
BG_DARK     = (30,  30,  30)   # VS Code dark
TEXT_DARK   = (30,  30,  30)
TEXT_LIGHT  = (212, 212, 212)
TEXT_WHITE  = (255, 255, 255)
TEXT_YELLOW = (220, 220, 110)
BULLET_ACCENT = THEME_ACCENT
DIVIDER     = (200, 200, 200)

# Font paths (macOS)
_FONT_SANS   = "/System/Library/Fonts/Helvetica.ttc"
_FONT_MONO   = "/System/Library/Fonts/SFNSMono.ttf"
_FONT_MONO2  = "/System/Library/Fonts/Supplemental/Courier New.ttf"


def _load_font(path: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    try:
        if path.endswith(".ttc"):
            # index 1 is often bold in .ttc; index 0 is regular
            idx = 1 if bold else 0
            return ImageFont.truetype(path, size, index=idx)
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Slide data (mirrors pptx_writer.ParsedSlide)
# ---------------------------------------------------------------------------

@dataclass
class Slide:
    number: int
    slide_type: str
    title: str
    bullets: list[str] = field(default_factory=list)
    body: str = ""
    code: str = ""


_SLIDE_HEADER_RE = re.compile(r"^## Slide \d+:", re.MULTILINE)


def parse_content_md(content_md: str) -> list[Slide]:
    slides: list[Slide] = []
    blocks = re.split(r"^---\s*$", content_md, flags=re.MULTILINE)
    slide_num = 0

    for block in blocks:
        block = block.strip()
        if not block or not re.search(r"## Slide \d+:", block):
            continue
        slide_num += 1

        def _field(name: str) -> str:
            m = re.search(rf"^{name}:\s*(.+)$", block, re.MULTILINE)
            return m.group(1).strip().strip('"') if m else ""

        def _multiline(name: str) -> str:
            m = re.search(rf"^{name}:\s*\|\s*\n(.*?)(?=^\w|\Z)", block, re.MULTILINE | re.DOTALL)
            return m.group(1).rstrip() if m else ""

        bullets: list[str] = []
        bm = re.search(r"^bullets:\s*\n(.*?)(?=^\w|\Z)", block, re.MULTILINE | re.DOTALL)
        if bm:
            for line in bm.group(1).splitlines():
                stripped = line.strip().lstrip("- ").strip().strip('"')
                if stripped:
                    bullets.append(stripped)

        slides.append(Slide(
            number=slide_num,
            slide_type=_field("type") or "content",
            title=_field("title"),
            bullets=bullets,
            body=_multiline("body"),
            code=_multiline("code"),
        ))

    return slides


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _draw_title_bar(draw: ImageDraw.Draw, title: str, font_bold: ImageFont.FreeTypeFont,
                    bg: tuple, fg: tuple) -> int:
    """Draw a coloured title bar at the top. Returns y offset after the bar."""
    bar_h = 110
    draw.rectangle([0, 0, WIDTH, bar_h], fill=bg)
    draw.text((60, 20), title, font=font_bold, fill=fg)
    return bar_h + 20


def _wrap_and_draw(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont,
                   x: int, y: int, max_width: int, line_spacing: int,
                   fill: tuple) -> int:
    """Word-wrap text and draw it. Returns the y position after the last line."""
    # Estimate chars per line using average char width
    avg_char_w = font.getlength("x")
    chars_per_line = max(20, int(max_width / avg_char_w))
    wrapped = textwrap.fill(text, width=chars_per_line)
    for line in wrapped.splitlines():
        draw.text((x, y), line, font=font, fill=fill)
        y += line_spacing
    return y


# ---------------------------------------------------------------------------
# Slide type renderers
# ---------------------------------------------------------------------------

def _render_title_slide(slide: Slide) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), THEME_PRIMARY)
    draw = ImageDraw.Draw(img)
    f_title = _load_font(_FONT_SANS, 72, bold=True)
    f_sub   = _load_font(_FONT_SANS, 36)
    # Centered title
    draw.text((WIDTH // 2, HEIGHT // 2 - 60), slide.title,
              font=f_title, fill=TEXT_WHITE, anchor="mm")
    subtitle = slide.body or slide.bullets[0] if slide.bullets else ""
    if subtitle:
        draw.text((WIDTH // 2, HEIGHT // 2 + 60), subtitle,
                  font=f_sub, fill=(253, 230, 138), anchor="mm")   # light amber on indigo
    return img


def _render_content_slide(slide: Slide) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_LIGHT)
    draw = ImageDraw.Draw(img)
    f_title  = _load_font(_FONT_SANS, 44, bold=True)
    f_bullet = _load_font(_FONT_SANS, 28)
    f_body   = _load_font(_FONT_SANS, 26)

    y = _draw_title_bar(draw, slide.title, f_title, bg=THEME_PRIMARY, fg=TEXT_WHITE)

    if slide.bullets:
        for bullet in slide.bullets:
            # Bullet dot
            draw.ellipse([60, y + 10, 74, y + 24], fill=BULLET_ACCENT)
            y = _wrap_and_draw(draw, bullet, f_bullet,
                               x=90, y=y, max_width=WIDTH - 120,
                               line_spacing=36, fill=TEXT_DARK)
            y += 8
    elif slide.body:
        y = _wrap_and_draw(draw, slide.body, f_body,
                           x=60, y=y, max_width=WIDTH - 120,
                           line_spacing=38, fill=TEXT_DARK)
    return img


def _render_code_slide(slide: Slide) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
    draw = ImageDraw.Draw(img)
    f_title = _load_font(_FONT_SANS, 38, bold=True)
    f_code  = _load_font(_FONT_MONO if Path(_FONT_MONO).exists() else _FONT_MONO2, 22)

    # Title bar (slightly lighter dark)
    draw.rectangle([0, 0, WIDTH, 80], fill=(50, 50, 60))
    draw.text((40, 18), slide.title, font=f_title, fill=TEXT_YELLOW)

    # Code area
    x, y = 40, 100
    code_text = slide.code or slide.bullets[0] if slide.bullets else "// no code"
    for line in code_text.splitlines():
        draw.text((x, y), line, font=f_code, fill=TEXT_LIGHT)
        y += 30
        if y > HEIGHT - 40:
            draw.text((x, y), "  ...", font=f_code, fill=(100, 100, 100))
            break
    return img


def _render_agenda_slide(slide: Slide) -> Image.Image:
    return _render_content_slide(slide)


def _render_summary_slide(slide: Slide) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), (245, 245, 255))
    draw = ImageDraw.Draw(img)
    f_title  = _load_font(_FONT_SANS, 44, bold=True)
    f_bullet = _load_font(_FONT_SANS, 30)
    y = _draw_title_bar(draw, slide.title or "Summary", f_title,
                        bg=THEME_PRIMARY, fg=TEXT_WHITE)
    for bullet in slide.bullets:
        draw.text((80, y), f"✓  {bullet}", font=f_bullet, fill=(40, 40, 80))
        y += 44
    return img


def _render_diagram_slide(slide: Slide) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    f_title = _load_font(_FONT_SANS, 44, bold=True)
    f_note  = _load_font(_FONT_SANS, 28)
    y = _draw_title_bar(draw, slide.title, f_title, bg=(100, 100, 120), fg=TEXT_WHITE)
    # Placeholder box
    box = [80, y + 20, WIDTH - 80, HEIGHT - 80]
    draw.rectangle(box, outline=(150, 150, 180), width=3)
    note = slide.bullets[0] if slide.bullets else "[DIAGRAM — see slides.pptx]"
    draw.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2),
              note, font=f_note, fill=(100, 100, 150), anchor="mm")
    return img


def _render_quiz_slide(slide: Slide) -> Image.Image:
    return _render_content_slide(slide)


_RENDERERS = {
    "title": _render_title_slide,
    "agenda": _render_agenda_slide,
    "content": _render_content_slide,
    "code": _render_code_slide,
    "summary": _render_summary_slide,
    "diagram-placeholder": _render_diagram_slide,
    "quiz": _render_quiz_slide,
}


def render_slide(slide: Slide) -> Image.Image:
    renderer = _RENDERERS.get(slide.slide_type, _render_content_slide)
    return renderer(slide)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_lecture_frames(lec_dir: Path, force: bool = False) -> list[Path]:
    """Render all slides in a lecture directory to PNG frames.

    Output: ``lec_dir/frames/slide-NN.png`` (1-indexed, zero-padded to 2 digits).
    Returns list of written PNG paths.
    """
    content_path = lec_dir / "content.md"
    if not content_path.exists():
        raise FileNotFoundError(f"content.md not found: {content_path}")

    frames_dir = lec_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    slides = parse_content_md(content_path.read_text())
    written: list[Path] = []

    for slide in slides:
        out_path = frames_dir / f"slide-{slide.number:02d}.png"
        if out_path.exists() and not force:
            written.append(out_path)
            continue
        img = render_slide(slide)
        img.save(out_path, "PNG")
        written.append(out_path)

    return written
