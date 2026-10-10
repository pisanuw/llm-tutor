"""Plan step 1.1 (finding A2): slide refinement and the supervision in force.

A turn fixes its supervision (whether the judge runs, detector strictness, the
stream gate) from the envelope it starts with. Slide refinement used to lower
the ceiling afterwards: a code-adjacent turn with an empty scope started at H5
(lenient detector, no judge), find_topic anchored it to a lecture the student
half knows, the envelope dropped to H4, and a C++ fence streamed through the
lenient gate while the telemetry recorded ``rung_used=4, allow``.

Now a mastery-band ceiling never drops mid-turn (the recorded rung is the one
that was enforced), and an exam lockdown on the retrieved goals, a hard cap,
still applies with the judge and the strict detector switched on for the
rest of the turn.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import turn as agent
from tutor.agent.detector import StreamGate
from tutor.agent.judge import FALLBACK_MESSAGE, Verdict
from tutor.agent.policy import Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import (
    LearningGoal,
    MasteryAttempt,
    Message,
    Student,
    StudentProgress,
    TurnEvent,
)
from tests.helpers import make_session, ToolUse
from tests.helpers import Usage, scripted_rounds
from tutor.utctime import utcnow

pytestmark = pytest.mark.usefixtures("no_strategist")   # tests/conftest.py

# Three lines of C++: under the lenient detector's line floor (allowed at H5),
# but a function signature the strict detector cuts (no code at H4 and below).
SHORT_FENCE = "```cpp\nint area(int w, int h) {\n  return w * h;\n}\n```"
SLIDE_REF = {"lecture_num": 3, "slide_url": "/slides/AST101/lecture-03/index.html",
             "lecture_title": "Seasons", "slide_number": 5}


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))  # not an admin
    s.add(LearningGoal(id="G3", title="Seasons", description="", course="AST101", lecture_num=3))
    s.flush()
    # Mastery 0.5 on the slide's goal: band ceiling H4 (no code).
    s.add(StudentProgress(student_id=1, goal_id="G3", status="unlocked", mastery_score=0.5,
                          last_evidence_at=utcnow()))
    s.commit()
    yield s
    s.close()


def _run(db, script, judge, *, get_active_exam=None):
    """One code-adjacent QUESTION with no classifier scope and no lecture:
    the envelope starts at the empty-scope default (H5) and find_topic's
    slide anchors it to lecture 3."""
    student = db.get(Student, 1)

    async def go():
        return [e async for e in agent.run_turn(
            student=student, db=db, user_message="why does my sunrise-time program crash?",
            lecture_num=None, course="AST101",
        )]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION, has_attempt=True, goal_ids=[])), \
         patch("tutor.agent.mastery.get_active_exam", return_value=get_active_exam), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script, usage=Usage(100, 20, 0))), \
         patch("tutor.agent.tools.dispatch_tool",
               return_value={"results": [], "slide_ref": SLIDE_REF}), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", judge):
        events = asyncio.run(go())
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    return events, ev


def _streamed(events) -> str:
    return "".join(e.get("text", "") for e in events if e["type"] == "token")


def test_band_refinement_keeps_the_ceiling_the_turn_started_with(db):
    """The A2 probe: H5 at the start, H4 from the slide's goal."""
    judge = MagicMock(return_value=Verdict("allow"))
    script = [
        ([], "tool_use", [ToolUse("find_topic", "tu_1", query="seasons")]),
        (["Compare with this:\n", SHORT_FENCE, "\nWhat is missing?"], "end_turn", []),
    ]
    events, ev = _run(db, script, judge)

    env = json.loads(ev.envelope_json)
    assert env["goals"] == ["G3"]              # refinement still widens scope
    assert env["max_rung"] == 5                 # but the ceiling did not drop
    assert "kept H5" in env["reason"]
    assert ev.rung_used == 5                    # telemetry = what was enforced
    verdicts = json.loads(ev.verdicts_json)
    assert verdicts["judged"] is False          # H5 routine turn: no judge, by design
    assert "supervision_tightened" not in verdicts
    judge.assert_not_called()


def test_lockdown_from_the_slide_turns_on_judge_and_strict_detector(db):
    """An exam lockdown is a hard cap: it applies mid-turn and is enforced."""
    db.add(MasteryAttempt(student_id=1, goal_id="G3", status="pending", questions_json="{}"))
    db.commit()
    judge = MagicMock(return_value=Verdict("block", rule="exam_lockdown", reason="teaches G3"))
    script = [
        (["Let me look at the slides. "], "tool_use",
         [ToolUse("find_topic", "tu_1", query="seasons")]),
        (["Here it is:\n", SHORT_FENCE, "\nDone."], "end_turn", []),
    ]
    # get_active_exam=None stands in for a pending exam that does not
    # intercept this message (another course's exam, or a second pending one).
    events, ev = _run(db, script, judge)

    judge.assert_called_once()
    envelope_judged = judge.call_args.args[0]
    assert envelope_judged.exam_lockdown == ["G3"]
    assert envelope_judged.max_rung == 1
    assert judge.call_args.kwargs["samples"] == 3        # escalated review

    streamed = _streamed(events)
    assert "int area(" not in streamed                   # nothing leaked
    # The prefix streamed live before the tool result; the judged part
    # replaced the rest and starts its own paragraph.
    assert streamed == "Let me look at the slides.\n\n" + FALLBACK_MESSAGE

    assert ev.rung_used == 1
    verdicts = json.loads(ev.verdicts_json)
    assert verdicts["judged"] is True
    assert verdicts["supervision_tightened"] is True
    assert verdicts["verdict"] == "block"
    saved = db.query(Message).filter_by(role="assistant").one().content
    assert saved == streamed


def test_lockdown_switch_strict_detector_cuts_before_the_judge(db):
    """Even when the judge allows, the strict detector (H1) cuts the fence."""
    db.add(MasteryAttempt(student_id=1, goal_id="G3", status="pending", questions_json="{}"))
    db.commit()
    judge = MagicMock(return_value=Verdict("allow"))
    script = [
        ([], "tool_use", [ToolUse("find_topic", "tu_1", query="seasons")]),
        (["Here it is:\n", SHORT_FENCE], "end_turn", []),
    ]
    events, ev = _run(db, script, judge)

    assert "int area(" not in _streamed(events)
    judged_draft = judge.call_args.args[1]
    assert "int area(" not in judged_draft               # redacted before judging
    verdicts = json.loads(ev.verdicts_json)
    assert verdicts["detector_cut"] is True


def test_stream_gate_take_pending_hands_back_unemitted_text():
    gate = StreamGate()
    out = gate.feed("prose then ```cpp\nint x")
    assert out == "prose then "
    held = gate.take_pending()
    assert held == "```cpp\nint x"
    # The gate is reset: nothing is held and new prose flows normally.
    assert gate.close() == ""
    assert gate.feed("more prose") + gate.close() == "more prose"
