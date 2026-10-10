"""Plan step 3.7 (finding B13, after step 1.8): the gate as an AST102 student meets it.

A prerequisite exam gives one line on why a topic from the other course is
checked (only when decision 5 makes such prerequisites bind), says how to put
the check off, and a bare "Not now" answers the question the gate held back
while the lecture's check waits a day. The chat offers a "Not now" button.
Done when: a walk-through transcript of a week-one AST102 code paste reads
sensibly (printed by the walk-through test; run it with -s to read it).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.browser_support import ORIGIN, app_client, expect, open_page, sse, student_db
from tutor.agent import mastery, navigator
from tutor.agent import turn as agent
from tutor.agent.judge import Verdict
from tutor.agent.policy import Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import Base, LearningGoal, MasteryAttempt, Message, Student, StudentPath
from tests.helpers import applied

CODE_PASTE = ("Why does this print garbage?\n\n```cpp\ndouble mags[3] = {1.5, 2.0, 0.8};\n"
              "for (int i = 0; i <= 3; ++i) cout << mags[i];\n```")


def _questions(conceptual=True):
    return {"q1": {"question": "What is a lunar phase?", "rubric": "r"},
            "q2": {"question": "Why does the lit half face the Sun?", "rubric": "r"},
            "code": {"question": "Name one phase.", "starter": "", "rubric": "r",
                     "is_conceptual": conceptual}}


# ---------------------------------------------------------------------------
# The intro and the deferral words
# ---------------------------------------------------------------------------

def test_the_intro_explains_a_cross_course_check_in_one_line():
    goal = LearningGoal(id="AST101-g", title="Estimate angles on the sky", description="",
                        course="AST101", lecture_num=2)
    text = mastery.format_exam_intro(goal, _questions(), course="AST102")
    assert "\nIt comes from AST 101, which AST 102 builds on.\n" in text
    same = mastery.format_exam_intro(goal, _questions(), course="AST101")
    assert "It comes from" not in same
    for intro in (text, same):
        assert intro.endswith("Not a good time? Reply **Not now**: I will answer your question "
                              "and leave this lecture's check for a day.")
        assert "—" not in intro


@pytest.mark.parametrize("message,bare", [
    ("Not now", True), ("not now thanks", True), ("Can we do this later?", True),
    ("skip this please", True), ("not now, explain eclipses first", False),
    ("I'd rather talk about tides", False), ("Q1: the share of the lit half we see", False),
])
def test_a_bare_deferral_asks_for_nothing_new(message, bare):
    assert mastery.is_bare_deferral(message) is bare


# ---------------------------------------------------------------------------
# The walk-through: a fresh AST102 student pastes code in week one
# ---------------------------------------------------------------------------

@pytest.fixture
def graph():
    """The real goal graph, every goal with exam questions (the worst case),
    and a fresh AST102 student."""
    from tutor.startup import seed_learning_goals_if_empty

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    seed_learning_goals_if_empty(engine)
    db = sessionmaker(bind=engine)()
    if not db.query(LearningGoal).count():
        pytest.skip("curriculum/learning_goals.json not present")
    db.add(Student(id=1, email="fresh_ast102@example.edu", name="Fresh"))
    db.add(StudentPath(student_id=1, course="AST102", track_id="AST102-lecture-order"))
    db.commit()
    bank = {g.id: {"questions": [
        {"id": f"{g.id}-{i}", "kind": "concept", "question": f"{g.title}: question {i}?", "rubric": "r"}
        for i in range(5)]} for g in db.query(LearningGoal).all()}
    agent._INFLIGHT.clear()
    with patch.object(mastery, "_load_exam_bank", return_value=bank):
        yield db
    db.close()


class _Usage:
    input_tokens = 300
    output_tokens = 80
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _turn(db, message, lecture, seen):
    """One turn through run_turn with the model calls faked: the actor
    answers whatever arrives as the final user message, and ``seen``
    collects those messages."""
    async def actor(system, messages, tools):
        asked = messages[-1]["content"]
        seen.append(asked if isinstance(asked, str) else str(asked))
        yield ("chunk", "Look at the loop's stop condition: what is the last valid index?")
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = _Usage()
        final.model = None
        yield ("final", final)

    from tutor.agent.strategist import NO_MOVE

    attempt = "```" in message
    patches = [
        patch("tutor.agent.preclassify.classify", return_value=Classification(
            intent=Intent.ATTEMPT if attempt else Intent.QUESTION, has_attempt=attempt)),
        patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE),
        patch("tutor.agent.judge.judge", return_value=Verdict("allow")),
        patch("tutor.agent.mastery.classify_exam_message",
              side_effect=lambda m, **k: mastery._keyword_exam_intent(m)),
        patch("tutor.agent.turn_stages.actor_client.stream_round", actor),
        patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"),
    ]

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message=message,
            lecture_num=lecture, course="AST102")]
    with applied(patches):
        return asyncio.run(go())


def _said(events) -> str:
    """What the tutor streamed; a section break (two saved replies, two
    bubbles in the chat) shows as " / "."""
    return "".join(e["text"] if e["type"] == "token" else " / "
                   for e in events if e["type"] in ("token", "section"))


def _walk(db):
    transcript, seen = [], []

    def step(who_says, message, lecture):
        events = _turn(db, message, lecture, seen)
        transcript.append(f"STUDENT (lecture {lecture}): {message.splitlines()[0]}")
        transcript.append("TUTOR: " + " ".join(_said(events).split()))
        return events

    return transcript, seen, step


def test_week_one_ast102_code_paste_walk_through(graph, capsys):
    db = graph
    transcript, seen, step = _walk(db)

    # Lecture 1: no exam. Its only earlier prerequisites are AST 101 topics,
    # which advise and do not bind (decision 5's default).
    first = step("student", CODE_PASTE, 1)
    assert not any(e["type"] == "exam" for e in first)
    assert "Quick Mastery Check" not in _said(first) and seen[-1] == CODE_PASTE

    # Lecture 2: one check, on an AST 102 lecture 1 topic, with a way out.
    second = step("student", CODE_PASTE, 2)
    exam = [e for e in second if e["type"] == "exam"]
    assert exam == [{"type": "exam", "goal": "Compare stars on the magnitude scale",
                     "not_now": "Not now"}]
    intro = _said(second)
    assert "It comes from" not in intro                     # same course
    assert "Reply **Not now**" in intro

    # "Not now": the check is put off, and the pasted code is answered.
    third = step("student", "Not now", 2)
    assert _said(third).startswith(mastery.DEFER_ACK + " / ")
    assert seen[-1] == CODE_PASTE                           # the held-back question
    assert db.query(MasteryAttempt).one().status == "deferred"

    # The rest of the day on lecture 2 is answered, not re-gated.
    fourth = step("student", CODE_PASTE.replace("<=", "<"), 2)
    assert not any(e["type"] == "exam" for e in fourth)

    with capsys.disabled():
        print("\n--- week-one AST102 walk-through ---\n" + "\n".join(transcript))
    roles = [m.role for m in db.query(Message).order_by(Message.id).all()]
    # "Not now" saved two replies: the acknowledgement, then the answer.
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant", "assistant",
                     "user", "assistant"]


def test_with_binding_prerequisites_the_ast101_check_says_why(graph, monkeypatch):
    """Decision 5 flipped: lecture 1 is gated by an AST 101 topic, and the
    intro says in one line why."""
    monkeypatch.setattr(navigator, "CROSS_COURSE_PREREQS_BIND", True)
    events = _turn(graph, CODE_PASTE, 1, [])
    assert [e for e in events if e["type"] == "exam"][0]["goal"] == "Estimate angles on the sky"
    assert "It comes from AST 101, which AST 102 builds on." in _said(events)


def test_a_deferral_with_its_own_request_answers_that(graph):
    seen: list[str] = []
    _turn(graph, CODE_PASTE, 2, seen)
    _turn(graph, "not now, explain eclipses first", 2, seen)
    assert seen[-1] == "not now, explain eclipses first"


# ---------------------------------------------------------------------------
# The chat's "Not now" button
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    s = student_db()
    s.add(Message(student_id=1, course="AST101", role="user", content="What is a light-year?"))
    s.commit()
    with app_client(s) as c:
        yield c
    s.close()


def test_the_chat_offers_not_now_under_a_check(browser, client):
    intro = ("Before we go further, I want to check your understanding of **Moon Phases**.\n\n"
             "**Quick Mastery Check**\n\n**Q1:** What is a lunar phase?")
    posted: list[str] = []

    def turn(route, url):
        posted.append(route.request.post_data or "")
        events = ([{"type": "turn", "id": "t1"},
                   {"type": "exam", "goal": "Moon Phases", "not_now": "Not now"},
                   {"type": "token", "text": intro}, {"type": "done", "full_text": intro}]
                  if len(posted) == 1 else
                  [{"type": "turn", "id": "t2"}, {"type": "token", "text": "Sure."},
                   {"type": "done", "full_text": "Sure."}])
        route.fulfill(status=200, content_type="text/event-stream", body=sse(events))

    with open_page(browser, client, clock=False) as (page, net):
        net.stubs[("POST", "/chat/message")] = turn
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.wait_for_selector("#messages >> text=What is a light-year?")
        page.fill("#msg-input", "for (int i = 0; i <= 3; ++i) cout << mags[i];")
        page.click("button:has-text('Send')")
        button = page.locator("[data-not-now] button")
        expect(button).to_have_text("Not now")
        assert button.bounding_box()["height"] >= 44
        button.click()
        expect(page.locator("#messages >> text=Sure.")).to_be_visible()
        assert 'name="message"\r\n\r\nNot now\r\n' in posted[1]
        expect(page.locator("[data-not-now]")).to_have_count(0)     # gone once used
