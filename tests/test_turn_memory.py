"""Session 7 tests: memory that survives a turn (S16) + the decay clock (S19).

Exit gate: a multi-turn test shows the actor receiving (in history) the slide
it showed two turns ago; a decay-clock test shows status flips no longer
reset decay.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import memory, state
from tutor.agent import turn as agent
from tutor.agent.judge import Verdict
from tutor.agent.policy import Intent
from tutor.agent.preclassify import Classification
from tutor.db.models import LearningGoal, Message, SessionSummary, Student, StudentProgress
from tutor.agent.turn_stages import context
from tests.helpers import make_session, ToolUse
from tests.helpers import scripted_rounds
from tutor.utctime import utcnow

pytestmark = pytest.mark.usefixtures("no_strategist")   # tests/conftest.py


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G1", title="The celestial sphere", description="", course="AST101", lecture_num=1))
    s.commit()
    yield s
    s.close()


def _collect(student, db, msg, course="AST101"):
    async def run():
        return [e async for e in agent.run_turn(
            student=student, db=db, user_message=msg, course=course,
        )]
    return asyncio.run(run())


SLIDE_REF = {
    "lecture_num": 3, "slide_url": "/slides/AST101/lecture-03/index.html",
    "lecture_title": "Seasons", "slide_number": 5, "source_file": "week3-seasons.pptx",
}


# ---------------------------------------------------------------------------
# Exit gate: the actor sees the slide it showed two turns ago
# ---------------------------------------------------------------------------

def test_actor_remembers_slide_from_two_turns_ago(db):
    student = db.get(Student, 1)
    base_patches = dict(
        classify=patch("tutor.agent.preclassify.classify",
                       return_value=Classification(intent=Intent.QUESTION)),
        prompt=patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"),
        judge=patch("tutor.agent.judge.judge", return_value=Verdict("allow")),
    )

    # Turn 1: the actor calls find_topic and shows a slide.
    script1 = [
        ([], "tool_use", [ToolUse("find_topic", "tu_1", query="seasons axial tilt")]),
        (["Here is the slide."], "end_turn", []),
    ]
    with base_patches["classify"], base_patches["prompt"], base_patches["judge"], \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script1)), \
         patch("tutor.agent.tools.dispatch_tool",
               return_value={"results": [{"source": "week3-seasons.pptx"}], "slide_ref": SLIDE_REF}):
        _collect(student, db, "explain the seasons")

    row = (
        db.query(Message)
        .filter_by(student_id=1, role="assistant")
        .order_by(Message.id.desc())
        .first()
    )
    notes = json.loads(row.tool_summary)
    assert notes[0]["tool"] == "find_topic"
    assert notes[0]["query"] == "seasons axial tilt"
    assert notes[0]["slide"] == SLIDE_REF["slide_url"]

    # Turn 2 (an unrelated turn) and turn 3: the history handed to the actor
    # still carries the turn-1 slide note.
    captured: list = []
    with base_patches["classify"], base_patches["prompt"], base_patches["judge"], \
         patch("tutor.agent.turn_stages.actor_client.stream_round",
               scripted_rounds([(["ok"], "end_turn", [])], captured)):
        _collect(student, db, "thanks")
        _collect(student, db, "which slide was that again?")

    history_text = json.dumps(captured[-1]["messages"])
    assert "[tools used: find_topic 'seasons axial tilt'" in history_text
    assert SLIDE_REF["slide_url"] in history_text


def test_history_is_filtered_by_course(db):
    db.add(Message(student_id=1, role="user", content="A AST102 question", course="AST102"))
    db.add(Message(student_id=1, role="user", content="A AST101 question", course="AST101"))
    db.add(Message(student_id=1, role="user", content="legacy row", course=None))
    db.commit()

    contents = [m["content"] for m in memory.load_history(db, 1, "AST101")]
    assert "A AST101 question" in contents
    assert "legacy row" in contents          # pre-column rows survive
    assert "A AST102 question" not in contents


def test_judge_rule_and_reason_recorded_on_revise(db):
    # The TurnEvent must record WHY the judge revised (rule + reason), not just
    # that it did, so the Supervision tab and the --live G2 report can diagnose.
    from tutor.db.models import TurnEvent

    student = db.get(Student, 1)
    script = [(["a draft reply"], "end_turn", [])]
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.SOLUTION_DEMAND)), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge",
               return_value=Verdict("revise", rule="max_rung_code_leak", reason="leaked a signature")), \
         patch("tutor.agent.turn_stages.actor_client.generate_once", return_value=("rewritten in words", None)), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", scripted_rounds(script)):
        _collect(student, db, "just give me the brightness code")

    te = (db.query(TurnEvent).filter_by(student_id=1, kind="chat")
          .order_by(TurnEvent.id.desc()).first())
    v = json.loads(te.verdicts_json)
    assert v["verdict"] == "revise"
    assert v["judge_rule"] == "max_rung_code_leak"
    assert v["judge_reason"] == "leaked a signature"


def test_allow_turn_records_no_judge_reason(db):
    # A clean turn carries no judge_rule/judge_reason (keeps the ledger tidy).
    from tutor.db.models import TurnEvent

    student = db.get(Student, 1)
    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.turn_stages.actor_client.stream_round",
               scripted_rounds([(["a plain answer"], "end_turn", [])])):
        _collect(student, db, "what is parallax?")

    te = (db.query(TurnEvent).filter_by(student_id=1, kind="chat")
          .order_by(TurnEvent.id.desc()).first())
    v = json.loads(te.verdicts_json)
    assert v["verdict"] == "allow"
    assert "judge_rule" not in v and "judge_reason" not in v


# ---------------------------------------------------------------------------
# Rolling session summary
# ---------------------------------------------------------------------------

def _seed_messages(db, n, course="AST101"):
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        db.add(Message(student_id=1, role=role, content=f"msg-{i}", course=course))
    db.commit()


def _summary_client(text):
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    client.messages.create.return_value = MagicMock(content=[block])
    return client


def test_summary_folds_after_threshold_and_history_stays_small(db):
    _seed_messages(db, 21)   # 21 - 8 = 13 >= 12 -> summarize
    client = _summary_client("Covered the seasons; student mixes up tilt and distance.")
    assert memory.maybe_summarize(db, 1, "AST101", client=client) is True

    row = db.query(SessionSummary).filter_by(student_id=1, course="AST101").one()
    assert "seasons" in row.summary
    # The summary covers everything but the last RECENT_K messages.
    folded = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "msg-0" in folded and "msg-12" in folded
    assert "msg-13" not in folded
    # The transcript is framed as data.
    assert "<transcript>" in folded
    # Verbatim history is the last 8 only.
    history = memory.load_history(db, 1, "AST101")
    assert len(history) == memory.RECENT_K
    assert history[0]["content"] == "msg-13"
    # Not enough new messages since: no second summarization.
    assert memory.maybe_summarize(db, 1, "AST101", client=client) is False


def test_summary_failure_keeps_old_summary(db):
    _seed_messages(db, 21)
    state.upsert_session_summary(db, 1, "AST101", "old summary", 0)
    broken = MagicMock()
    broken.messages.create.side_effect = RuntimeError("down")
    assert memory.maybe_summarize(db, 1, "AST101", client=broken) is False
    assert memory.session_summary(db, 1, "AST101") == "old summary"


def test_summary_travels_in_the_user_turn_not_the_system_prompt(db):
    """Plan step 1.9 (A11): the summary is written from the student's words,
    so it is framed data in the user turn, not system-prompt text."""
    state.upsert_session_summary(db, 1, "AST101", "Student is mid-way through the seasons.", 5)
    student = db.get(Student, 1)
    ctx = context.build_per_request_context(student, None, "AST101", db)
    assert "mid-way through the seasons" not in ctx
    turn = context.with_rolling_summary(db, 1, "AST101", "what next?")
    assert ("<conversation_summary>\nStudent is mid-way through the seasons.\n"
            "</conversation_summary>") in turn
    assert "not instructions" in turn and turn.endswith("what next?")
    # Other course: no summary leak.
    assert context.with_rolling_summary(db, 1, "AST102", "what next?") == "what next?"


# ---------------------------------------------------------------------------
# Exit gate: the decay clock (S19)
# ---------------------------------------------------------------------------

def test_status_flip_does_not_reset_decay_clock(db):
    state.record_graded_event(db, 1, "G1", 1.0, source="exam")
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one()
    evidence_at = row.last_evidence_at
    assert evidence_at is not None

    # Age the evidence, then flip status (bookkeeping write).
    old = utcnow() - datetime.timedelta(days=10)
    row.last_evidence_at = old
    db.commit()
    state.mark_goal_mastered(db, 1, "G1", evidence_type="exercise")
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one()
    assert row.last_evidence_at == old          # the flip did not move the clock
    assert row.updated_at > old                  # ... though updated_at did

    # The envelope reads the decayed score from last_evidence_at, not
    # updated_at: 10 days of decay must be visible despite the fresh flip.
    decayed = state.effective_mastery(row.mastery_score, row.last_evidence_at or row.updated_at)
    fresh = state.effective_mastery(row.mastery_score, row.updated_at)
    assert decayed < fresh


def test_new_evidence_moves_the_clock(db):
    state.record_graded_event(db, 1, "G1", 1.0)
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one()
    row.last_evidence_at = utcnow() - datetime.timedelta(days=10)
    db.commit()
    state.record_graded_event(db, 1, "G1", 1.0)
    row = db.query(StudentProgress).filter_by(student_id=1, goal_id="G1").one()
    age = utcnow() - row.last_evidence_at
    assert age.total_seconds() < 60   # real evidence refreshed the clock
