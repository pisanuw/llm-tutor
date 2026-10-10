"""The "Thinking ..." line (latency plan): from the moment a
message is sent until the reply's first words arrive, the bubble's row shows
"Thinking" and gains a dot every half second, so the student knows the
message was received. It goes when the reply starts and is removed when the
reply is finished.

Runs in headless Chromium (tests/browser_support.py); skips where Playwright
or a Chromium it can launch is missing.
"""
from __future__ import annotations

import re

import pytest

from tests.browser_support import ORIGIN, app_client, open_page, sse, student_db


@pytest.fixture
def client():
    db = student_db()
    with app_client(db) as c:
        yield c
    db.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_thinking_shows_until_the_reply_starts(browser, client, width):
    with open_page(browser, client, width=width, height=800) as (page, net):
        _check(page, net)


def _check(page, net):
    held = []
    net.stubs[("POST", "/chat/message")] = lambda route, url: held.append(route)
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.fill("#msg-input", "what is parallax?")
    page.click("button:has-text('Send')")

    note = page.locator(".thinking")
    page.wait_for_function("() => /^Thinking \\.{3,}$/.test("
                           "document.querySelector('.thinking')?.textContent || '')")
    first = note.text_content()
    page.wait_for_timeout(1200)
    later = note.text_content()
    assert re.fullmatch(r"Thinking \.+", later) and len(later) > len(first)
    assert note.is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    text = "Parallax is the shift of a near star."
    held[0].fulfill(status=200, content_type="text/event-stream",
                    body=sse([{"type": "turn", "id": "t1"}, {"type": "token", "text": text},
                              {"type": "done", "full_text": text}]))
    page.wait_for_selector(f"#messages >> text={text}")
    page.wait_for_function("() => !document.querySelector('.thinking')")
