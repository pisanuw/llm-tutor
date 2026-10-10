"""Links the app hands out point at something (student-facing review,
step 0.3; findings F3, F4, F8, F10).

F3: the What's-next card, the map and the lecture picker built deck URLs
with no check that the deck existed. F4: a goal's book anchor was never
checked against the book's headings. F8: a partial slide seed booted clean.
F10: no test resolved a template's links against the app's routes.
Done when: every static link in a template resolves to a route or a mount,
every book anchor names a heading pandoc would give that id, a deck is
offered only when it is on disk, and the seed check says which decks are
missing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tutor import decks, seed_check
from tutor.courses import COURSES, lecture_numbers
from tutor.db.models import LearningGoal, Student, StudentPath, Track, WhitelistEntry
from tutor.paths import CURRICULUM_DIR
from tests.helpers import login_cookie, make_session

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "tutor" / "web" / "templates"
STATIC = ROOT / "tutor" / "web" / "static"
GOALS = json.loads((CURRICULUM_DIR / "learning_goals.json").read_text())["goals"]

# Literal same-site targets in the templates: href, action, src.
_LINK = re.compile(r'(?:href|action|src)="(/[^"{}]*)"')
# Mounts whose files come from a seed, a build or the disk: the prefix is
# checked, the file is not (it is not in the repository).
_SEEDED = ("/slides/", "/lecture-audio/", "/book/", "/explorables/")


def _routes():
    from tutor.main import app

    return app.routes


def _resolves(path: str) -> bool:
    """A route of the app matches ``path`` for a GET, or a mount serves it
    (a file under /static must exist in the repository)."""
    from starlette.routing import Match, Mount

    if path.startswith("/static/"):
        return (STATIC / path[len("/static/"):].split("?")[0]).is_file()
    if path.startswith(_SEEDED):
        # /book and /lecture-audio are mounted only when their directories
        # exist (a build, a seed); the prefix is the app's either way.
        return (path.startswith(("/book/", "/lecture-audio/"))
                or any(isinstance(r, Mount) and path.startswith(r.path + "/") for r in _routes()))
    scope = {"type": "http", "method": "GET", "path": path.split("?")[0], "root_path": "",
             "headers": [], "query_string": b""}
    for route in _routes():
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return True
        if match == Match.PARTIAL and getattr(route, "methods", None) and "GET" not in route.methods:
            return True   # a POST-only form action
    return False


def test_every_literal_link_in_a_template_resolves():
    bad = []
    for tpl in sorted(TEMPLATES.glob("*.html")):
        for m in _LINK.finditer(tpl.read_text()):
            target = m.group(1).split("#")[0]
            if not target or target == "/":
                continue
            if not _resolves(target):
                bad.append(f"{tpl.name}: {target}")
    assert bad == [], bad


def test_every_literal_link_in_a_page_script_resolves():
    bad = []
    for script in ("chat.js", "map.js", "track.js", "conversations.js", "deck-track.js"):
        text = (STATIC / script).read_text()
        for m in re.finditer(r"""['"`](/(?:chat|path|review|api|auth|map|lectures|errors|feedback)[^'"`$?\s]*)""", text):
            target = m.group(1)
            if target.endswith("/"):
                continue
            if not _resolves(target):
                bad.append(f"{script}: {target}")
    assert bad == [], bad


# ---------------------------------------------------------------------------
# The curriculum's anchors
# ---------------------------------------------------------------------------

def test_every_slide_anchor_names_a_lecture_of_the_plan():
    bad = []
    for g in GOALS:
        anchor = g.get("slide_anchor") or {}
        lecture = anchor.get("lecture", g.get("lecture"))
        if not isinstance(lecture, int) or lecture not in lecture_numbers(g["course"]):
            bad.append((g["id"], lecture))
    assert bad == []
    assert any(g.get("slide_anchor") for g in GOALS)        # the fixture carries anchors


def _pandoc_ids(chapter: Path) -> set[str]:
    html = subprocess.run(["pandoc", "--from", "markdown", "--to", "html", "--section-divs", str(chapter)],
                          capture_output=True, text=True, check=True).stdout
    return set(re.findall(r'<(?:section|h[1-6])[^>]*\sid="([^"]+)"', html))


def _example_book_present() -> bool:
    """The book lives at the repository root, outside the content root; the
    fixture's anchors name the example course's chapters (book-chapters.json),
    so they are checked only while that book is the one checked out."""
    chapters = json.loads((CURRICULUM_DIR / "book-chapters.json").read_text())
    stems = {stem for course, by_lecture in chapters.items() if not course.startswith("_")
             for stem in by_lecture.values()}
    return bool(stems) and all((ROOT / "book" / f"{stem}.qmd").is_file() for stem in stems)


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc gives the book its heading ids")
def test_every_book_anchor_names_a_heading_pandoc_would_give_that_id():
    """The book is rendered by Quarto, whose heading ids are pandoc's auto
    identifiers; an explicit {#id} wins. A chapter is parsed once."""
    if not _example_book_present():
        pytest.skip("book/ is not the example course's book")
    assert any(g.get("book_anchor") for g in GOALS)          # the fixture carries anchors
    ids: dict[str, set[str]] = {}
    bad = []
    for g in GOALS:
        anchor = g.get("book_anchor")
        if not anchor:
            continue
        chapter = ROOT / "book" / f"{anchor['chapter']}.qmd"
        if not chapter.is_file():
            bad.append((g["id"], "no chapter", anchor["chapter"]))
            continue
        if anchor["chapter"] not in ids:
            ids[anchor["chapter"]] = _pandoc_ids(chapter)
        if anchor["section_id"] not in ids[anchor["chapter"]]:
            bad.append((g["id"], anchor["chapter"], anchor["section_id"]))
    assert bad == []


# ---------------------------------------------------------------------------
# Decks are offered only when they exist
# ---------------------------------------------------------------------------

@pytest.fixture
def decks_on_disk(tmp_path, monkeypatch):
    """Course AST101 has lectures 1 and 4 rendered; AST102 has none."""
    import tutor.config as config

    out = tmp_path / "output"
    for n in (1, 4):
        d = out / "AST101" / f"lecture-{n:02d}"
        d.mkdir(parents=True)
        (d / "index.html").write_text("<html><body><section>deck</section></body></html>")
    cfg = dict(config.get_config())
    cfg["output_dir"] = out
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(decks, "get_config", lambda: cfg)
    decks._reset_for_tests()
    yield out
    decks._reset_for_tests()


def test_the_deck_helper_reads_the_output_directory(decks_on_disk):
    assert decks.available_lectures("AST101") == [1, 4]
    assert decks.available_lectures("AST102") == []
    assert decks.available_lectures("999") == []
    assert decks.deck_url("AST101", 4) == "/slides/AST101/lecture-04/index.html"
    assert decks.deck_url("AST101", 5) is None and decks.deck_url("AST101", None) is None
    assert decks.available_decks() == {"AST101": [1, 4], "AST102": []}
    assert decks.missing_lectures("AST101") == [2, 3] and decks.missing_lectures("AST102") == [1, 2, 3, 4]


def test_the_seed_check_names_the_missing_decks(decks_on_disk):
    ok, detail = seed_check.check_slides(decks_on_disk)
    assert ok is False and detail.startswith("6 deck(s) missing: AST101/lecture-02, AST101/lecture-03")
    for course in COURSES:
        for n in lecture_numbers(course):
            d = decks_on_disk / course / f"lecture-{n:02d}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "index.html").write_text("x")
    assert seed_check.check_slides(decks_on_disk) == (True, "8 decks")
    assert seed_check.main(["slides", str(decks_on_disk)]) == 0


@pytest.fixture
def signed_in(decks_on_disk):
    from fastapi.testclient import TestClient

    from tutor.auth.deps import get_db
    from tutor.main import app

    db = make_session()
    db.add(Student(id=1, email="ada@example.edu", name="Ada"))
    db.add(WhitelistEntry(email="ada@example.edu", added_by="test"))
    for course in ("AST101", "AST102"):
        db.add(Track(id=f"{course}-lecture-order", course=course, title="Lecture order", goal_ids_json="[]"))
    db.add(LearningGoal(id="AST101-L04-telescope-aperture", title="Telescope aperture", description="",
                        course="AST101", lecture_num=4,
                        slide_anchor=json.dumps({"lecture": 4, "slide": 3, "title": "Aperture"})))
    db.add(LearningGoal(id="AST101-L02-eclipse-conditions", title="Eclipses", description="",
                        course="AST101", lecture_num=2,
                        slide_anchor=json.dumps({"lecture": 2, "slide": 1, "title": "Eclipses"})))
    db.commit()
    db.add(StudentPath(student_id=1, course="AST101", track_id="AST101-lecture-order"))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set("session_token", login_cookie(db, 1))
    try:
        yield client, db
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_the_map_offers_slides_only_for_a_deck_on_disk(signed_in):
    client, _db = signed_in
    html = client.get("/map?course=AST101").text
    graph = json.loads(html.split('<script type="application/json" id="graph-data">')[1].split("</script>")[0])
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id["AST101-L04-telescope-aperture"]["slides_url"] == "/slides/AST101/lecture-04/index.html"
    assert by_id["AST101-L02-eclipse-conditions"]["slides_url"] is None
    assert by_id["AST101-L02-eclipse-conditions"]["slide_anchor"]["lecture"] == 2    # the anchor itself stays
    assert "d.slides_url" in (STATIC / "map.js").read_text() and "d.slide_anchor.lecture).padStart" not in (STATIC / "map.js").read_text()


def test_the_chat_page_carries_the_decks_on_disk(signed_in):
    client, _db = signed_in
    html = client.get("/chat?course=AST101").text
    assert 'window.TUTOR_DECKS = {"AST101": [1, 4], "AST102": []};' in html
    assert "no slides yet" in (STATIC / "chat.js").read_text()


def test_whats_next_links_slides_only_for_a_deck_on_disk(signed_in, monkeypatch):
    from tutor.routers import chat as chat_router

    client, db = signed_in
    from tutor.agent import navigator

    monkeypatch.setattr(navigator, "next_goals",
                        lambda student_id, course, db, track_id=None, n=3: [
                            "AST101-L04-telescope-aperture", "AST101-L02-eclipse-conditions"])
    by_id = {c["id"]: c for c in chat_router._whats_next(1, "AST101", db)}
    assert by_id["AST101-L04-telescope-aperture"]["slides_url"] == "/slides/AST101/lecture-04/index.html"
    assert by_id["AST101-L02-eclipse-conditions"]["slides_url"] is None


def test_an_absent_book_or_narration_directory_is_a_boot_warning_on_render(tmp_path, monkeypatch):
    import tutor.main as main

    built, clips = tmp_path / "_book", tmp_path / "audio"
    assert main._data_warnings(built, clips, production=False) == []           # a checkout: nothing
    said = main._data_warnings(built, clips, production=True)
    assert len(said) == 2 and "book is not built" in said[0] and "narration directory is absent" in said[1]
    built.mkdir()
    clips.mkdir()
    assert main._data_warnings(built, clips, production=True) == []
    monkeypatch.setenv("RENDER", "true")
    assert main._data_warnings(tmp_path / "nope", clips) == [main._data_warnings(tmp_path / "nope", clips, production=True)[0]]
    monkeypatch.delenv("RENDER")
    assert main._data_warnings(tmp_path / "nope", clips) == []
