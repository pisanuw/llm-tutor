"""scripts/link_check.py, network-free: a fake site answers the crawl
(step 0.3; F10, F13)."""

from __future__ import annotations

import json

import httpx
import pytest

from scripts import link_check

SITE = "https://tutor.test"

PAGES = {
    "/auth/login": '<a href="/help">Help</a> <a href="/book/index.html">Book</a> '
                   '<link rel="stylesheet" href="/static/vendor/tailwind.css"> <a href="/chat">Chat</a>',
    "/help": '<a href="/book/index.html">the book</a> <a href="mailto:x@example.edu">mail</a> <a href="#top">top</a>',
    "/book/index.html": '<a href="01-intro.html">1</a> <a href="99-gone.html">99</a> '
                           '<img src="/book/site_libs/logo.png"> <a href="https://elsewhere.example/x">out</a>',
    "/book/01-intro.html": '<a href="index.html">home</a>',
    "/book/site_libs/logo.png": "png",
    "/static/vendor/tailwind.css": "css",
    "/explorables/AST101-L02-explain-moon-phases.html": "<p>explorable</p>",
}


def _site(decks_missing=None, token_ok=True):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "elsewhere.example":
            return httpx.Response(200)
        if request.url.host == "blocked.example":
            return httpx.Response(403)
        if request.url.host == "gone.example":
            return httpx.Response(404)
        if path == "/admin/export/decks":
            if request.headers.get("X-Admin-Token") != "tkn" or not token_ok:
                return httpx.Response(401)
            missing = decks_missing or {"AST101": [], "AST102": []}
            return httpx.Response(200, json={"AST101": [1], "AST102": [1], "missing": missing})
        if path in PAGES:
            ctype = "text/html" if not path.endswith((".png", ".css")) else "application/octet-stream"
            return httpx.Response(200, text=PAGES[path], headers={"content-type": ctype})
        return httpx.Response(404, text="not here", headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handle), base_url=SITE, follow_redirects=True)


def test_links_in_resolves_relative_and_skips_the_non_links():
    out = link_check.links_in(PAGES["/help"], SITE + "/help")
    assert out == [SITE + "/book/index.html"]
    out = link_check.links_in(PAGES["/book/index.html"], SITE + "/book/index.html")
    assert SITE + "/book/01-intro.html" in out and "https://elsewhere.example/x" in out


def test_the_crawl_finds_the_dead_chapter_and_stops_at_gated_pages(monkeypatch):
    monkeypatch.setattr(link_check, "explorable_pages", lambda: ["/explorables/AST101-L02-explain-moon-phases.html",
                                                                  "/explorables/AST102-L99-missing.html"])
    with _site() as client:
        report = link_check.run(SITE, None, external=False, client=client)
    assert sorted(report.dead) == [
        f"404 {SITE}/book/99-gone.html",
        f"404 {SITE}/explorables/AST102-L99-missing.html",
    ]
    assert report.notes == ["ADMIN_TOKEN unset: the decks on the instance were not counted."]
    assert report.ok is False
    text = link_check.render(report)
    assert "2 dead" in text and "DEAD     404" in text


def test_external_links_blocked_is_not_dead(monkeypatch):
    monkeypatch.setattr(link_check, "explorable_pages", lambda: [])
    monkeypatch.setattr(link_check, "opener_links", lambda: {
        "https://elsewhere.example/x": ["curriculum/teachme/AST101/a.json"],
        "https://blocked.example/y": ["curriculum/teachme/AST101/b.json"],
        "https://gone.example/z": ["curriculum/teachme/AST102/c.json"],
    })
    with _site() as client:
        report = link_check.run(SITE, "tkn", external=True, client=client)
    assert [d.split(" (")[0] for d in report.dead] == [f"404 {SITE}/book/99-gone.html", "404 https://gone.example/z"]
    assert report.blocked == ["403 https://blocked.example/y (curriculum/teachme/AST101/b.json)"]


def test_missing_decks_on_the_instance_fail_the_run(monkeypatch):
    monkeypatch.setattr(link_check, "explorable_pages", lambda: [])
    with _site(decks_missing={"AST101": [], "AST102": [3, 4]}) as client:
        report = link_check.run(SITE, "tkn", external=False, client=client)
    assert report.missing_decks == {"AST102": [3, 4]} and report.ok is False
    assert "AST 102: 2 lecture(s) without a deck" in link_check.render(report)
    with _site(token_ok=False) as client:
        report = link_check.run(SITE, "tkn", external=False, client=client)
    assert "401 /admin/export/decks" in report.dead


def test_the_openers_external_links_are_found():
    links = link_check.opener_links()
    assert all(u.startswith("http") for u in links)
    assert all(f.startswith("curriculum/teachme/") for fs in links.values() for f in fs)


@pytest.mark.parametrize("path,is_gated", [("/chat", True), ("/slides/AST101/lecture-01/index.html", True),
                                           ("/book/05.html", False), ("/help", False), ("/", True)])
def test_gated_paths(path, is_gated):
    assert link_check.gated(path) is is_gated


def test_the_decks_export_needs_the_admin_token(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import tutor.config as config
    from tutor import decks
    from tutor.main import app

    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()
    cfg = dict(config.get_config())
    cfg["output_dir"] = tmp_path
    for n in (1, 2):
        d = tmp_path / "AST101" / f"lecture-{n:02d}"
        d.mkdir(parents=True)
        (d / "index.html").write_text("x")
    monkeypatch.setattr(decks, "get_config", lambda: cfg)
    decks._reset_for_tests()
    client = TestClient(app)
    assert client.get("/admin/export/decks").status_code in (303, 401, 403)
    resp = client.get("/admin/export/decks", headers={"X-Admin-Token": "tkn-test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["AST101"] == [1, 2] and data["AST102"] == []
    assert data["missing"]["AST101"] == [3, 4] and data["missing"]["AST102"] == [1, 2, 3, 4]
    assert json.dumps(data)   # plain JSON for the crawl
    decks._reset_for_tests()
