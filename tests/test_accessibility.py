"""Plan step 3.4 (findings D8, D9, D10, D11, D12, D26): the accessibility pass.

D8: speak and thumbs buttons were 20 x 16 px, chips 30 px, prediction buttons
32 px. D9: the selects, the message, code, and problem boxes, and the
feedback box had no label. D10: nothing in the chat reached a screen reader.
D11: Tab left the help dialog. D12: the map's nodes and the track picker's
cards were mouse-only. D26: no motion setting was honoured.
Done when: the audit reports zero unlabeled controls and zero sub-44 px
targets on the chat.

Runs the pages in headless Chromium (tests/browser_support.py); skipped where
Playwright or a Chromium it can launch is missing.
"""
from __future__ import annotations

import json

import pytest

from tests.browser_support import ORIGIN, app_client, audit, expect, open_page, sse, student_db
from tutor.db.models import LearningGoal, Message, Track

REPLY = "Your loop stops one short. Check the bound on `i`."
TURN = [{"type": "turn", "id": "t1"},
        {"type": "slide", "url": "/slides/AST101/lecture-04/index.html", "title": "Loops", "num": 3},
        {"type": "eval", "score": 3, "correct": True},
        {"type": "prediction", "predicted": 2, "actual": 3},
        {"type": "token", "text": REPLY},
        {"type": "done", "full_text": REPLY, "message_id": 41}]      # a saved reply, as the server sends
STARTERS = {"course": "AST101", "starters": [
    {"label": "Teach me the phases of the Moon", "message": "Teach me the phases of the Moon", "kind": "next"},
    {"label": "Quiz me on the celestial sphere", "message": "Quiz me on the celestial sphere", "kind": "curated"}]}


@pytest.fixture
def db():
    s = student_db()
    s.add(Track(id="AST101-foundations", course="AST101", title="Foundations first",
                goal_ids_json="[]"))
    s.add_all([
        LearningGoal(id="AST101-L01-a", title="Celestial sphere", description="The sky as a sphere.", course="AST101",
                     lecture_num=1),
        LearningGoal(id="AST101-L02-b", title="Moon phases", description="Why the Moon changes shape.",
                     course="AST101", lecture_num=2),
        LearningGoal(id="AST102-L01-c", title="Brightness", description="How bright a star looks.", course="AST102",
                     lecture_num=1),
        Message(student_id=1, course="AST101", role="user", content="What is a lunar phase?"),
        Message(student_id=1, course="AST101", role="assistant",
                content="A lunar phase is how much of the lit half we see."),
    ])
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db, monkeypatch):
    # A configured voice, so the speak button is there to audit. The config
    # is cached, possibly while other modules were imported: clear it.
    from tutor.config import get_config

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-dummy")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice")
    get_config.cache_clear()
    with app_client(db) as c:
        yield c


def _chat(page, net):
    net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
        status=200, content_type="text/event-stream", body=sse(TURN))
    net.stubs[("GET", "/chat/starters")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(STARTERS))
    net.stubs[("POST", "/chat/feedback")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json", body='{"ok": true, "id": 1}')
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.wait_for_selector("#messages >> text=What is a lunar phase?")


def _open_everything(page):
    """A reply with its controls, the Ideas sheet, the code panel with its
    prediction strip, and the feedback reason box: every control the chat
    shows in normal use."""
    page.fill("#msg-input", "Is my loop right?")
    page.click("button:has-text('Send')")
    expect(page.locator("[id^=bubble-] .tts-btn")).to_have_count(1)
    page.click("button[aria-label='Something was wrong']")
    expect(page.locator(".fb-reason textarea")).to_be_visible()
    page.click("button[aria-label='Ideas for what to ask']")
    expect(page.locator("text=Teach me the phases of the Moon")).to_be_visible()
    page.click("button[aria-label='Submit code for feedback']")
    page.fill("#code-input", "for (int i = 0; i < n - 1; i++) {}")
    page.click("button:has-text('Evaluate')")
    expect(page.locator("button[aria-label^='I expect 3 out of 4']")).to_be_visible()


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_the_chat_has_no_unlabeled_controls_and_no_small_targets(browser, client, width, height):
    with open_page(browser, client, width=width, height=height, clock=False) as (page, net):
        _chat(page, net)
        _open_everything(page)
        found = audit(page)
        assert found["checked"] > 25
        assert found["unnamed"] == []
        assert found["small"] == []


def test_the_help_dialog_is_labelled_and_keeps_focus_inside(browser, client):
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        _chat(page, net)
        page.click("header summary")
        opener = page.locator("header nav button:has-text('Help') >> visible=true")
        opener.click()
        dialog = page.locator("#help-modal")
        expect(dialog).to_be_visible()
        expect(page.locator("button[aria-label='Close help']")).to_be_focused()
        found = audit(page, "#help-modal")
        assert found["unnamed"] == [] and found["small"] == []
        # Twenty Tabs forward and back never leave the dialog.
        inside = "document.getElementById('help-modal').contains(document.activeElement)"
        for key in ["Tab"] * 20 + ["Shift+Tab"] * 20:
            page.keyboard.press(key)
            assert page.evaluate(inside), key
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(opener).to_be_focused()          # back on the button that opened it


def test_replies_scores_and_errors_reach_the_live_region(browser, client):
    with open_page(browser, client, width=390, clock=False) as (page, net):
        _chat(page, net)
        region = page.locator("#sr-status[role=status]")
        page.fill("#msg-input", "Is my loop right?")
        page.click("button:has-text('Send')")
        expect(region).to_contain_text("Score: 3 out of 4, Correct")
        expect(region).to_contain_text("You predicted 2, scored 3")
        expect(region).to_contain_text(f"Tutor: {REPLY.replace('`', '')}")
        # An error is read out too.
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "turn", "id": "t2"},
                      {"type": "error", "message": "The tutor is not available right now."},
                      {"type": "done"}]))
        page.fill("#msg-input", "Again?")
        page.click("button:has-text('Send')")
        expect(region).to_contain_text("The tutor is not available right now.")


def test_track_cards_work_from_the_keyboard(browser, client):
    with open_page(browser, client, width=390, clock=False) as (page, net):
        page.goto(f"{ORIGIN}/track-picker?course=AST101")
        card = page.locator("#card-AST101-foundations")
        expect(card).to_have_attribute("aria-pressed", "false")
        card.focus()
        page.keyboard.press("Enter")
        expect(card).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#card-AST101-lecture-order")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#status-msg[role=status]")).to_contain_text("Order saved.")
        page.locator("#card-AST101-lecture-order").focus()
        page.keyboard.press(" ")
        expect(page.locator("#card-AST101-lecture-order")).to_have_attribute("aria-pressed", "true")
        found = audit(page, "main, #track-list")
        assert found["unnamed"] == []


def test_map_nodes_and_the_list_view_work_from_the_keyboard(browser, client):
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        page.goto(f"{ORIGIN}/map")
        nodes = page.locator("g.node[role=button][tabindex='0']")
        expect(nodes).to_have_count(3)
        labels = sorted(nodes.evaluate_all("els => els.map(e => e.getAttribute('aria-label'))"))
        assert labels[0].startswith("Brightness, Not started, AST 102 lecture 1")
        # Enter on a focused node opens its details and moves focus there;
        # Escape closes them and returns focus to the node.
        first = page.locator("g.node[aria-label^='Celestial sphere']")
        first.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#panel.open #panel-title")).to_have_text("Celestial sphere")
        expect(page.locator("#panel-title")).to_be_focused()
        close = page.locator("#panel-close")
        assert close.get_attribute("aria-label") == "Close the details"
        box = close.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
        page.keyboard.press("Escape")
        expect(page.locator("#panel.open")).to_have_count(0)
        expect(first).to_be_focused()
        # The list view: every goal as a button, by course and lecture.
        page.click("#view-toggle")
        expect(page.locator("#view-toggle")).to_have_attribute("aria-pressed", "true")
        items = page.locator("#goal-list button")
        expect(items).to_have_count(3)
        assert page.locator("#goal-list h2").all_inner_texts() == ["AST 101", "AST 102"]
        expect(page.locator("#graph-svg")).to_be_hidden()
        items.nth(1).focus()
        page.keyboard.press("Enter")
        expect(page.locator("#panel.open #panel-title")).to_have_text("Moon phases")
        found = audit(page, "#goal-list")
        assert found["unnamed"] == [] and found["small"] == []


def test_reduced_motion_stops_animation(browser, client):
    probe = """() => {
        const d = document.createElement('div');
        d.className = 'cursor';
        document.body.appendChild(d);
        return getComputedStyle(d, '::after').animationDuration;
    }"""
    with open_page(browser, client, width=390, clock=False) as (page, net):
        _chat(page, net)
        assert page.evaluate(probe) == "0.8s"
    with open_page(browser, client, width=390, clock=False, reduced_motion="reduce") as (page, net):
        _chat(page, net)
        assert page.evaluate(probe) != "0.8s"
        # The map lays itself out before it shows: nothing drifts.
        page.goto(f"{ORIGIN}/map")
        where = "() => [...document.querySelectorAll('g.node')].map(n => n.getAttribute('transform'))"
        before = page.evaluate(where)
        page.wait_for_timeout(300)
        assert page.evaluate(where) == before and all(before)
