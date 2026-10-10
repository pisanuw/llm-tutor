"""Plan step 5.5 (finding D20): the frontend's scripts.

D20: htmx and its SSE extension (58 KB) loaded on every page though no page
used an hx- attribute; marked's highlight option was a no-op in the vendored
version; the chat's 1,300-line script and the map's script sat inline in
their templates, re-sent with every page view.
Done when: chat.html under 400 lines; page weight down 58 KB. The page has
since taken on about twenty one-line config scripts and Jinja sets naming the
tutor and the courses (courses.yaml), so the line budget is 425; the inline
script check below is what keeps the logic in chat.js.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re

import pytest

from tests.browser_support import ORIGIN, app_client, open_page, student_db

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "tutor" / "web" / "templates"
STATIC = ROOT / "tutor" / "web" / "static"


def _inline_scripts(html: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)]


def test_chat_html_is_markup_again():
    chat = (TEMPLATES / "chat.html").read_text()
    assert len(chat.splitlines()) < 425
    for name in ("chat.html", "map.html"):
        for body in _inline_scripts((TEMPLATES / name).read_text()):
            assert len(body.strip().splitlines()) <= 1, (name, body[:80])     # config lines only


def test_htmx_is_gone():
    vendor = {p.name for p in (STATIC / "vendor").iterdir()}
    assert not {"htmx.min.js", "htmx-sse.js"} & vendor
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text()
        assert "htmx.min.js" not in text and "htmx-sse" not in text and " hx-" not in text, tpl.name
    assert "window.htmx" not in (TEMPLATES / "base.html").read_text()


def test_marked_has_no_highlight_option():
    js = (STATIC / "chat.js").read_text()
    assert "marked.setOptions({breaks: true, gfm: true});" in js
    assert "highlight:" not in js
    assert "Prism.highlightAllUnder" in js            # highlighting is done after rendering


def test_the_scripts_say_one_hint_at_a_time_too():
    for name in ("chat.js", "map.js"):
        assert "rung" not in (STATIC / name).read_text().lower(), name


def test_the_page_scripts_are_served_by_content_version():
    db = student_db()
    with app_client(db) as client:
        for page, name in (("/chat?course=AST101", "chat.js"), ("/map?course=AST101", "map.js")):
            html = client.get(page).text
            src = re.search(rf'<script src="(/static/{name}\?v=([0-9a-f]{{12}}))"></script>', html)
            assert src, page
            digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
            assert src.group(2) == digest
            served = client.get(src.group(1))
            assert served.status_code == 200 and "javascript" in served.headers["content-type"]


def test_a_changed_file_gets_a_new_url(tmp_path, monkeypatch):
    from tutor import templating

    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    (tmp_path / "x.js").write_text("one")
    first = templating.asset_url("x.js")
    assert first.startswith("/static/x.js?v=") and templating.asset_url("x.js") == first
    (tmp_path / "x.js").write_text("two")
    stat = (tmp_path / "x.js").stat()
    os.utime(tmp_path / "x.js", ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert templating.asset_url("x.js") != first
    assert templating.asset_url("missing.js") == "/static/missing.js"


def test_the_stylesheet_scans_the_page_scripts():
    """The chat builds elements with Tailwind classes; a class that lives only
    in chat.js must still be compiled in."""
    config = (ROOT / "frontend" / "tailwind.config.js").read_text()
    assert '"./tutor/web/static/*.js"' in config
    css = (STATIC / "vendor" / "tailwind.css").read_text()
    # RETRY_BTN in chat.js; hover:bg-amber-700 appears in no template.
    assert "hover:bg-amber-700" in (STATIC / "chat.js").read_text()
    assert not any("hover:bg-amber-700" in t.read_text() for t in TEMPLATES.glob("*.html"))
    assert ".hover\\:bg-amber-700:hover" in css


def test_the_page_weight_went_down():
    """htmx.min.js (48,101 bytes) and htmx-sse.js (10,081) left every page."""
    base = (TEMPLATES / "base.html").read_text()
    scripts = re.findall(r'<script[^>]*src="/static/([^"?]+)"', base)
    assert scripts == ["vendor/alpine-collapse.min.js", "vendor/alpine.min.js"]


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_the_chat_and_the_map_run_from_their_static_scripts(browser, width, height):
    db = student_db()
    with app_client(db) as client:
        with open_page(browser, client, width=width, height=height, clock=False) as (page, net):
            page.goto(f"{ORIGIN}/chat?course=AST101")
            page.wait_for_function("window.Alpine && document.querySelector('[x-data]')._x_dataStack")
            assert page.evaluate("Alpine.$data(document.querySelector('[x-data]')).course") == "AST101"
            assert page.evaluate("typeof tutorApp") == "function"
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            page.goto(f"{ORIGIN}/map?course=AST101")
            page.wait_for_function("typeof GRAPH === 'object' && Array.isArray(GRAPH.nodes)")
