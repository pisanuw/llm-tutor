"""Book/static link fixes.

Guards two regressions:
  1. The /book mount must use html=True so the Quarto site root ("/book/",
     where the brand/logo links) serves index.html instead of 404ing.
  2. The in-app "Book" nav links go straight to the book, not through the /book
     auth-gate redirect hop.
"""

from __future__ import annotations

import re
from pathlib import Path

from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _ROOT / "tutor" / "web" / "templates"


def test_static_html_true_serves_directory_index(tmp_path):
    """The mechanism behind the fix: html=True serves index.html for a dir."""
    (tmp_path / "index.html").write_text("<h1>home</h1>")

    with_html = Starlette()
    with_html.mount("/b", StaticFiles(directory=str(tmp_path), html=True))
    c = TestClient(with_html)
    assert c.get("/b/").status_code == 200          # dir root -> index.html
    assert "home" in c.get("/b/").text
    assert c.get("/b/index.html").status_code == 200

    without_html = Starlette()
    without_html.mount("/b", StaticFiles(directory=str(tmp_path)))
    c2 = TestClient(without_html)
    assert c2.get("/b/").status_code == 404          # the bug we fixed


def test_book_mount_uses_html_true():
    src = (_ROOT / "tutor" / "main.py").read_text()
    line = next(ln for ln in src.splitlines() if '"/book"' in ln and "mount" in ln)
    assert "html=True" in line, f"/book mount must set html=True: {line.strip()}"


def test_book_nav_links_are_direct():
    """The book link lives in the shared site header (plan step 3.1), which
    every signed-in page renders."""
    nav = (_TEMPLATES / "_nav.html").read_text()
    assert "/book/index.html" in nav, "the site header should link the book directly"
    for name in ("chat.html", "map.html"):
        html = (_TEMPLATES / name).read_text()
        assert "site_header(" in html, f"{name} should render the shared site header"
        assert 'href="/book"' not in html, f"{name} still uses the /book redirect hop"


def test_no_bare_book_dir_link_in_templates():
    """Nav links should target index.html explicitly (robust even if a future
    mount drops html=True); the bare '/book/' form is only safe post-fix."""
    for tpl in _TEMPLATES.glob("*.html"):
        for m in re.finditer(r'href="(/book/[^"]*)"', tpl.read_text()):
            href = m.group(1)
            assert href != "/book/", f"{tpl.name}: link to bare /book/ (use index.html)"
