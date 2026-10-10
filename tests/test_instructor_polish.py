"""Plan step 4.3 (findings D30, B26): instructor polish.

D30: "Replies students flagged as wrong: 1" had no link, "Last active" had no
year, a partial reply kept speak and thumbs controls in history, and a class
label with a quote or a backslash broke the delete confirmation. B26: most
exercises have no tests, so instructors should see which grades came from
running tests and which from the tutor's rubric. Also: plain-language
mastery tiers, and an "all my classes" view.
Done when: screenshots at both widths (the browser test takes them; the
assertions below pin the content).
"""
from __future__ import annotations

import datetime
import json
import re

import pytest

from tests.browser_support import ORIGIN, app_client, expect, open_page
from tutor import classes as classes_mod
from tutor.auth.access import set_instructor
from tutor.db.models import (
    Message, MessageFeedback, Student, StudentPath, Track, TurnEvent, WhitelistEntry,
)
from tutor.utctime import utcnow
from tests.helpers import make_session


@pytest.fixture
def db():
    s = make_session()
    s.add_all([
        Student(id=1, email="teach@example.edu", name="Terry Teacher"),
        Student(id=2, email="kim@example.edu", name="Kim Keen"),
        Student(id=3, email="quinn@example.edu", name="Quinn Quiet"),
        WhitelistEntry(email="teach@example.edu"), WhitelistEntry(email="kim@example.edu"),
        Track(id="AST101-lecture-order", course="AST101", title="Lecture order", goal_ids_json="[]"),
    ])
    s.commit()
    s.add(StudentPath(student_id=2, course="AST101", track_id="AST101-lecture-order"))
    set_instructor(s, "teach@example.edu", True)
    first = classes_mod.create_class(s, name="AST 101 A", course="AST101", instructor_email="teach@example.edu",
                                     term="Autumn 2026", created_by="teach@example.edu")
    second = classes_mod.create_class(s, name="AST 101 B", course="AST101", instructor_email="teach@example.edu",
                                      term="Autumn 2026", created_by="teach@example.edu")
    classes_mod.enroll(s, first.id, "kim@example.edu")
    classes_mod.enroll(s, second.id, "quinn@example.edu")
    now = utcnow()
    s.add(Message(student_id=2, role="user", content="what is a telescope for?", course="AST101",
                  lecture_num=4, created_at=now - datetime.timedelta(hours=3)))
    reply = Message(student_id=2, role="assistant", content="A telescope makes things bigger.",
                    course="AST101", lecture_num=4, created_at=now - datetime.timedelta(hours=3))
    s.add(reply)
    s.flush()
    s.add(MessageFeedback(message_id=reply.id, student_id=2, rating=-1, comment="Not right",
                          updated_at=now - datetime.timedelta(hours=2)))
    for source in ("test", "llm-opinion", "llm-opinion", "unavailable"):
        s.add(TurnEvent(student_id=2, kind="exercise", ts=now - datetime.timedelta(hours=1),
                        verdicts_json=json.dumps({"score": 3.0, "passed": True, "eval_source": source})))
    s.commit()
    s.info["classes"] = (first.id, second.id)
    s.info["reply_id"] = reply.id
    yield s
    s.close()


@pytest.fixture
def teacher(db):
    with app_client(db, student_id=1) as c:
        yield c


def test_flagged_replies_are_listed_and_linked(teacher, db):
    first, _ = db.info["classes"]
    page = teacher.get(f"/instructor?class_id={first}").text
    assert '<a href="#flagged"' in page                               # the count links down
    link = (f'href="/instructor/transcript/view?class_id={first}&amp;student=kim%40example.edu'
            f'&amp;msg={db.info["reply_id"]}#msg-{db.info["reply_id"]}"')
    assert link in page and "Not right" in page and "A telescope makes things bigger." in page
    detail = teacher.get(f"/instructor?class_id={first}&student=kim%40example.edu").text
    assert f'id="msg-{db.info["reply_id"]}"' in detail                # the anchor it lands on


def test_dates_carry_the_year(teacher, db):
    first, _ = db.info["classes"]
    year = str(utcnow().year)
    page = teacher.get(f"/instructor?class_id={first}").text
    roster = page[page.index("<h2 class=\"font-semibold text-gray-800\">Roster</h2>"):]
    assert re.search(rf"{year}-\d\d-\d\d", roster)
    detail = teacher.get(f"/instructor?class_id={first}&student=kim%40example.edu").text
    assert re.search(rf"{year}-\d\d-\d\d \d\d:\d\d UTC", detail)


def test_tiers_are_plain_and_grading_sources_show(teacher, db):
    first, _ = db.info["classes"]
    page = teacher.get(f"/instructor?class_id={first}").text
    for old in ("Provisional mastery (quiz tier)", "Mastery confirmed by spaced retrieval",
                "Mini-exams", "Goals mastered"):
        assert old not in page, old
    assert "Topics solid (passed a quiz or strong exercises)" in page
    assert "Of those topics, confirmed by a later review" in page
    assert "Exercise grading:" in page
    assert "1 by running tests, 2 by the tutor's rubric (a model's opinion)" in page   # unavailable skipped
    detail = teacher.get(f"/instructor?class_id={first}&student=kim%40example.edu").text
    assert "(1 by tests, 2 by the rubric)" in detail


def test_all_my_classes(teacher, db):
    first, second = db.info["classes"]
    page = teacher.get("/instructor?view=all").text
    assert "All my classes</h1>" in page and "data-overview" in page
    for cid in (first, second):
        assert re.search(rf'href="/instructor\?class_id={cid}&(amp;)?days=7"', page), cid
    row = page[re.search(rf"class_id={first}&(amp;)?days=7", page).start():]
    assert re.findall(r'<td class="px-3 py-2 text-right">(\d+)</td>', row)[:6] == ["1", "1", "0", "0", "1", "1"]
    assert 'data-all-classes' in teacher.get(f"/instructor?class_id={first}").text


def test_a_class_label_with_quotes_cannot_break_the_delete_confirmation(monkeypatch):
    """The label reaches the dialog through a data attribute, never through a
    JavaScript string."""
    import pathlib

    tpl = (pathlib.Path(__file__).resolve().parent.parent / "tutor" / "web" / "templates"
           / "admin.html").read_text()
    form = tpl[tpl.index('action="/admin/classes/delete"'):]
    form = form[:form.index(">")]
    assert 'data-confirm="Delete {{ c.label }}?' in form
    assert 'onsubmit="return confirm(this.dataset.confirm)"' in form


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_instructor_page_at_both_widths(browser, teacher, db, width, height):
    first, _ = db.info["classes"]
    with open_page(browser, teacher, width=width, height=height, clock=False) as (page, net):
        page.goto(f"{ORIGIN}/instructor?class_id={first}")
        expect(page.locator("#flagged")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.click("#flagged a")
        expect(page.locator(f"#msg-{db.info['reply_id']}")).to_be_visible()
        page.goto(f"{ORIGIN}/instructor?view=all")
        expect(page.locator("[data-overview]")).to_be_visible()


def test_a_partial_reply_in_history_gets_no_reply_controls(browser, db):
    db.add(Message(student_id=2, role="assistant", course="AST101",
                   content="Start with where the Sun is.\n\n[Reply incomplete: the tutor ran into a problem.]"))
    db.commit()
    with app_client(db, student_id=2) as kim:
        with open_page(browser, kim, clock=False) as (page, net):
            page.goto(f"{ORIGIN}/chat?course=AST101")
            page.wait_for_selector("#messages >> text=Start with where the Sun is.")
            bubble = page.locator("[id^=hist-]", has_text="Start with where the Sun is.")
            assert bubble.locator(".fb-wrap").count() == 0
            ok = page.locator("[id^=hist-]", has_text="A telescope makes things bigger.")
            assert ok.locator(".fb-wrap").count() == 1
