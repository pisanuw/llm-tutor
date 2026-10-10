"""What the adversarial review of bulletproofing Stage 3 found
(its section 5): a refused question lost
on reload, Send held forever by a lapsed session, a notice naming the
limit that lifts first, a starter or a retried Evaluate overwriting or
doubling text, the server counting line breaks twice, a review answer lost
or graded twice, and the smaller edges around them.
"""
from __future__ import annotations

import datetime
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tutor.agent import state, usage
from tutor.db.models import (
    LearningGoal, Message, Student, StudentProgress, TurnEvent, WhitelistEntry,
)
from tutor.utctime import utcnow
from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tests.helpers import login_cookie, make_session


@pytest.fixture
def db():
    s = make_session(Student(id=1, email="ada@example.edu", name="Ada"), WhitelistEntry(email="@example.edu", added_by="t"))
    yield s
    s.close()


@pytest.fixture
def web(db):
    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session_token", login_cookie(db, 1))
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def test_the_notice_names_the_limit_that_lifts_last(db, web, monkeypatch):
    from tutor import quotas

    monkeypatch.setenv("DAILY_EXCHANGE_LIMIT", "2")
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    quotas.save_quotas(db, {"day": 0, "week": 1.0, "month": 0})
    for _ in range(2):
        state.record_turn_event(db, student_id=1, kind="chat", cost_usd=0.60)
    db.commit()
    notice = usage.over_caps(db, 1)
    assert notice.startswith("That is this week's tutor budget used up.")       # not "midnight"
    assert web.get("/chat/quota").json()["blocked"] == notice
    windows = sorted(h.window for h in quotas.hits(db, utcnow() - datetime.timedelta(days=1)))
    assert windows == ["turns-day", "week"]                                     # both stops recorded


def test_the_server_counts_a_line_break_once(db, web, monkeypatch):
    import tutor.agent.turn as turn_mod
    from tutor.routers.chat import MAX_MESSAGE_CHARS

    async def fake_run_turn(**kw):
        yield {"type": "done", "full_text": "ok"}

    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)
    line = "x" * 99 + "\r\n"                     # what a browser's form encoding sends
    text = (line * (MAX_MESSAGE_CHARS // 100))[:-2]
    assert len(text.replace("\r\n", "\n")) < MAX_MESSAGE_CHARS < len(text)
    assert web.post("/chat/message", data={"message": text, "course": "AST101"}).status_code == 200


def _due_goal(db, goal="G1"):
    db.add(LearningGoal(id=goal, title="Eclipses", description="", course="AST101", lecture_num=2))
    db.flush()
    db.add(StudentProgress(student_id=1, goal_id=goal, status="unlocked", mastery_score=0.7,
                           fsrs_stability=2.0, fsrs_difficulty=5.0, fsrs_reps=1,
                           fsrs_due=utcnow() - datetime.timedelta(hours=2),
                           last_evidence_at=utcnow() - datetime.timedelta(days=3)))
    db.commit()


PROBE = {"id": "p1", "question": "What is an eclipse?", "rubric": "one body shadows another"}
GRADED = {"score": 3, "feedback": "Right.", "usage": {"in_tokens": 10, "out_tokens": 5}}


def test_a_review_answer_is_graded_once_while_the_first_is_running(tmp_path, monkeypatch):
    """Two Checks of one answer (a retry after a dropped connection) graded
    it twice, billed twice and moved the schedule twice."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tutor.agent import mastery
    from tutor.auth.deps import get_db
    from tutor.db.models import Base
    from tutor.main import app

    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as seed:
        seed.add_all([Student(id=1, email="ada@example.edu", name="Ada"), WhitelistEntry(email="@example.edu", added_by="t")])
        seed.commit()
        _due_goal(seed)
        token = login_cookie(seed, 1)

    def get_db_real():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    started = threading.Event()

    def slow_grader(*a, **k):
        started.set()
        time.sleep(1.0)
        return GRADED

    app.dependency_overrides[get_db] = get_db_real
    results = []
    try:
        client = TestClient(app)
        client.cookies.set("session_token", token)
        form = {"goal_id": "G1", "probe_id": "p1", "answer": "a shadow"}
        with patch.object(mastery, "_question_pool", return_value=[PROBE]), \
             patch.object(mastery, "grade_probe", slow_grader):
            first = threading.Thread(target=lambda: results.append(client.post("/review/grade", data=form)))
            first.start()
            assert started.wait(5)
            second = client.post("/review/grade", data=form)
            first.join(10)
    finally:
        app.dependency_overrides.clear()
    assert second.status_code == 409 and second.json()["error"] == "in_progress"
    assert results[0].status_code == 200
    with factory() as s:
        assert s.query(TurnEvent).filter_by(kind="review").count() == 1
    engine.dispose()


def test_review_dates_are_local_and_an_unknown_question_says_what_to_do(db, web):
    from tutor.agent import mastery

    _due_goal(db)
    with patch.object(mastery, "_question_pool", return_value=[PROBE]), \
         patch.object(mastery, "grade_probe", return_value=GRADED):
        r = web.post("/review/grade", data={"goal_id": "G1", "probe_id": "nope", "answer": "x"})
        assert r.status_code == 404 and "changed since the page loaded" in r.json()["feedback"]
        # Due at 03:00 UTC on the 9th is 8 PM PDT (the class's zone) on the 8th.
        progress = db.query(StudentProgress).one()
        progress.fsrs_due = datetime.datetime(2026, 10, 9, 3, 0)
        db.add(TurnEvent(student_id=1, kind="review", goal_ids_json='["G1"]', verdicts_json='{"score": 3}',
                         ts=utcnow()))
        db.commit()
        r = web.post("/review/grade", data={"goal_id": "G1", "probe_id": "p1", "answer": "x"})
        if utcnow() < datetime.datetime(2026, 10, 9, 3, 0):
            assert r.json()["error"] == "already_checked" and r.json()["next_due"] == "2026-10-08"


def test_a_failed_photo_read_gives_its_slot_back_and_the_limit_says_when(db, web, monkeypatch):
    from tutor.agent import vision

    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")
    monkeypatch.setattr(vision, "DAILY_PHOTO_LIMIT", 1)
    monkeypatch.setattr(vision, "transcribe_image", lambda d, m: {"ok": False, "error": "Could not read."})
    for _ in range(3):                           # failures before the model: no slot spent
        assert web.post("/chat/transcribe-photo", files={"image": ("p.png", png, "image/png")}).status_code == 422
    monkeypatch.setattr(vision, "transcribe_image", lambda d, m: {"ok": True, "code": "x", "usage": {"in_tokens": 5}})
    assert web.post("/chat/transcribe-photo", files={"image": ("p.png", png, "image/png")}).status_code == 200
    r = web.post("/chat/transcribe-photo", files={"image": ("p.png", png, "image/png")})
    error = r.json()["error"]
    assert r.status_code == 429 and "The next one opens at" in error
    assert "PDT" in error or "PST" in error                  # the class's zone (fixture: America/Los_Angeles)


def test_the_summarizer_is_ledgered(db):
    from tutor.agent import memory

    for i in range(memory.RECENT_K + memory.SUMMARIZE_EVERY + 2):
        db.add(Message(student_id=1, role="user" if i % 2 == 0 else "assistant", content=f"m{i}", course="AST101"))
    db.commit()
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text="A summary.")]
    resp.usage = MagicMock(input_tokens=3000, output_tokens=200, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)
    client.messages.create.return_value = resp
    assert memory.maybe_summarize(db, 1, "AST101", client=client) is True
    (row,) = db.query(TurnEvent).filter_by(kind="summary").all()
    assert row.in_tokens == 3000 and row.cost_usd > 0


def test_the_supervision_tab_words_a_count_cap_as_a_cap(db, monkeypatch):
    from tutor.routers import admin

    monkeypatch.setenv("DAILY_REVIEW_LIMIT", "1")
    state.record_turn_event(db, student_id=1, kind="review")
    db.commit()
    usage.over_review_caps(db, 1)
    db.commit()
    from tutor.templating import templates

    html = templates.get_template("admin.html").module  # noqa: F841 (the template compiles)
    from tutor import quotas

    hits = quotas.hits(db, utcnow() - datetime.timedelta(days=1))
    assert [h.window for h in hits] == ["reviews-day"] and not hits[0].limit_usd
    source = open(admin.__file__.replace("routers/admin.py", "web/templates/admin.html")).read()
    assert "{% else %}{{ h.window }} cap{% endif %}</li>" in source


def test_a_sign_in_refusal_keeps_where_the_student_was_going(db, web):
    web.cookies.clear()
    r = web.post("/auth/magic-link", data={"email": "not-an-address", "next": "/review?course=AST101"},
                 headers={"accept": "text/html"})
    assert r.status_code == 400 and 'name="next" value="/review?course=AST101"' in r.text
    r = web.post("/auth/magic-link", data={"email": "someone@gmail.com", "next": "/review?course=AST101"},
                 headers={"accept": "text/html"})
    assert r.status_code == 403 and 'name="next" value="/review?course=AST101"' in r.text


# ---------------------------------------------------------------------------
# In the browser
# ---------------------------------------------------------------------------

@pytest.fixture
def chat():
    s = student_db()
    s.add(Message(student_id=1, course="AST101", role="assistant", content="Welcome back."))
    s.commit()
    with app_client(s) as c:
        yield c
    s.close()


def _open(page):
    page.goto(f"{ORIGIN}/chat?course=AST101")
    page.wait_for_selector("#messages >> text=Welcome back.")


def _limited(net, notice="That is today's tutor budget used up. It comes back at midnight PDT."):
    hit = []

    def capped(route, url):
        hit.append(route.request.post_data_buffer)
        route.fulfill(status=200, content_type="text/event-stream",
                      body=sse([{"type": "turn", "id": "t1"}, {"type": "token", "text": notice},
                                {"type": "done", "full_text": notice, "limited": True}]))

    net.stubs[("POST", "/chat/message")] = capped
    net.stubs[("GET", "/chat/quota")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"windows": [], "note": notice if hit else None, "blocked": notice if hit else None}))
    return hit


def test_a_refused_question_survives_a_reload(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        _limited(net)
        _open(page)
        page.fill("#msg-input", "Why is the sky dark at night?")
        page.click("button:has-text('Send')")
        expect(page.locator("button:has-text('Send')")).to_be_disabled()
        page.reload()
        expect(page.locator("#msg-input")).to_have_value("Why is the sky dark at night?")


def test_a_lapsed_session_releases_a_held_send(browser, chat):
    with open_page(browser, chat) as (page, net):
        hit = _limited(net)
        _open(page)
        page.fill("#msg-input", "Why?")
        page.click("button:has-text('Send')")
        expect(page.locator("button:has-text('Send')")).to_be_disabled()
        assert hit
        # The check the send makes after the reply has answered, and armed
        # the minute's recheck, before the session lapses.
        for _ in range(100):
            if sum(1 for r in net.seen if r.startswith("GET /chat/quota")) >= 2:
                break
            page.wait_for_timeout(50)
        page.wait_for_timeout(100)
        net.stubs[("GET", "/chat/quota")] = lambda route, url: route.fulfill(
            status=401, content_type="application/json", body='{"detail": "Not authenticated"}')
        page.clock.fast_forward(61_000)
        expect(page.locator("button:has-text('Send')")).to_be_enabled()


def test_a_starter_with_a_lapsed_session_keeps_the_typed_question(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        net.stubs[("GET", "/chat/starters")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body='{"course": "AST101", "starters": [{"label": "Quiz me", "message": "Quiz me on moon phases"}]}')
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=401, content_type="application/json", body='{"detail": "Not authenticated"}')
        _open(page)
        page.fill("#msg-input", "My half-typed question")
        page.click("button[aria-label='Ideas for what to ask']")
        page.click("button[data-track=starter]:has-text('Quiz me')")
        expect(page.locator("#messages")).to_contain_text("Your session expired")
        assert page.input_value("#msg-input") == "My half-typed question"
        assert page.evaluate("JSON.parse(localStorage.getItem('tutor-draft')).text") == "My half-typed question"


def test_a_retried_evaluate_sends_the_code_once_and_no_stray_prediction(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        posted = []

        def busy_then_ok(route, url):
            posted.append(route.request.post_data_buffer.decode("utf-8", "replace"))
            events = ([{"type": "turn", "id": "t1"}, {"type": "busy", "message": "Still answering."}]
                      if len(posted) == 1 else
                      [{"type": "turn", "id": "t2"}, {"type": "token", "text": "OK."},
                       {"type": "done", "full_text": "OK.", "message_id": 5}])
            route.fulfill(status=200, content_type="text/event-stream", body=sse(events))

        net.stubs[("POST", "/chat/message")] = busy_then_ok
        _open(page)
        page.fill("#msg-input", "Why does it loop?")
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", "int CODE_MARK = 1;")
        page.click("button:has-text('Evaluate')")
        page.click("[data-predict] button:has-text('skip')")
        expect(page.locator("#messages")).to_contain_text("Still answering.")
        assert page.input_value("#msg-input") == "Why does it loop?"     # only the typed part comes back
        page.click("button:has-text('Evaluate')")
        page.click("[data-predict] button[aria-label^='I expect 3']")
        expect(page.locator("#messages")).to_contain_text("OK.")
        assert posted[1].count("CODE_MARK") == 1 and "predicted_score" in posted[1]
        # A refused-too-long Evaluate leaves no prediction for the next message.
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", "y" * 31_000)
        page.click("button:has-text('Evaluate')")
        page.click("[data-predict] button[aria-label^='I expect 2']")
        expect(page.locator("[data-input-note]")).to_contain_text("characters")
        page.fill("#msg-input", "unrelated")
        page.click("button:has-text('Send')")
        expect(page.locator("#messages")).to_contain_text("unrelated")
        page.wait_for_timeout(300)
        assert "predicted_score" not in posted[-1]


def test_enter_sends_on_a_desktop_and_shift_enter_keeps_the_line(browser, chat):
    with open_page(browser, chat, width=1280, height=800, clock=False) as (page, net):
        posted = []
        net.stubs[("POST", "/chat/message")] = lambda route, url: (posted.append(1), route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "turn", "id": "t1"}, {"type": "token", "text": "Hi."},
                      {"type": "done", "full_text": "Hi.", "message_id": 3}])))
        _open(page)
        page.click("#msg-input")
        page.keyboard.type("first line")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("second line")
        assert page.input_value("#msg-input") == "first line\nsecond line" and posted == []
        page.keyboard.press("Enter")
        expect(page.locator("#messages")).to_contain_text("Hi.")
        assert posted == [1]


REVIEW = {"due_count": 2, "shown": 2, "minutes": 2, "amnesty": False, "items": [
    {"goal_id": "G1", "probe_id": "p1", "title": "Eclipses", "question": "What is an eclipse?"},
    {"goal_id": "G2", "probe_id": "p2", "title": "Seasons", "question": "What causes the seasons?"}]}


def test_a_review_answer_comes_back_with_its_question_after_a_new_draw(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        draws = [REVIEW, {**REVIEW, "items": [
            {"goal_id": "G2", "probe_id": "p2", "title": "Seasons", "question": "What causes the seasons?"},
            {"goal_id": "G1", "probe_id": "p9", "title": "Eclipses", "question": "Name a kind of eclipse."}]}]
        net.stubs[("GET", "/review/session")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(draws.pop(0) if draws else REVIEW))
        net.stubs[("POST", "/review/grade")] = lambda route, url: route.fulfill(
            status=401, content_type="application/json", body='{"detail": "Not authenticated"}')
        page.goto(f"{ORIGIN}/review?course=AST101")
        page.fill("textarea[aria-label='Your answer']", "The Moon blocks the Sun.")
        page.click("button:has-text('Check')")
        expect(page.locator("[data-review-card-error]")).to_contain_text("Your session has ended")
        page.reload()                         # signed in again: a new session, a new draw
        expect(page.locator("text=What is an eclipse?")).to_be_visible()
        expect(page.locator("textarea[aria-label='Your answer']")).to_have_value("The Moon blocks the Sun.")


def test_another_student_never_gets_the_review_draft(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        net.stubs[("GET", "/review/session")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(REVIEW))
        page.goto(f"{ORIGIN}/review?course=AST101")
        page.evaluate("""localStorage.setItem('tutor-review-draft', JSON.stringify(
            {goal_id: 'G1', probe_id: 'p1', question: 'What is an eclipse?', answer: "Sam's answer",
             sid: 999, course: 'AST101', at: Date.now()}))""")
        page.reload()
        expect(page.locator("text=What is an eclipse?")).to_be_visible()
        assert page.input_value("textarea[aria-label='Your answer']") == ""


def test_a_review_limit_ends_the_session_for_today(browser, chat):
    with open_page(browser, chat, clock=False) as (page, net):
        net.stubs[("GET", "/review/session")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(REVIEW))
        net.stubs[("POST", "/review/grade")] = lambda route, url: route.fulfill(
            status=429, content_type="application/json",
            body=json.dumps({"error": "limit", "feedback": usage.review_limit_message()}))
        page.goto(f"{ORIGIN}/review?course=AST101")
        page.fill("textarea[aria-label='Your answer']", "x")
        page.click("button:has-text('Check')")
        page.click("[data-review-skip]:has-text('Finish for today')")
        expect(page.locator("[data-review-done]")).to_contain_text("That is plenty of review for today.")
