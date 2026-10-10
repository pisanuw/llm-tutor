"""Plan step 3.2 (finding D4): a dropped connection keeps the conversation.

Before the fix, a reply that could not reconnect within 90 seconds fell back
to reloading the history, which cleared every bubble before fetching and
swallowed the failure: an offline student saw only the welcome bubble, and the
"your answer was saved" message could never run.
Done when: offline after a drop leaves the conversation visible with a retry.

The chat page runs in headless Chromium. Its requests reach the app through
Playwright's router and a TestClient; the test cuts the network by failing
them and by emulating offline, so ``navigator.onLine`` and the ``online``
event behave as on a phone. A fake clock skips the reconnect window. Skipped
where Playwright or a Chromium it can launch is missing (CI has neither yet).
"""
from __future__ import annotations

import pytest

from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tutor.db.models import Message

QUESTION = "Explain parallax"
FIRST = "Parallax is the shift of a near star "
REST = "against far ones as Earth orbits the Sun."
REPLY = FIRST + REST
# The turn as the server buffers it; the first POST delivers the turn id and
# the first token, then the connection drops.
EVENTS = [{"type": "token", "text": FIRST}, {"type": "token", "text": REST},
          {"type": "done", "full_text": REPLY, "message_id": 1}]


@pytest.fixture
def db():
    s = student_db()
    s.add_all([
        Message(student_id=1, course="AST101", role="user", content="What is a lunar phase?"),
        Message(student_id=1, course="AST101", role="assistant",
                content="A lunar phase is how much of the lit half we see."),
        Message(student_id=1, course="AST102", role="user", content="What is a galaxy?"),
        Message(student_id=1, course="AST102", role="assistant",
                content="Billions of stars bound by gravity."),
    ])
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db):
    with app_client(db) as c:
        yield c


@pytest.fixture
def chat(browser, client):
    """(page, net) at phone width, before the chat page opens."""
    with open_page(browser, client) as (page, net):
        yield page, net


def _open(page):
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.wait_for_selector("#messages >> text=What is a lunar phase?")


def _drop_after_first_token(net, resume=None, offline=False):
    """The POST streams the turn id and one token, then the connection ends,
    taking the network with it when ``offline``. ``resume`` answers
    GET /chat/message/t1 (default: the rest of the turn)."""
    def first(route, url):
        if offline:
            net.go_offline()
        route.fulfill(status=200, content_type="text/event-stream",
                      body=sse([{"type": "turn", "id": "t1"}, EVENTS[0]]))

    net.stubs[("POST", "/chat/message")] = first

    def rest(route, url):
        offset = int(dict(p.split("=") for p in url.query.split("&"))["offset"])
        route.fulfill(status=200, content_type="text/event-stream", body=sse(EVENTS[offset:]))

    net.stubs[("GET", "/chat/message/t1")] = resume or rest


def _send(page, text=QUESTION):
    page.fill("#msg-input", text)
    page.click("button:has-text('Send')")


def _texts(page) -> str:
    return page.inner_text("#messages")


def _drop_past_the_window(page, net, resume=None):
    """Send, lose the connection after the first token, and stay offline past
    the automatic reconnect window."""
    _open(page)
    _drop_after_first_token(net, resume, offline=True)
    _send(page)
    page.wait_for_selector("#messages >> text=reconnecting")
    page.clock.fast_forward("02:00")
    page.wait_for_selector("[data-dropped]")


def test_offline_past_the_window_keeps_every_bubble_and_a_retry(chat):
    page, net = chat
    _drop_past_the_window(page, net)
    text = _texts(page)
    # The earlier conversation, the question, and what streamed are all still there.
    for kept in ("What is a lunar phase?", "A lunar phase is how much", QUESTION, FIRST.strip()):
        assert kept in text, kept
    note = page.inner_text("[data-dropped]")
    assert "You are offline" in note and "Nothing is lost" in note
    assert page.is_visible("[data-dropped] button:has-text('Retry')")
    assert page.query_selector("#history-notice") is None
    expect(page.locator("button:has-text('Send')")).to_be_enabled()   # the lock is released
    # The fallback tried the saved conversation before settling on the bubble.
    assert any(s.startswith("GET /chat/history") for s in net.seen[-3:])


def test_back_online_the_reply_finishes_by_itself(chat):
    page, net = chat
    _drop_past_the_window(page, net)
    net.go_online()                     # the online event runs the retry
    page.wait_for_selector(f"#messages >> text={REST}")
    page.wait_for_selector("[data-dropped]", state="detached")
    bubble = page.locator("[id^=bubble-]").last
    expect(bubble.locator(".fb-wrap")).to_have_count(1)     # a finished reply
    assert REPLY.strip() in bubble.inner_text()
    assert "What is a lunar phase?" in _texts(page)


def test_retry_while_still_offline_keeps_the_bubble(chat):
    page, net = chat
    _drop_past_the_window(page, net)
    before = len(net.seen)
    page.click("[data-dropped] button:has-text('Retry')")
    page.wait_for_function(
        "document.querySelector('[data-dropped]') && "
        "!document.querySelector('#messages .cursor')")
    tried = net.seen[before:]
    assert "GET /chat/message/t1?offset=1" in tried
    assert any(s.startswith("GET /chat/history") for s in tried)
    text = _texts(page)
    assert "What is a lunar phase?" in text and FIRST.strip() in text
    assert page.is_visible("[data-dropped] button:has-text('Retry')")


def test_after_the_stream_expired_retry_shows_the_saved_conversation(chat, db):
    page, net = chat
    gone = lambda route, url: route.fulfill(status=404, content_type="application/json",  # noqa: E731
                                            body='{"error": "unknown turn"}')
    _drop_past_the_window(page, net, resume=gone)
    # Meanwhile the server finished the turn and saved it.
    db.add_all([Message(student_id=1, course="AST101", role="user", content=QUESTION),
                Message(student_id=1, course="AST101", role="assistant", content=REPLY)])
    db.commit()
    net.go_online()
    page.wait_for_selector("[data-dropped]", state="detached")
    page.wait_for_selector(f"#messages >> text={REST}")
    text = _texts(page)
    assert "What is a lunar phase?" in text and text.count(QUESTION) == 1


def test_a_course_switch_while_offline_clears_the_other_course(chat):
    page, net = chat
    _open(page)
    net.go_offline()
    page.select_option("select[aria-label=Course]", "AST102")
    page.wait_for_selector("#history-notice")
    assert "offline" in page.inner_text("#history-notice")
    # The AST101 bubbles must not sit under the AST102 heading.
    assert "What is a lunar phase?" not in _texts(page)
    net.go_online()                     # the notice's retry runs by itself
    page.wait_for_selector("#messages >> text=What is a galaxy?")
    assert page.query_selector("#history-notice") is None


def test_a_failed_first_load_says_so_and_retry_loads_it(chat):
    page, net = chat
    net.stubs[("GET", "/chat/history")] = lambda route, url: route.fulfill(
        status=500, content_type="application/json", body='{"detail": "boom"}')
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.wait_for_selector("#history-notice")
    assert "could not load" in page.inner_text("#history-notice")
    del net.stubs[("GET", "/chat/history")]
    page.click("#history-notice button:has-text('Retry')")
    page.wait_for_selector("#messages >> text=What is a lunar phase?")
    assert page.query_selector("#history-notice") is None


def test_a_message_that_never_left_goes_back_in_the_box(chat):
    page, net = chat
    _open(page)
    net.stubs[("POST", "/chat/message")] = lambda route, url: route.abort("connectionreset")
    _send(page)
    page.wait_for_selector("#messages >> text=Could not reach Astra")
    assert page.input_value("#msg-input") == QUESTION
    # The question is back in the box, not also shown as sent.
    assert QUESTION not in _texts(page)


def test_a_stream_that_closes_without_done_reconnects(chat):
    page, net = chat
    _open(page)
    _drop_after_first_token(net)        # online: the resume delivers the rest
    _send(page)
    page.wait_for_selector("#messages >> text=reconnecting")
    page.clock.fast_forward("00:02")
    page.wait_for_selector(f"#messages >> text={REST}")
    bubble = page.locator("[id^=bubble-]").last
    expect(bubble.locator(".fb-wrap")).to_have_count(1)
    assert "reconnecting" not in _texts(page)
