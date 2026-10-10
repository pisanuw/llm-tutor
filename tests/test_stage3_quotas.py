"""A used-up quota or cap is handled gracefully, and a large paste is taken
or refused in words (bulletproofing Stage 3).

R21: the refusal emptied the box though the question was never saved, gave
the notice a speaker that always failed, and left Send enabled. R25: the
class caps never said when they lift, the budget line spoke only of the
dollar quotas, and review stops left no record. R29: the speaker showed on
notices and failed on replies older than the last 20.
"""
from __future__ import annotations

import asyncio
import datetime
import json

import pytest
from fastapi.testclient import TestClient

from tutor.agent import state, usage
from tutor.db.models import Message, Student, TurnEvent, WhitelistEntry
from tutor.utctime import utcnow
from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tests.helpers import login_cookie, make_session

NOW = datetime.datetime(2026, 10, 7, 20, 0)          # 1 PM PDT (the fixture's zone), a Wednesday


@pytest.fixture
def db():
    s = make_session(Student(id=1, email="ada@example.edu", name="Ada"), Student(id=2, email="bo@example.edu", name="Bo"),
                     WhitelistEntry(email="@example.edu", added_by="test"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# R25: the class caps say when they lift, and the budget line reports them
# ---------------------------------------------------------------------------

def test_the_class_caps_say_when_they_lift():
    assert usage.daily_limit_message(NOW).endswith(
        "more questions open at midnight PDT (in about 11 hours). The slides and the book are still open. :-)")
    assert "the next items open at midnight PDT (in about 11 hours)" in usage.review_limit_message(NOW)
    # The class budget runs on the UTC month, which ends in the class's afternoon.
    assert "It is back on October 31 at 5 PM PDT (in 24 days)" in usage.monthly_cap_message(NOW)


def test_the_budget_line_reports_the_class_caps(db, monkeypatch):
    monkeypatch.setenv("DAILY_EXCHANGE_LIMIT", "5")
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    for _ in range(3):
        state.record_turn_event(db, student_id=1, kind="chat")
    assert usage.cap_indicator(db, 1) == {"blocked": None, "note": None}
    state.record_turn_event(db, student_id=1, kind="chat")
    note = usage.cap_indicator(db, 1)["note"]
    assert note.startswith("You have 1 question left for today (of 5). More open at midnight PDT")
    state.record_turn_event(db, student_id=1, kind="chat")
    assert usage.cap_indicator(db, 1)["blocked"].startswith(usage.DAILY_LIMIT_MESSAGE)
    assert db.query(TurnEvent).filter_by(kind="quota").count() == 0          # reading records nothing

    monkeypatch.delenv("DAILY_EXCHANGE_LIMIT", raising=False)
    monkeypatch.setenv("MONTHLY_COST_CAP_USD", "0.01")
    state.record_turn_event(db, student_id=2, kind="chat", cost_usd=0.02)
    assert usage.cap_indicator(db, 1)["blocked"].startswith(usage.MONTHLY_CAP_MESSAGE)


def test_the_quota_route_carries_the_class_caps(db, monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app

    monkeypatch.setenv("DAILY_EXCHANGE_LIMIT", "1")
    state.record_turn_event(db, student_id=1, kind="chat")
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.cookies.set("session_token", login_cookie(db, 1))
        q = client.get("/chat/quota").json()
    finally:
        app.dependency_overrides.clear()
    assert q["blocked"].startswith(usage.DAILY_LIMIT_MESSAGE) and q["note"] == q["blocked"]


def test_a_review_stop_is_recorded_once_a_day(db, monkeypatch):
    from tutor import quotas

    monkeypatch.setenv("DAILY_REVIEW_LIMIT", "1")
    monkeypatch.delenv("MONTHLY_COST_CAP_USD", raising=False)
    state.record_turn_event(db, student_id=1, kind="review")
    db.commit()
    assert usage.over_review_caps(db, 1).startswith(usage.REVIEW_LIMIT_MESSAGE)
    assert usage.over_review_caps(db, 1).startswith(usage.REVIEW_LIMIT_MESSAGE)
    hits = quotas.hits(db, utcnow() - datetime.timedelta(days=1))
    assert [h.window for h in hits] == ["reviews-day"]


# ---------------------------------------------------------------------------
# R21: the refusal says it was not taken
# ---------------------------------------------------------------------------

def test_a_capped_turn_marks_its_done_as_limited(db, monkeypatch):
    from tutor.agent import turn

    monkeypatch.setenv("DAILY_EXCHANGE_LIMIT", "1")
    state.record_turn_event(db, student_id=1, kind="chat")
    db.commit()

    async def collect():
        return [e async for e in turn.run_turn(student=db.get(Student, 1), db=db, user_message="hi",
                                               lecture_num=None, slide_num=None, course="AST101")]

    events = asyncio.run(collect())
    assert [e["type"] for e in events] == ["token", "done"]
    assert events[-1]["limited"] is True and "message_id" not in events[-1]
    assert db.query(Message).count() == 0                                    # the question was not saved


@pytest.fixture
def chat(monkeypatch):
    s = student_db()
    s.add(Message(student_id=1, course="AST101", role="assistant", content="Welcome back."))
    s.commit()
    with app_client(s) as c:
        yield c, s
    s.close()


def test_the_page_gives_the_question_back_and_waits(browser, chat, monkeypatch):
    from tutor.config import get_config

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    get_config.cache_clear()
    client, _ = chat
    notice = usage.daily_limit_message()
    with open_page(browser, client, clock=False) as (page, net):
        hit = []

        def capped(route, url):
            hit.append(1)
            route.fulfill(status=200, content_type="text/event-stream",
                          body=sse([{"type": "turn", "id": "t1"}, {"type": "token", "text": notice},
                                    {"type": "done", "full_text": notice, "limited": True}]))

        net.stubs[("POST", "/chat/message")] = capped
        # What the server's budget line says once the cap is hit.
        net.stubs[("GET", "/chat/quota")] = lambda route, url: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"windows": [], "note": notice if hit else None, "blocked": notice if hit else None,
                             "exempt": False}))
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=Welcome back.")
        page.fill("#msg-input", "Why is the sky dark at night?")
        page.click("button:has-text('Send')")
        expect(page.locator("#msg-input")).to_have_value("Why is the sky dark at night?")
        expect(page.locator("[data-quota-note]")).to_have_text(notice)
        expect(page.locator("button:has-text('Send')")).to_be_disabled()
        bubble = page.locator("[id^=bubble-]").last
        expect(bubble).to_contain_text("daily practice limit")
        assert bubble.locator(".tts-btn").count() == 0 and bubble.locator(".fb-wrap").count() == 0
        assert page.locator("#messages >> text=Why is the sky dark at night?").count() == 0


# ---------------------------------------------------------------------------
# R29: the speaker only on saved replies, and by id
# ---------------------------------------------------------------------------

def test_an_older_reply_is_spoken_by_its_id(db, monkeypatch):
    from unittest.mock import patch

    from tutor.auth.deps import get_db
    from tutor.main import app

    old = Message(student_id=1, role="assistant", content="An old reply about eclipses.", course="AST101")
    db.add(old)
    db.flush()
    db.add_all([Message(student_id=1, role="assistant", content=f"Reply {i}.", course="AST101") for i in range(25)])
    db.add(Message(student_id=2, role="assistant", content="Bo's reply.", course="AST101"))
    db.commit()
    theirs = db.query(Message).filter_by(student_id=2).one().id
    app.dependency_overrides[get_db] = lambda: db
    spoken = []
    try:
        client = TestClient(app)
        client.cookies.set("session_token", login_cookie(db, 1))
        with patch("tutor.agent.tts.synthesize", lambda text: spoken.append(text) or b"ID3"):
            # By text alone, older than the last 20: refused, as before.
            assert client.post("/chat/tts", data={"text": old.content}).status_code == 403
            # By id: spoken from the stored text, whatever the button sent.
            r = client.post("/chat/tts", data={"text": "anything", "message_id": str(old.id)})
            assert r.status_code == 200 and spoken == ["An old reply about eclipses."]
            # Someone else's reply by id is not the student's to speak.
            assert client.post("/chat/tts", data={"text": "Bo's reply.", "message_id": str(theirs)}).status_code == 403
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# R26: a photo is ledgered and checks the class budget
# ---------------------------------------------------------------------------

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")


def test_a_photo_is_ledgered_and_stops_at_the_class_budget(db, monkeypatch):
    from tutor.agent import vision
    from tutor.auth.deps import get_db
    from tutor.main import app

    monkeypatch.delenv("DAILY_EXCHANGE_LIMIT", raising=False)
    monkeypatch.setattr(vision, "transcribe_image", lambda data, media: {
        "ok": True, "code": "int x;", "usage": {"in_tokens": 1500, "out_tokens": 40}})
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.cookies.set("session_token", login_cookie(db, 1))
        r = client.post("/chat/transcribe-photo", files={"image": ("p.png", PNG, "image/png")})
        assert r.status_code == 200 and r.json() == {"ok": True, "code": "int x;"}
        (row,) = db.query(TurnEvent).filter_by(kind="vision").all()
        assert row.student_id == 1 and row.in_tokens == 1500 and row.cost_usd > 0

        monkeypatch.setenv("MONTHLY_COST_CAP_USD", "0.000001")
        r = client.post("/chat/transcribe-photo", files={"image": ("p.png", PNG, "image/png")})
        assert r.status_code == 429 and r.json()["error"].startswith(usage.MONTHLY_CAP_MESSAGE)
        assert db.query(TurnEvent).filter_by(kind="vision").count() == 1
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# R27, R28 in the browser: the review card and a lapsed session
# ---------------------------------------------------------------------------

SESSION = {"due_count": 2, "shown": 2, "minutes": 2, "amnesty": False, "items": [
    {"goal_id": "G1", "probe_id": "p1", "title": "Eclipses", "question": "What is an eclipse?"},
    {"goal_id": "G2", "probe_id": "p2", "title": "Seasons", "question": "What causes the seasons?"}]}


def _review(net, grade):
    net.stubs[("GET", "/review/session")] = lambda route, url: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(SESSION))
    net.stubs[("POST", "/review/grade")] = grade


def test_a_review_card_that_cannot_be_checked_has_a_way_forward(browser, chat):
    client, _ = chat
    with open_page(browser, client, clock=False) as (page, net):
        _review(net, lambda route, url: route.fulfill(
            status=409, content_type="application/json",
            body=json.dumps({"error": "already_checked", "next_due": "2026-10-09",
                             "feedback": "Your answer to this one was already checked; it comes back for review on 2026-10-09."})))
        page.goto(f"{ORIGIN}/review?course=AST101")
        page.fill("textarea[aria-label='Your answer']", "The Moon blocks the Sun.")
        page.click("button:has-text('Check')")
        expect(page.locator("[data-review-card-error]")).to_contain_text("already checked")
        page.click("[data-review-skip]")
        expect(page.locator("text=What causes the seasons?")).to_be_visible()


def test_a_lapsed_session_on_review_keeps_the_answer(browser, chat):
    client, _ = chat
    with open_page(browser, client, clock=False) as (page, net):
        _review(net, lambda route, url: route.fulfill(status=401, content_type="application/json",
                                                     body='{"detail": "Not authenticated"}'))
        page.goto(f"{ORIGIN}/review?course=AST101")
        page.fill("textarea[aria-label='Your answer']", "The Moon blocks the Sun.")
        page.click("button:has-text('Check')")
        expect(page.locator("[data-review-card-error]")).to_contain_text("Your session has ended")
        link = page.locator("[data-review-signin]")
        expect(link).to_have_attribute("href", "/auth/login?next=%2Freview%3Fcourse%3DAST101")
        page.reload()                                       # the page after signing in
        expect(page.locator("textarea[aria-label='Your answer']")).to_have_value("The Moon blocks the Sun.")


def test_a_reply_cut_by_a_lapsed_session_says_so(browser, chat):
    client, _ = chat
    with open_page(browser, client, clock=False) as (page, net):
        # The stream drops after its first token; the resume finds the session gone.
        net.stubs[("POST", "/chat/message")] = lambda route, url: route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse([{"type": "turn", "id": "t9"}, {"type": "token", "text": "Partly "}]))
        net.stubs[("GET", "/chat/message/t9")] = lambda route, url: route.fulfill(
            status=401, content_type="application/json", body='{"detail": "Not authenticated"}')
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=Welcome back.")
        page.fill("#msg-input", "Explain eclipses")
        page.click("button:has-text('Send')")
        expired = page.locator("[data-expired]")
        expect(expired).to_contain_text("Your session ended while this reply was coming")
        expect(expired.locator("a")).to_have_attribute("href", "/auth/login?next=%2Fchat%3Fcourse%3DAST101")


def test_a_photo_with_a_lapsed_session_says_so(browser, chat):
    client, _ = chat
    with open_page(browser, client, clock=False) as (page, net):
        net.stubs[("POST", "/chat/transcribe-photo")] = lambda route, url: route.fulfill(
            status=401, content_type="application/json", body='{"detail": "Not authenticated"}')
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=Welcome back.")
        page.click("button[aria-label='Submit code for feedback']")
        page.fill("#code-input", "int main() {}")
        page.set_input_files("input[type=file]:not([capture])",
                             files=[{"name": "shot.png", "mimeType": "image/png", "buffer": PNG}])
        page.fill("#msg-input", "Why does it loop?")
        page.set_input_files("input[type=file]:not([capture])",
                             files=[{"name": "shot.png", "mimeType": "image/png", "buffer": PNG}])
        expect(page.locator("[data-photo-error]")).to_contain_text("Your session has ended")
        expect(page.locator("a:has-text('Sign in again')")).to_be_visible()
        # Each box keeps its own: the question for the message box, the code
        # for the code panel (the Stage 3 review: the code came back as the question).
        page.reload()
        expect(page.locator("#code-input")).to_have_value("int main() {}")
        expect(page.locator("#msg-input")).to_have_value("Why does it loop?")
