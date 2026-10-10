"""Plan step 4.1 (finding D22, decision 13's default): the admin Today panel.

D22: twelve tabs that did not wrap, an all-time stats row on every tab, and a
default view of every student by last seen; nothing answered "who is stuck or
absent, what is the class asking", and the page was unreadable on a phone.
Done when: the default admin view answers the three Monday questions.
"""
from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tests.browser_support import ORIGIN, open_page
from tutor.db.models import (
    ClassEnrollment, CourseClass, Message, MessageFeedback, Student,
)
from tutor.utctime import utcnow
from tests.helpers import make_session


@pytest.fixture()
def setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers import status

    status._reset_for_tests()
    db = make_session()
    prof = Student(id=1, email="prof@example.edu", name="Prof")
    ada = Student(id=2, email="ada@example.edu", name="Ada Lovelace")        # active
    bo = Student(id=3, email="bo@example.edu", name="Bo", login_count=2,      # went quiet
                 last_login=utcnow() - datetime.timedelta(days=20))
    cy = Student(id=4, email="cy@example.edu", name="Cy")                    # never started
    db.add_all([prof, ada, bo, cy])
    db.flush()
    cls = CourseClass(name="AST 101 A", course="AST101", term="Autumn 2026", instructor_id=1)
    db.add(cls)
    db.flush()
    for s in (ada, bo, cy):
        db.add(ClassEnrollment(class_id=cls.id, student_id=s.id))
    now = utcnow()
    for lec, n in ((4, 3), (3, 1)):
        for i in range(n):
            db.add(Message(student_id=2, role="user", content=f"lecture {lec} question {i}",
                           course="AST101", lecture_num=lec, created_at=now - datetime.timedelta(hours=i + 1)))
    db.add(Message(student_id=3, role="user", content="old question", course="AST101", lecture_num=2,
                   created_at=now - datetime.timedelta(days=20)))
    reply = Message(student_id=2, role="assistant", content="A wrong answer about eclipses.",
                    course="AST101", lecture_num=4, created_at=now - datetime.timedelta(minutes=30))
    db.add(reply)
    db.flush()
    db.add(MessageFeedback(message_id=reply.id, student_id=2, rating=-1, comment="This is wrong",
                           updated_at=now - datetime.timedelta(minutes=10)))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    from tutor.routers.admin import issue_admin_session
    client.cookies.set("admin_session", issue_admin_session())
    try:
        yield client, db, cls
    finally:
        app.dependency_overrides.clear()
        db.close()
        config.get_config.cache_clear()


def test_the_default_view_answers_the_monday_questions(setup):
    client, _db, cls = setup
    page = client.get("/admin").text
    assert "data-today" in page and "Students (" not in page       # not the old default
    card = page[page.index(f'data-today-class="{cls.id}"'):]
    # Who is active, who went quiet, who never started, with names that open
    # the instructor drill-down.
    assert "Active</dt>" in card and "Went quiet</dt>" in card and "Never started</dt>" in card
    assert f'href="/instructor?class_id={cls.id}&student=bo%40example.edu">Bo</a>' in card
    assert f'href="/instructor?class_id={cls.id}&student=cy%40example.edu">Cy</a>' in card
    assert f'href="/instructor?class_id={cls.id}"' in card
    # What the class is asking: lecture 4 first, with three questions.
    asked = card[card.index("Most asked about"):]
    assert asked.index("Lecture 4") < asked.index("Lecture 3")
    assert "(3 questions)" in asked and "(1 question)" in asked
    # The flagged reply, linked to the transcript at that message.
    assert "Replies students flagged (1, 1 unseen)" in page
    assert 'href="/feedback/' in page and "This is wrong" in page          # the one link (review step 0.4)
    # And a health dot that opens the Health tab.
    assert "data-health-dot" in page and 'href="/admin?tab=health"' in page


def test_the_outliers_card_ranks_from_the_rollup(setup):
    """Review step 0.9: the two highest and lowest per measure, from
    student_days, which the page refreshes before it reads."""
    from tutor.db.models import StudentDay

    client, db, cls = setup
    page = client.get("/admin").text
    assert db.query(StudentDay).count() >= 1                     # the refresh wrote this week
    card = page[page.index("data-today-outliers"):page.index("data-today-quotas")]
    assert "Where students stand, last 7 days" in card and "a rank, not a judgement" in card
    questions = card[card.index('data-outlier="questions"'):]
    assert f'href="/instructor?class_id={cls.id}&student=ada%40example.edu"' in questions
    assert "Ada Lovelace</a> (4)" in questions and "2 with none" in questions
    assert 'data-outlier="failed_logins"' in card


def test_the_counts_come_from_the_class_report(setup):
    client, _db, cls = setup
    card = client.get("/admin").text.split(f'data-today-class="{cls.id}"')[1]
    counts = [int(x) for x in
              __import__("re").findall(r'<dd class="text-2xl font-bold[^"]*">(\d+)</dd>', card)[:3]]
    assert counts == [1, 1, 1]                                     # Ada, Bo, Cy


def test_tabs_are_grouped_and_the_stats_row_stays_on_students(setup):
    client, _db, _cls = setup
    page = client.get("/admin").text
    for group in ("People", "Learning", "Tutor"):
        assert f'role="group" aria-label="{group}"' in page
    for key in ("today", "students", "classes", "access", "logins", "topics", "exercises",
                "misconceptions", "conversations", "supervision", "calibration", "activity", "health"):
        assert page.count(f'href="/admin?tab={key}"') >= 1, key
    assert 'href="/admin?tab=today"\n           class="inline-flex items-center min-h-[44px] text-white' in page
    assert "Exercise Attempts" not in page                          # no stats row on Today
    assert "Exercise Attempts" in client.get("/admin?tab=students").text
    assert "Exercise Attempts" not in client.get("/admin?tab=access").text


def test_no_classes_says_what_to_do(setup):
    client, db, cls = setup
    cls.archived = True
    db.commit()
    page = client.get("/admin").text
    assert "No open classes yet." in page and 'href="/admin?tab=classes"' in page


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_the_today_page_fits_the_screen(browser, setup, width, height):
    client, _db, _cls = setup
    with open_page(browser, client, width=width, height=height, clock=False) as (page, net):
        from tutor.routers.admin import issue_admin_session
        page.context.add_cookies([{"name": "admin_session", "value": issue_admin_session(), "url": ORIGIN}])
        page.goto(f"{ORIGIN}/admin")
        page.wait_for_selector("[data-today]")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
