"""Plan step 3.6 (findings D24, D25): the words a first-time student reads.

D24: "one rung at a time", a "Lecture Order" chip with no context,
Locked/Seen/Unlocked/Mastered, raw "Network error: TypeError" and "Error 500"
strings, "Hi jsmith3" from the email, and a track picker whose title and
heading disagreed. D25: the progress panel counted both courses under one
course's heading and cut titles without an ellipsis.
Done when: the copy is reviewed against the help page.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests.browser_support import ORIGIN, app_client, expect, open_page, student_db
from tutor.db.models import LearningGoal, Message, Student, StudentProgress, Track
from tutor.routers.onboarding import first_name

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "tutor" / "web" / "templates"
LONG_TITLE = "Why the Moon shows phases and how solar eclipses line up"


@pytest.fixture
def db():
    s = student_db()
    s.add_all([
        LearningGoal(id="AST101-a", title=LONG_TITLE, description="", course="AST101", lecture_num=1),
        LearningGoal(id="AST101-b", title="Moon phases", description="", course="AST101", lecture_num=2),
        LearningGoal(id="AST102-a", title="Parallax", description="", course="AST102", lecture_num=1),
        LearningGoal(id="AST102-b", title="Galaxies", description="", course="AST102", lecture_num=2),
        LearningGoal(id="AST102-c", title="Dark matter", description="", course="AST102", lecture_num=3),
    ])
    # Real track orders, so "What's next" lists the frontier. (Until plan
    # step 5.3 the list showed only because a mastered goal with no evidence
    # date was review-due forever, finding B20.)
    s.get(Track, "AST101-lecture-order").goal_ids_json = json.dumps(["AST101-a", "AST101-b"])
    s.get(Track, "AST102-lecture-order").goal_ids_json = json.dumps(["AST102-a", "AST102-b", "AST102-c"])
    s.commit()
    s.add_all([
        StudentProgress(student_id=1, goal_id="AST101-b", status="mastered"),
        StudentProgress(student_id=1, goal_id="AST102-a", status="mastered"),
        StudentProgress(student_id=1, goal_id="AST102-b", status="unlocked"),
        Message(student_id=1, course="AST101", role="user", content="What is a lunar phase?"),
        Message(student_id=1, course="AST102", role="user", content="What is parallax?"),
        Message(student_id=1, course="AST102", role="user", content="What is a galaxy?"),
    ])
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db):
    with app_client(db) as c:
        yield c


@pytest.mark.parametrize("email,name,greeting", [
    ("jsmith3@example.edu", "jsmith3", ""),               # magic-link sign-up: no display name
    ("jsmith3@example.edu", "", ""),
    ("ada@example.edu", "Ada Lovelace", "Ada"),           # a real name, from Google
    ("ada@example.edu", "Ada", "Ada"),                    # the local part, but written by a person
])
def test_the_greeting_uses_a_real_name_or_none(email, name, greeting):
    assert first_name(Student(email=email, name=name)) == greeting


def test_no_one_rung_at_a_time_left():
    """The help page, the welcome page, and the chat all say "one hint at a
    time"."""
    sources = [TEMPLATES / n for n in ("chat.html", "welcome.html", "_help_content.html")]
    for path in sources:
        text = " ".join(path.read_text().lower().split())
        assert "rung" not in text, path.name
        assert "one hint at a time" in text, path.name


def test_the_progress_panel_counts_its_own_course(client):
    """D25: messages and goals spanned both courses under a AST101 heading."""
    panel_ast101 = client.get("/chat/progress?course=AST101").text
    assert "Your progress in AST 101" in panel_ast101
    assert "1 solid" in panel_ast101 and "0 practiced" in panel_ast101 and "of 2" in panel_ast101
    assert "Messages: <span class=\"font-semibold\">1</span>" in panel_ast101
    panel_ast102 = client.get("/chat/progress?course=AST102").text
    assert "Your progress in AST 102" in panel_ast102
    assert "1 solid" in panel_ast102 and "1 practiced" in panel_ast102 and "of 3" in panel_ast102
    assert "Messages: <span class=\"font-semibold\">2</span>" in panel_ast102
    for panel in (panel_ast101, panel_ast102):
        assert "mastered" not in panel and "unlocked" not in panel
        assert "Change the order" in panel


def test_long_titles_end_in_an_ellipsis_not_a_cut(client, db):
    from tutor.agent.state import record_topic_access

    record_topic_access(db, 1, 1, None, LONG_TITLE)
    db.commit()
    panel = client.get("/chat/progress?course=AST101").text
    # Whole, with the CSS doing the cutting (it was cut at 40 characters).
    assert f'<li class="truncate" title="{LONG_TITLE}">L01: {LONG_TITLE}</li>' in panel
    assert '<span class="block truncate font-medium' in panel         # What's next titles too


def test_the_track_picker_has_one_name(client):
    page = client.get("/track-picker?course=AST101").text
    assert "<title>Astra — Order of suggestions</title>" in page
    assert '<h1 class="text-2xl font-bold text-gray-900 mb-1">Order of suggestions</h1>' in page
    assert "Save this order and go to the chat" in page


def test_the_map_uses_the_plain_status_words(client):
    page = client.get("/map").text
    for word in ("Not started", "Started", "Practiced", "Solid"):
        assert f"</span>{word}</div>" in page, word
    for old in (">Locked<", ">Unlocked<", ">Mastered<", ">Seen<"):
        assert old not in page


def test_the_order_chip_says_what_it_orders(browser, client):
    with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
        page.goto(f"{ORIGIN}/chat?course=AST101")
        expect(page.locator("header [data-track-chip]")).to_have_text("🧭 Order: Lecture order")


def test_a_refused_message_reads_plainly_and_comes_back(browser, client):
    """D24: a failed send showed "Error 500: Internal Server Error"."""
    with open_page(browser, client, clock=False) as (page, net):
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=500, content_type="text/plain", body="Internal Server Error")
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=What is a lunar phase?")
        page.fill("#msg-input", "Explain parallax")
        page.click("button:has-text('Send')")
        note = page.locator("#messages [role=alert]").last
        expect(note).to_contain_text("The tutor is having trouble right now")
        assert "Internal Server Error" not in page.inner_text("#messages")
        expect(page.locator("#msg-input")).to_have_value("Explain parallax")
