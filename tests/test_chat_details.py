"""Plan step 3.5 (findings D6, D13, D14, D15, D16, D18, D19, D23, D27, D28,
B22): chat details.

The server-side checks run everywhere; the page checks run in headless
Chromium (tests/browser_support.py) and skip where Playwright or a Chromium
it can launch is missing.
Done when: each has a screenshot or a test.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse

import pytest

from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tutor.agent.state import record_topic_access, topic_label
from tutor.agent.tts import _clean_for_speech
from tutor.db.models import Message, TopicAccess

# A 2 x 2 PNG for the photo input.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAAiGAEBF8e1lQAAAABJRU5ErkJggg==")


@pytest.fixture
def db():
    s = student_db()
    s.add_all([Message(student_id=1, course="AST101", role="user", content="What is a lunar phase?"),
               Message(student_id=1, course="AST101", role="assistant",
                       content="A lunar phase is how much of the lit half we see.")])
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db, no_decks):
    with app_client(db) as c:
        yield c


@pytest.fixture
def no_decks(tmp_path, monkeypatch):
    """No deck on disk, whatever the working tree's output/ holds: the
    /slides mount and the page's deck list both read an empty folder."""
    from tutor import decks
    from tutor.main import app

    real = decks.get_config
    monkeypatch.setattr(decks, "get_config", lambda: {**real(), "output_dir": tmp_path})
    mount = next(r for r in app.routes if getattr(r, "name", "") == "slides")
    monkeypatch.setattr(mount.app, "all_directories", [str(tmp_path)])
    decks._reset_for_tests()
    yield tmp_path
    decks._reset_for_tests()


def _reply(net, text, extra=()):
    """The next message gets a turn that streams ``text``."""
    posted = []

    def turn(route, url):
        posted.append(route.request.post_data or "")
        route.fulfill(status=200, content_type="text/event-stream",
                      body=sse([{"type": "turn", "id": "t1"}, *extra,
                                {"type": "token", "text": text},
                                {"type": "done", "full_text": text, "message_id": 1}]))

    net.stubs[("POST", "/chat/message")] = turn
    return posted


def _open_chat(page, query="course=AST101"):
    page.goto(f"{ORIGIN}/chat?{query}")
    page.wait_for_selector("#messages >> text=What is a lunar phase?")


def _send(page, text):
    page.fill("#msg-input", text)
    page.click("button:has-text('Send')")


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message,label", [
    ('Please evaluate my code for this problem: "convert degrees to radians"\n\n```cpp\nint main() {}\n```',
     "convert degrees to radians"),
    ("Please evaluate my C++ code:\n\n```cpp\nint x;\n```", "Code review"),
    ("Why does this print 0?\n```cpp\nint total = 0;\n```\nThanks", "Why does this print 0? Thanks"),
    ("~~~\nint x;\n~~~", "Code review"),
    ("Explain parallax", "Explain parallax"),
    ("x" * 100, "x" * 64),
])
def test_topic_labels_leave_code_out(message, label):
    """D19: the fence text leaked into Recent topics and the admin Topics tab."""
    assert topic_label(message) == label


def test_recorded_topics_use_the_label(db):
    record_topic_access(db, 1, 3, None, "Please evaluate my C++ code:\n\n```cpp\nint x;\n```")
    assert db.query(TopicAccess).one().topic_tag == "Code review"


def test_the_voice_does_not_read_the_replayed_question():
    """B22: after an exam pass the reply quotes the student's question."""
    reply = ("**Mastery confirmed** for Moon phases (3/3).\n\n---\n\n"
             "Back to your question: “my email is jsmith3@example.edu, why is the Moon a crescent?”\n\n"
             "Start from where the Sun is.")
    speech = _clean_for_speech(reply)
    assert "jsmith3" not in speech and "crescent" not in speech
    assert speech == ("Mastery confirmed for Moon phases (3/3). Back to your question. "
                      "Start from where the Sun is.")


def test_the_chat_page_says_whether_a_voice_is_configured(client, monkeypatch):
    """D16: the speak button showed on every reply without a voice."""
    assert "window.TUTOR_TTS = false;" in client.get("/chat?course=AST101").text
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    from tutor.config import get_config

    get_config.cache_clear()
    assert "window.TUTOR_TTS = true;" in client.get("/chat?course=AST101").text


def test_a_missing_lecture_is_a_page_not_json(client):
    """D15: a missing lecture was a bare JSON 404."""
    resp = client.get("/lectures/AST101/99")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Lecture 99 of AST 101 is not available." in resp.text
    assert "data-nav-menu" in resp.text                  # the site header
    assert client.get("/lectures/999/1").status_code == 404


# ---------------------------------------------------------------------------
# In the browser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,prompt", [("quiz", "Quiz me on: Parallax"),
                                         ("review", "Help me review: Parallax"),
                                         ("", "Explain: Parallax")])
def test_map_modes_become_their_own_prompts(browser, client, mode, prompt):
    """D6: Quiz me and Review pre-filled "Explain:" like Teach me."""
    with open_page(browser, client, clock=False) as (page, net):
        _open_chat(page, f"topic=Parallax&course=AST101&lecture_num=3&mode={mode}")
        expect(page.locator("#msg-input")).to_have_value(prompt)


def test_the_review_page_shows_a_load_error_and_no_script_errors(browser, client):
    """D13: the error sat inside a block hidden until a session loaded, and
    five Alpine null reads fired on every load (open_page fails the test on
    any page error)."""
    with open_page(browser, client, clock=False) as (page, net):
        page.goto(f"{ORIGIN}/review?course=AST101")
        expect(page.locator("text=Nothing due right now.")).to_be_visible()
        net.stubs[("GET", "/review/session")] = lambda route, url: route.fulfill(
            status=500, content_type="application/json", body='{"detail": "boom"}')
        page.reload()
        expect(page.locator("[data-review-error]")).to_contain_text("Could not load your review session")
        del net.stubs[("GET", "/review/session")]
        page.click("[data-review-error] button")
        expect(page.locator("text=Nothing due right now.")).to_be_visible()
        expect(page.locator("[data-review-error]")).to_be_hidden()


def test_streaming_follows_only_a_reader_at_the_bottom(browser, client, db):
    """D14: every streamed frame yanked the list to the bottom."""
    for i in range(30):
        db.add(Message(student_id=1, course="AST101", role="assistant", content=f"Old reply {i}.\n\n" * 3))
    db.commit()
    with open_page(browser, client, clock=False) as (page, net):
        _open_chat(page)
        box = page.locator("#messages")
        at_bottom = "el => el.scrollHeight - el.scrollTop - el.clientHeight < 40"
        assert box.evaluate(at_bottom)                    # opens at the latest message
        # A reader who scrolled up keeps their place; a pill offers the reply.
        box.evaluate("el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); }")
        page.evaluate("() => { const d = Alpine.$data(document.querySelector('[x-data]'));"
                      " appendStreamingBubble('b-x'); updateStreamingBubble('b-x', 'New words '.repeat(80)); }")
        expect(page.locator("#new-pill")).to_be_visible()
        assert box.evaluate("el => el.scrollTop") < 50
        page.click("#new-pill")
        assert box.evaluate(at_bottom)
        expect(page.locator("#new-pill")).to_be_hidden()
        # Sending always jumps to the bottom and follows the reply.
        box.evaluate("el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); }")
        _reply(net, "Fresh answer. " * 60)
        _send(page, "Another question")
        expect(page.locator("#messages >> text=Another question")).to_be_visible()
        page.wait_for_function(
            "() => { const el = document.getElementById('messages');"
            " return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }")


def test_a_missing_deck_gets_a_sentence_not_json(browser, client):
    """D15: a missing deck showed the server's JSON in the slide panel."""
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        _open_chat(page, "course=AST101&lecture_num=3")
        expect(page.locator("[data-deck-missing]")).to_be_visible()
        expect(page.locator("[data-deck-missing]")).to_contain_text("AST 101, lecture 3 are not available")
        expect(page.locator("iframe[title='Lecture slides']")).to_be_hidden()


def test_no_speak_button_without_a_voice(browser, client):
    """D16: without a voice the button showed and failed in a tooltip."""
    with open_page(browser, client, clock=False) as (page, net):
        _open_chat(page)
        _reply(net, "Parallax shrinks with distance.")
        _send(page, "Explain parallax")
        bubble = page.locator("[id^=bubble-]").last
        expect(bubble.locator(".fb-wrap")).to_have_count(1)
        assert bubble.locator(".tts-btn").count() == 0


def test_a_voice_failure_says_so_beside_the_button(browser, client, monkeypatch):
    from tutor.config import get_config

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    get_config.cache_clear()
    with open_page(browser, client, clock=False) as (page, net):
        net.stubs[("POST", "/chat/tts")] = lambda route, url: route.fulfill(
            status=502, content_type="application/json", body='{"error": "upstream"}')
        _open_chat(page)
        page.locator("[id^=hist-] .tts-btn").first.click()
        note = page.locator("[id^=hist-] .tts-note").first
        expect(note).to_have_text("Voice unavailable right now.")
        expect(page.locator("#sr-status")).to_contain_text("Voice unavailable right now.")


def test_the_prediction_explains_the_scale_on_one_row(browser, client):
    """D18: the scale was never explained and the row wrapped at 390 px."""
    with open_page(browser, client, clock=False) as (page, net):
        _open_chat(page)
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", "int main() { return 0; }")
        page.click("button:has-text('Evaluate')")
        legend = page.locator("[data-predict-legend]")
        expect(legend).to_contain_text("0 does not compile")
        expect(legend).to_contain_text("4 excellent")
        buttons = page.locator("[data-predict-row] button")
        expect(buttons).to_have_count(6)
        page.wait_for_timeout(500)                        # the strip finishes opening
        tops = buttons.evaluate_all("els => els.map(b => Math.round(b.getBoundingClientRect().top))")
        assert len(set(tops)) == 1, tops                   # one row at 390 px
        assert buttons.nth(3).get_attribute("aria-label") == "I expect 3 out of 4: correct"


def test_submitted_code_shows_as_code(browser, client, db):
    """D19: the student's code was a proportional paragraph with its fences."""
    code = "for (int i = 0; i < n; i++) {\n    sum += a[i];\n}"
    with open_page(browser, client, clock=False) as (page, net):
        _open_chat(page)
        _reply(net, "Looks right.")
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", code)
        page.fill("#problem-input", "sum an array")
        page.click("button:has-text('Evaluate')")
        page.click("[data-predict-row] button:has-text('skip')")
        bubble = page.locator("#messages .justify-end").last
        expect(bubble.locator("pre code")).to_have_text(code)
        assert "```" not in bubble.inner_text()
        assert bubble.locator("pre").evaluate("el => getComputedStyle(el).whiteSpace") == "pre-wrap"
        assert bubble.locator("pre").evaluate("el => getComputedStyle(el).fontFamily").find("mono") >= 0
        # Readable: the code's colour is not its background's (a class
        # missing from the built stylesheet once made it dark on dark).
        assert bubble.locator("pre").evaluate(
            "el => { const s = getComputedStyle(el); return s.color !== s.backgroundColor; }")
        # The same after a reload, from the saved history.
        db.add(Message(student_id=1, course="AST101", role="user",
                       content=f'Please evaluate my code for this problem: "sum an array"\n\n```cpp\n{code}\n```'))
        db.commit()
        page.reload()
        page.wait_for_selector("#messages pre code")
        expect(page.locator("#messages .justify-end pre code").last).to_have_text(code)


@pytest.mark.parametrize("touch", [True, False])
def test_photos_come_from_the_camera_or_the_library_with_inline_errors(browser, client, touch):
    """D23: the input forced the camera, and errors were alert() boxes."""
    with open_page(browser, client, clock=False, touch=touch) as (page, net):
        net.stubs[("POST", "/chat/transcribe-photo")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": False, "error": "No code found in the photo."}))
        page.on("dialog", lambda d: pytest.fail(f"a native dialog opened: {d.message}"))
        _open_chat(page)
        page.click("button[aria-label='Submit code for feedback']")
        expect(page.locator("button:has-text('Choose photo')")).to_be_visible()
        camera = page.locator("button:has-text('Camera')")
        if touch:
            expect(camera).to_be_visible()
        else:
            expect(camera).to_be_hidden()
        assert page.locator("input[type=file][capture]").count() == 1      # the camera's only
        page.set_input_files("input[type=file]:not([capture])",
                             files=[{"name": "shot.png", "mimeType": "image/png", "buffer": PNG}])
        expect(page.locator("[data-photo-error]")).to_have_text("No code found in the photo.")


def test_tutor_markdown_drops_images_and_opens_other_sites_in_a_new_tab(browser, client):
    """D27: <img> was allowed in replies and external links replaced the chat.
    Since review step 0.3 (F7) a link to the map, the slides, the lectures,
    the book or an explorable opens in a new tab too; a /chat link stays."""
    reply = ("See ![x](https://evil.example/x.png) the [notes](https://example.com/notes), "
             "[your map](/map) and [ask about parallax](/chat?topic=parallax).")
    with open_page(browser, client, clock=False) as (page, net):
        _open_chat(page)
        _reply(net, reply)
        _send(page, "Where are the notes?")
        bubble = page.locator("[id^=bubble-]").last
        expect(bubble.locator("a")).to_have_count(3)
        assert bubble.locator("img").count() == 0
        outside = bubble.locator("a[href='https://example.com/notes']")
        assert outside.get_attribute("target") == "_blank"
        assert outside.get_attribute("rel") == "noopener noreferrer"
        assert bubble.locator("a[href='/map']").get_attribute("target") == "_blank"
        assert bubble.locator("a[href='/chat?topic=parallax']").get_attribute("target") is None


@pytest.mark.parametrize("value", ["abc", "25", "0"])
def test_a_bad_lecture_number_keeps_lecture_one(browser, client, value):
    """D28: lecture_num was not validated (lecture-NaN)."""
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        _open_chat(page, f"course=AST101&lecture_num={value}")
        src = page.get_attribute("iframe[title='Lecture slides']", "src")
        assert "/slides/AST101/lecture-01/" in src
        expect(page.locator("select[aria-label=Lecture]")).to_have_value("1")


def test_a_slide_from_the_other_course_leaves_the_chat_in_its_own(browser, client):
    """D28: showSlide changed the course without onCourseChange, so the next
    message went to the other course. And no slide number is sent."""
    slide = {"type": "slide", "url": "/slides/AST102/lecture-02/index.html", "title": "Galaxies", "num": 5}
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        _open_chat(page)
        posted = _reply(net, "Galaxies are covered in AST102.", extra=[slide])
        _send(page, "What is a galaxy?")
        expect(page.locator("#messages >> text=Galaxies are covered in AST102.")).to_be_visible()
        expect(page.locator("select[aria-label=Course]")).to_have_value("AST101")
        expect(page.locator("iframe[title='Lecture slides']")).to_have_attribute(
            "src", re.compile("/slides/AST102/lecture-02/"))
        _send(page, "And dark matter?")
        expect(page.locator("#messages >> text=And dark matter?")).to_be_visible()
        fields = [urllib.parse.unquote(p) for p in posted]
        assert all('name="course"\r\n\r\nAST101' in f for f in fields), fields
        assert not any('name="slide_num"' in f for f in fields)


def test_a_lecture_the_page_knows_has_no_deck_is_never_requested(browser, client, monkeypatch):
    """The picker labelled a lecture "(no slides yet)" and then loaded its
    deck anyway: a 404 row and a dead-link email on every pick (the review
    of step 0.9). With a deck list on the page, the frame is skipped and the
    sentence shows; a listed deck is still requested."""
    from tutor import decks
    from tutor.db.models import ActivityEvent

    monkeypatch.setattr(decks, "available_decks", lambda *a, **k: {"AST101": [1], "AST102": []})
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        _open_chat(page, "course=AST101&lecture_num=3")
        expect(page.locator("[data-deck-missing]")).to_be_visible()
        expect(page.locator("[data-deck-missing]")).to_contain_text("lecture 3")
        assert page.locator("iframe[title='Lecture slides']").count() == 0
        assert not any("/slides/AST101/lecture-03/" in r for r in net.seen), net.seen
        page.select_option("select[aria-label=Lecture]", "1")
        page.wait_for_selector("iframe[title='Lecture slides']", state="attached")
        page.wait_for_timeout(300)
        assert any("/slides/AST101/lecture-01/" in r for r in net.seen), net.seen
        page.evaluate("() => window.tutorTrackFlush && window.tutorTrackFlush()")
        page.wait_for_timeout(300)
    db = client.app.dependency_overrides[__import__("tutor.auth.deps", fromlist=["get_db"]).get_db]()
    rows = [r for r in db.query(ActivityEvent).all() if r.name == "deck_missing"]
    # Lecture 3 was reported without a request; lecture 1 is listed but not
    # on disk here, so its load reported it the old way, through the frame.
    assert [(r.course, r.lecture_num) for r in rows] == [("AST101", 3), ("AST101", 1)]

