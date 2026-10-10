"""deck-track.js and the tag that loads it (activity analytics, step 1.6).

The /slides mount serves every deck's index.html with one versioned tracking
tag before </body> (a rendered deck carries none, so it keeps naming only
the vendored Reveal files); nothing else under the mount changes. In
headless Chromium a rendered two-slide deck records its opening, the slide
change with the dwell on the first slide, the narration played, a hidden
tab, and the close.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import wave
from pathlib import Path

import pytest

from tests.browser_support import ORIGIN, app_client, open_page, student_db
from tutor import config, deck_tracking
from tutor.db.models import ActivityEvent
from tutor.templating import STATIC_DIR

GOLDEN_CONTENT = Path(__file__).resolve().parent / "golden" / "deck" / "content.md"


def _digest() -> str:
    return hashlib.sha256((STATIC_DIR / "deck-track.js").read_bytes()).hexdigest()[:12]


def _tag(heartbeat="60") -> str:
    return f'<script src="/static/deck-track.js?v={_digest()}" data-heartbeat="{heartbeat}"></script>'


# ---------------------------------------------------------------------------
# The injector
# ---------------------------------------------------------------------------

def test_a_bare_deck_gets_one_tag_before_its_body_end():
    out = deck_tracking.inject("<html><body><p>x</p>\n</body>\n</html>")
    assert out == f"<html><body><p>x</p>\n{_tag()}\n</body>\n</html>"
    assert deck_tracking.inject(out) == out                       # idempotent


def test_an_existing_plain_tag_is_replaced_by_the_versioned_one(monkeypatch):
    monkeypatch.setenv("ACTIVITY_HEARTBEAT_S", "30")
    config.get_config.cache_clear()
    plain = '<html><body><script src="/static/deck-track.js"></script>\n</body></html>'
    out = deck_tracking.inject(plain)
    assert out.count("deck-track.js") == 1 and _tag("30") in out


def test_no_body_end_still_gets_the_tag_and_off_means_none(monkeypatch):
    assert deck_tracking.inject("<p>fragment</p>").endswith(_tag() + "\n")
    monkeypatch.setenv("ACTIVITY_EVENTS", "off")
    config.get_config.cache_clear()
    assert deck_tracking.inject(f"<body>{_tag()}</body>") == "<body></body>"


# ---------------------------------------------------------------------------
# The mount
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    session = student_db()
    yield session
    session.close()


@pytest.fixture
def decks(tmp_path):
    """The /slides mount served from a temporary output tree holding one
    rendered deck (lecture 1, with narration clips beside it so the deck
    embeds the player) and one hand-written deck without the tag."""
    from pipeline.render.html_renderer import render_lecture
    from tutor.main import app

    out = tmp_path / "output"
    lec = out / "AST101" / "lecture-01"
    (lec / "audio").mkdir(parents=True)
    (lec / "audio" / "slide-01.mp3").write_bytes(b"ID3")
    shutil.copy(GOLDEN_CONTENT, lec / "content.md")
    render_lecture(lec / "content.md", lec / "index.html", "AST 101 Lecture 01")
    bare = out / "AST101" / "lecture-02"
    bare.mkdir()
    (bare / "index.html").write_text("<html><body><p>old deck</p></body></html>")
    (bare / "notes.txt").write_text("plain file")
    mount = next(r for r in app.routes if getattr(r, "name", "") == "slides")
    before = list(mount.app.all_directories)
    mount.app.all_directories = [str(out)]
    try:
        yield out
    finally:
        mount.app.all_directories = before


def test_the_mount_serves_every_deck_with_one_versioned_tag(db, decks):
    with app_client(db) as client:
        rendered = client.get("/slides/AST101/lecture-01/index.html")
        assert rendered.status_code == 200 and rendered.headers["cache-control"] == "no-cache"
        assert rendered.text.count("deck-track.js") == 1 and _tag() in rendered.text
        assert 'id="deck-narration"' in rendered.text
        old = client.get("/slides/AST101/lecture-02/index.html")
        assert old.text == f"<html><body><p>old deck</p>{_tag()}\n</body></html>"
        assert client.get("/slides/AST101/lecture-02/notes.txt").text == "plain file"
        assert client.get("/slides/AST101/lecture-03/index.html").status_code == 404
        served = client.get(f"/static/deck-track.js?v={_digest()}")
        assert served.status_code == 200 and "javascript" in served.headers["content-type"]


def test_a_deck_still_needs_a_signed_in_student(db, decks):
    from fastapi.testclient import TestClient

    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    assert TestClient(app).get("/slides/AST101/lecture-01/index.html").status_code == 401


# ---------------------------------------------------------------------------
# In the browser
# ---------------------------------------------------------------------------

def _silence(seconds=0.4) -> bytes:
    """A WAV of silence, served as the narration clip."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * int(8000 * seconds))
    return buf.getvalue()


def _rows(db):
    db.expire_all()
    return db.query(ActivityEvent).order_by(ActivityEvent.seq).all()


def test_a_deck_records_its_slides_narration_and_close(browser, db, decks):
    clip = _silence(1.2)
    with app_client(db) as client:
        with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
            for n in range(1, 7):
                net.stubs[("GET", f"/lecture-audio/AST101/lecture-01/audio/slide-0{n}.mp3")] = (
                    lambda route, url: route.fulfill(status=200, content_type="audio/wav", body=clip))
            page.goto(f"{ORIGIN}/slides/AST101/lecture-01/index.html")
            page.wait_for_function("() => window.deck && window.tutorDeckFlush")
            page.wait_for_timeout(400)                      # on the first slide
            page.evaluate("() => window.deck.next()")
            page.wait_for_timeout(100)
            page.click("#deck-narrate-btn")                 # plays slide 2's clip ...
            # ... whose end advances the deck to slide 3 and plays on.
            page.wait_for_function("() => window.deck.getIndices().h >= 2")
            page.click("#deck-narrate-btn")                 # pause
            page.wait_for_timeout(100)
            page.evaluate("""() => {
                Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_timeout(100)
            page.evaluate("() => window.tutorDeckFlush()")
            page.wait_for_timeout(300)

    rows = _rows(db)
    names = [r.name for r in rows]
    assert names[0] == "deck_open" and "deck_close" not in names, names
    opened = rows[0]
    assert (opened.course, opened.lecture_num, opened.slide_num) == ("AST101", 1, 1)
    assert json.loads(opened.detail_json) == {"context": "tab", "has_audio": True, "slides_total": 6}
    views = [r for r in rows if r.name == "slide_view"]
    assert len(views) >= 2
    first = views[0]
    assert (first.slide_num, json.loads(first.detail_json)) == (2, {"how": "next", "prev_slide": 1})
    assert first.value_ms >= 300                            # the dwell on slide 1
    assert views[1].slide_num == 3 and json.loads(views[1].detail_json)["how"] == "auto"
    played = [r for r in rows if r.name == "narration_play"]
    ended = [r for r in rows if r.name == "narration_ended"]
    paused = [r for r in rows if r.name == "narration_pause"]
    assert played[0].slide_num == 2 and ended[0].slide_num == 2 and ended[0].value_ms >= 900
    assert paused and paused[-1].slide_num == 3
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    assert all(r.path == "/slides/AST101/lecture-01/index.html" for r in rows)


def test_a_deck_in_the_chat_panel_knows_its_context_and_shares_the_visit(browser, db, decks):
    with app_client(db) as client:
        with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
            page.goto(f"{ORIGIN}/chat?course=AST101")
            page.wait_for_function("() => typeof window.tutorTrack === 'function'")
            frame = page.frame_locator("#slide-container iframe")
            frame.locator(".reveal").wait_for()
            page.wait_for_function(
                "() => { const f = document.querySelector('#slide-container iframe');"
                " return f && f.contentWindow && f.contentWindow.tutorDeckFlush; }")
            page.wait_for_timeout(200)
            page.evaluate("() => document.querySelector('#slide-container iframe').contentWindow.tutorDeckFlush()")
            page.evaluate("() => window.tutorTrackFlush()")
            page.wait_for_timeout(300)

    rows = _rows(db)
    opened = next(r for r in rows if r.name == "deck_open")
    assert json.loads(opened.detail_json)["context"] == "chat"
    page_view = next(r for r in rows if r.name == "page_view")
    assert opened.visit_id == page_view.visit_id and opened.page_view_id != page_view.page_view_id
