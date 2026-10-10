"""Plan step 3.3 (findings D5, D17): the chat page on a phone.

D17: the chat header took two to four rows on a 390 px phone. D5: on a phone,
slides, progress, What's next, and the review nudge all sat behind the Slides
toggle, and a slide the tutor brought up gave no sign on the chat panel.
Done when: the header is under 60 px at 390 px; a slide event is visible
without toggling.

Runs the chat page in headless Chromium (tests/browser_support.py); skipped
where Playwright or a Chromium it can launch is missing.
"""
from __future__ import annotations

import re

import pytest

from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tutor.db.models import Message

SLIDE = {"type": "slide", "url": "/slides/AST101/lecture-04/index.html", "title": "Telescopes", "num": 7}
REPLY = "A telescope's mirror gathers the light."


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


def _open(browser, client, width, height=844):
    return open_page(browser, client, width=width, height=height, clock=False)


def _goto_chat(page, net):
    # A turn that brings up a slide, then answers.
    net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
        status=200, content_type="text/event-stream",
        body=sse([{"type": "turn", "id": "t1"}, SLIDE, {"type": "token", "text": REPLY},
                  {"type": "done", "full_text": REPLY}]))
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.wait_for_selector("#messages >> text=What is a lunar phase?")


def _header_height(page) -> float:
    return page.locator("header").bounding_box()["height"]


def _ask(page):
    page.fill("#msg-input", "Explain telescopes")
    page.click("button:has-text('Send')")
    page.wait_for_selector(f"#messages >> text={REPLY}")


def test_the_phone_header_is_one_row_under_60px(browser, client):
    with _open(browser, client, 390) as (page, net):
        _goto_chat(page, net)
        assert _header_height(page) < 60
        # The track chip and the lecture picker are off the phone header.
        assert not page.is_visible("header [data-track-chip]")
        assert page.locator("header select[aria-label=Lecture]").count() == 0
        # Still one row while the tutor is thinking, and in slides mode.
        page.evaluate("Alpine.$data(document.querySelector('[x-data]')).streaming = true")
        expect(page.locator("header >> text=Thinking...")).to_be_hidden()
        assert _header_height(page) < 60
        page.click("header button:has-text('Slides')")
        # The lecture picker shows with the slides it drives.
        expect(page.locator("select[aria-label=Lecture]")).to_be_visible()
        assert _header_height(page) < 60


def test_a_slide_in_chat_mode_shows_a_chip_and_a_badge(browser, client):
    with _open(browser, client, 390) as (page, net):
        _goto_chat(page, net)
        assert not page.is_visible("[data-slide-badge]")
        _ask(page)
        chip = page.locator("[data-slide-chip] button")
        expect(chip).to_be_visible()
        assert "Telescopes, slide 7" in chip.inner_text()
        assert chip.get_attribute("aria-label") == "Show the slide: Telescopes, slide 7"
        assert chip.bounding_box()["height"] >= 44
        expect(page.locator("[data-slide-badge]")).to_be_visible()
        # The chip opens the slides panel on that lecture and clears the dot.
        chip.click()
        # The slides panel, on that lecture (this test has no decks, so it
        # shows the missing-deck note in place of the frame).
        assert "/slides/AST101/lecture-04/" in page.get_attribute("iframe[title='Lecture slides']", "src")
        expect(page.locator("[data-deck-missing]")).to_be_visible()
        expect(page.locator("[data-deck-missing]")).to_contain_text("lecture 4")
        expect(page.locator("#messages")).to_be_hidden()
        expect(page.locator("[data-slide-badge]")).to_be_hidden()
        expect(page.locator("select[aria-label=Lecture]")).to_have_value("4")


def test_seeing_the_slides_first_clears_the_dot(browser, client):
    with _open(browser, client, 390) as (page, net):
        _goto_chat(page, net)
        _ask(page)
        expect(page.locator("[data-slide-badge]")).to_be_visible()
        page.click("header button:has-text('Slides')")
        expect(page.locator("[data-slide-badge]")).to_be_hidden()
        page.click("header button:has-text('Chat')")
        expect(page.locator("#messages")).to_be_visible()
        expect(page.locator("[data-slide-badge]")).to_be_hidden()


def test_the_ideas_sheet_carries_progress_and_the_track_on_phones(browser, client):
    with _open(browser, client, 390) as (page, net):
        _goto_chat(page, net)
        page.click("button[title='Ideas for what to ask']")
        expect(page.locator("#progress-panel-m")).to_contain_text("Your progress in AST 101")
        expect(page.locator("#progress-panel-m")).to_be_visible()
        link = page.locator("[data-track-link]")
        expect(link).to_be_visible()
        expect(link).to_contain_text("Order: Lecture order")
        assert link.get_attribute("href") == "/track-picker?course=AST101"


def test_desktop_keeps_the_side_by_side_layout(browser, client):
    with _open(browser, client, 1280, 800) as (page, net):
        _goto_chat(page, net)
        assert page.is_visible("header [data-track-chip]")
        assert page.is_visible("select[aria-label=Lecture]")     # in the slides bar
        expect(page.locator("#progress-panel")).to_contain_text("Your progress in AST 101")
        _ask(page)
        # The slide shows beside the chat: no chip, no toggle, no dot.
        expect(page.locator("iframe[title='Lecture slides']")).to_have_attribute(
            "src", re.compile("/slides/AST101/lecture-04/"))
        assert page.locator("[data-slide-chip]").count() == 1      # built, but hidden here
        expect(page.locator("[data-slide-chip] button")).to_be_hidden()
        expect(page.locator("[data-slide-badge]")).to_be_hidden()
        page.click("button[title='Ideas for what to ask']")
        expect(page.locator("text=Try asking")).to_be_visible()
        expect(page.locator("#progress-panel-m")).to_be_hidden()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
