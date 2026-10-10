"""Chat failure states (plan step 0.7, findings D1, D2, A21, C2).

A failed tutor call used to reach history as a note that always blamed the
connection, and a page request without a live session got raw JSON. These
pin the event order a failed turn produces, the note history keeps, the
HTML-only login redirect, and the next= path through both login flows.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import patch

import anthropic
import httpx
import pytest

from tutor.agent import turn as agent
from tutor.agent.judge import Verdict
from tutor.agent.policy import Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import LearningGoal, Message, Student, TurnEvent
from tutor.routers.auth import _safe_next
from tutor.agent.turn_stages import errors
from tests.helpers import make_session

pytestmark = pytest.mark.usefixtures("no_strategist")   # tests/conftest.py


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="Light-years", description="", course="AST101", lecture_num=1))
    s.commit()
    yield s
    s.close()


def _failing_round(chunks, exc):
    async def fake(system, messages, tools):
        for c in chunks:
            yield ("chunk", c)
        raise exc
    return fake


def _run(db, stream):
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=Verdict("allow")), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", stream):
        async def go():
            return [e async for e in agent.run_turn(
                student=db.get(Student, 1), db=db, user_message="what is a light-year?",
                course="AST101")]
        return asyncio.run(go())


def _saved_reply(db):
    return db.query(Message).filter_by(role="assistant").one().content


def test_failed_turn_sends_error_then_done_and_saves_the_real_cause(db):
    events = _run(db, _failing_round([], Exception("Error code: 401 - invalid x-api-key")))
    kinds = [e["type"] for e in events]
    assert kinds == ["error", "done"]
    assert "isn't available" in events[0]["message"]
    note = "[No reply: the tutor was not available (the AI service was not set up or had no credit).]"
    assert events[-1]["full_text"] == note
    assert _saved_reply(db) == note
    verdicts = json.loads(db.query(TurnEvent).filter_by(kind="chat").one().verdicts_json)
    assert verdicts["verdict"] == "stream_error"
    assert verdicts["stream_error_kind"] == "unavailable"


def test_partial_reply_is_kept_and_marked_incomplete(db):
    conn_error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    events = _run(db, _failing_round(["A light-year is the distance light travels in a year. "],
                                     conn_error))
    kinds = [e["type"] for e in events]
    assert kinds[-2:] == ["error", "done"]
    assert "token" not in kinds[kinds.index("error"):]   # nothing the client would drop
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed == "A light-year is the distance light travels in a year. "
    saved = _saved_reply(db)
    assert saved.startswith("A light-year is the distance")
    assert saved.endswith("[Reply incomplete: the tutor lost its connection to the AI service.]")


def test_error_kinds():
    assert errors.error_kind(Exception("authentication_error")) == "unavailable"
    assert errors.error_kind(Exception("Error code: 529 overloaded")) == "overloaded"
    conn = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    assert errors.error_kind(conn) == "connection"
    assert errors.error_kind(ValueError("boom")) == "other"


# ---------------------------------------------------------------------------
# Page requests without a session go to the login page
# ---------------------------------------------------------------------------

HTML = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def test_page_without_session_redirects_to_login_with_next(app_client):
    resp = app_client.get("/chat?course=AST102", headers=HTML, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?next=%2Fchat%3Fcourse%3DAST102"
    assert 'session_token=""' in resp.headers.get("set-cookie", "")


def test_stale_cookie_on_a_page_also_redirects(app_client):
    app_client.cookies.set("session_token", "not-a-jwt")
    resp = app_client.get("/map", headers=HTML, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?next=%2Fmap"


def test_fetch_and_post_keep_the_json_401(app_client):
    assert app_client.get("/chat/history").status_code == 401            # fetch: Accept */*
    # A script's POST says Sec-Fetch-Dest "empty"; a form's is a navigation
    # and goes to the login page (bulletproofing R19, tests/test_stage2_urls.py).
    resp = app_client.post("/chat/message", data={"message": "hi"},
                           headers={**HTML, "sec-fetch-dest": "empty", "sec-fetch-mode": "cors"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


def test_a_page_404_is_a_page_and_a_fetch_404_stays_json(app_client):
    """Since review step 0.1 a navigation to a dead link gets error.html
    (status kept) with a reference; a fetch keeps the JSON its script expects."""
    resp = app_client.get("/no-such-page", headers=HTML)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "That page is not here" in resp.text and "Reference " in resp.text
    fetched = app_client.get("/no-such-page")
    assert fetched.status_code == 404 and fetched.json() == {"detail": "Not Found"}


# ---------------------------------------------------------------------------
# next= through the login flows, never off-site
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("/chat?course=AST102", "/chat?course=AST102"),
    ("/instructor?class_id=3", "/instructor?class_id=3"),
    ("//evil.com", "/chat"),
    ("/\\evil.com", "/chat"),
    ("https://evil.com/x", "/chat"),
    ("javascript:alert(1)", "/chat"),
    ("/auth/logout", "/chat"),
    ("/\tevil", "/chat"),
    ("", "/chat"),
    (None, "/chat"),
])
def test_safe_next(raw, expected):
    assert _safe_next(raw) == expected


def test_login_page_carries_a_safe_next_only(app_client):
    body = app_client.get("/auth/login?next=%2Fmap").text
    assert '<input type="hidden" name="next" value="/map">' in body
    assert 'name="next"' not in app_client.get("/auth/login?next=%2F%2Fevil.com").text


def _magic_login(client, monkeypatch, next_value):
    import tutor.config as config
    from tutor.routers import auth

    monkeypatch.setenv("ADMIN_EMAILS", "prof@example.edu")   # bypasses the whitelist
    config.get_config.cache_clear()
    auth._magic_link_log.clear()
    page = client.post("/auth/magic-link", data={"email": "prof@example.edu", "next": next_value}).text
    link = re.search(r'href="https?://[^/"]+(/auth/magic/[^"]+)"', page).group(1)
    return link, client.post(link, follow_redirects=False)


def test_magic_link_returns_to_the_requested_page(app_client, monkeypatch):
    link, resp = _magic_login(app_client, monkeypatch, "/map")
    assert link.endswith("?next=%2Fmap")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/map"


def test_magic_link_never_redirects_off_site(app_client, monkeypatch):
    link, resp = _magic_login(app_client, monkeypatch, "//evil.com")
    assert "next=" not in link
    assert resp.headers["location"] == "/chat"
