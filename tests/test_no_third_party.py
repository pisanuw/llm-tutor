"""Plan step 6.6 (finding E13): no page makes a third-party request.

Every asset a page loads (a script, stylesheet, image, font, frame, or media
file; a module import; a CSS import or url()) is served by the app. The
templates, the page scripts and stylesheets, the vendored stylesheets, the
explorables, and the slide deck the renderer writes are read for an absolute
URL in a loading position; the deck is also opened in Chromium, which records
every request it makes. Links a student clicks are not loads and may point
anywhere.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "tutor" / "web"
STATIC = WEB / "static"
REVEAL = STATIC / "vendor" / "reveal"
DECK_SOURCE = ROOT / "tests" / "golden" / "deck" / "content.md"


def rendered_deck(tmp: Path) -> Path:
    """A deck as the renderer writes it now (every slide type), not a stored copy."""
    import shutil

    from pipeline.render.html_renderer import render_lecture

    lec = tmp / "AST101" / "lecture-04"
    lec.mkdir(parents=True, exist_ok=True)
    shutil.copy(DECK_SOURCE, lec / "content.md")
    render_lecture(lec / "content.md", lec / "index.html", "AST 101 Lecture 04")
    return lec / "index.html"

_HTML_LOADS = (
    re.compile(r"<(?:script|img|iframe|source|audio|video|embed|track|input)\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)", re.I),
    re.compile(r"<link\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)", re.I),
    re.compile(r"<object\b[^>]*?\bdata\s*=\s*[\"']([^\"']+)", re.I),
)
_JS_LOADS = (
    re.compile(r"^\s*import\s+(?:[\w*{}\s,]+\s+from\s+)?[\"']([^\"']+)[\"']", re.M),
    re.compile(r"\bimport\(\s*[\"']([^\"']+)[\"']\s*\)"),
    re.compile(r"\bfetch\(\s*[\"'`]([^\"'`]+)"),
    re.compile(r"\bnew\s+(?:Worker|EventSource|WebSocket)\(\s*[\"'`]([^\"'`]+)"),
)
_CSS_LOADS = (
    re.compile(r"@import\s+(?:url\()?\s*[\"']?([^\"')\s;]+)", re.I),
    re.compile(r"\burl\(\s*[\"']?([^\"')]+)", re.I),
)
_THIRD_PARTY = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//", re.I)


def _loads(text: str, kinds) -> list[str]:
    return [u for rx in kinds for u in rx.findall(text)]


def third_party(path: Path) -> list[str]:
    """The absolute URLs ``path`` would make a browser fetch."""
    text = path.read_text(errors="replace")
    if path.suffix == ".css":
        found = _loads(text, _CSS_LOADS)
    elif path.suffix in (".js", ".mjs"):
        found = _loads(text, _JS_LOADS)
    else:   # HTML: tags, inline module scripts, inline styles
        found = _loads(text, _HTML_LOADS + _JS_LOADS + _CSS_LOADS)
    return sorted({u.strip() for u in found if _THIRD_PARTY.match(u)})


def _files() -> list[Path]:
    files = sorted((WEB / "templates").rglob("*.html"))
    files += sorted(STATIC.glob("*.js")) + sorted(STATIC.rglob("*.css"))
    files += sorted((ROOT / "explorables").glob("*.html"))
    files += sorted(REVEAL.rglob("*.esm.js"))
    return files


def test_the_scan_covers_the_pages():
    names = {p.name for p in _files()}
    assert {"base.html", "chat.html", "login.html", "chat.js", "reveal.esm.js", "white.css"} <= names
    assert len(_files()) >= 25


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_nothing_is_loaded_from_another_origin(path):
    assert third_party(path) == []


def test_the_scan_catches_each_kind_of_load(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(
        '<script src="https://cdn.example/a.js"></script>'
        '<link rel="stylesheet" href="//cdn.example/b.css">'
        '<a href="https://example.edu/syllabus">a link is not a load</a>'
        "<script type=\"module\">\nimport X from 'https://cdn.example/c.esm.js';\n</script>"
        "<style>@import url(https://fonts.example/d.css); .x { background: url('https://cdn.example/e.png') }</style>"
        '<img src="/static/local.png">')
    assert third_party(page) == ["//cdn.example/b.css", "https://cdn.example/a.js", "https://cdn.example/c.esm.js",
                                 "https://cdn.example/e.png", "https://fonts.example/d.css"]


def test_a_rendered_deck_loads_nothing_from_another_origin(tmp_path):
    assert third_party(rendered_deck(tmp_path)) == []


def test_a_deck_names_only_vendored_files_that_exist(tmp_path):
    deck = rendered_deck(tmp_path).read_text()
    assets = _loads(deck, _HTML_LOADS + _JS_LOADS)
    assert len(assets) == 6
    for url in assets:
        assert url.startswith("/static/vendor/reveal/"), url
        assert (STATIC / url.removeprefix("/static/")).is_file(), url


def test_the_vendored_stylesheets_resolve():
    """The theme imports its font stylesheet, which names the font files; a
    missing one would be a quiet 404 rather than a CDN call, but a broken deck."""
    for css in REVEAL.rglob("*.css"):
        for ref in _loads(css.read_text(), _CSS_LOADS):
            if ref.startswith("data:"):
                continue
            target = (css.parent / ref.split("?")[0].split("#")[0]).resolve()
            assert target.is_file(), f"{css.relative_to(REVEAL)} names {ref}"


def test_a_deck_opens_with_no_third_party_request(browser, tmp_path):
    from tests.browser_support import ORIGIN, app_client, open_page, student_db

    deck = rendered_deck(tmp_path).read_text()
    requests: list[str] = []
    with app_client(student_db()) as client, open_page(browser, client, width=1280, height=800) as (page, net):
        net.stubs[("GET", "/slides/AST101/lecture-01/index.html")] = lambda route, url: route.fulfill(
            status=200, content_type="text/html", body=deck)
        page.on("request", lambda r: requests.append(r.url))
        page.goto(f"{ORIGIN}/slides/AST101/lecture-01/index.html")
        page.wait_for_function("document.querySelector('.reveal')?.classList.contains('ready')", timeout=15000)
    outside = [u for u in requests if not u.startswith((ORIGIN + "/", "data:", "blob:"))]
    assert outside == []
    assert f"{ORIGIN}/static/vendor/reveal/dist/reveal.esm.js" in requests
    assert any(u.endswith(".woff") for u in requests)                   # the theme's font, same origin
