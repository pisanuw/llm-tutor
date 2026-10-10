"""Plan step 1.7 (findings B14, B17, B21): exam machinery.

- A pending AST101 exam no longer intercepts (and grades) a AST102 message.
- No question the student failed today is drawn again; when the pool runs
  out there is no exam today instead of a repeat.
- An infrastructure close does not spend one of the day's attempts, and a
  classifier outage falls back to keywords before defaulting to ANSWER.
"""
from __future__ import annotations

import asyncio
import datetime
import json
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import mastery, state
from tutor.db.models import LearningGoal, MasteryAttempt, Student
from tutor.utctime import utcnow
from tests.helpers import make_session


def _pool(n_concept: int, n_code: int) -> list[dict]:
    qs = [{"id": f"c{i}", "kind": "concept", "question": f"concept-{i}?", "rubric": "r"}
          for i in range(n_concept)]
    qs += [{"id": f"k{i}", "kind": "code", "question": f"code-{i}?", "rubric": "r", "starter": ""}
           for i in range(n_code)]
    return qs


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.add(LearningGoal(id="G_ast101", title="Seasons", description="", course="AST101", lecture_num=3))
    s.add(LearningGoal(id="G_ast102", title="Galaxies", description="", course="AST102", lecture_num=3))
    s.commit()
    yield s
    s.close()


def _texts(attempt: MasteryAttempt) -> set[str]:
    return mastery._question_texts(json.loads(attempt.questions_json))


# ---------------------------------------------------------------------------
# B14: course-scoped intercept
# ---------------------------------------------------------------------------

def test_get_active_exam_is_scoped_to_the_course(db):
    a = state.start_exam_attempt(db, 1, "G_ast101", json.dumps({}))
    db.commit()
    assert mastery.get_active_exam(1, db, course="AST102") is None
    assert mastery.get_active_exam(1, db, course="AST101").id == a.id
    assert mastery.get_active_exam(1, db).id == a.id      # no course: any


def test_a_pending_ast101_exam_does_not_grade_a_ast102_message(db):
    from tutor.agent import turn as agent
    from tutor.agent.policy import Intent
    from tutor.agent.preclassify import Classification
    from tutor.agent.strategist import NO_MOVE

    attempt = state.start_exam_attempt(db, 1, "G_ast101", json.dumps(
        {"q1": {"question": "a?", "rubric": "r"}, "q2": {"question": "b?", "rubric": "r"},
         "code": {"question": "c?", "rubric": "r", "starter": "", "is_conceptual": True}}))
    db.commit()

    class _Usage:
        input_tokens = 10
        output_tokens = 5
        cache_read_input_tokens = 0

    async def one_round(system, messages, tools):
        yield ("chunk", "Spirals have arms; ellipticals do not.")
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = _Usage()
        yield ("final", final)

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message="how are galaxies classified?",
            lecture_num=3, course="AST102",
        )]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE), \
         patch("tutor.agent.mastery.check_prerequisite_gate", return_value=None), \
         patch("tutor.agent.mastery.classify_exam_message") as exam_intent, \
         patch("tutor.agent.mastery._grade_with_api") as grader, \
         patch("tutor.agent.turn_stages.actor_client.stream_round", one_round), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        events = asyncio.run(go())

    exam_intent.assert_not_called()
    grader.assert_not_called()
    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "Spirals have arms; ellipticals do not." in text and "Checking your answers" not in text
    db.refresh(attempt)
    assert attempt.status == "pending" and attempt.attempts == 0


# ---------------------------------------------------------------------------
# B17: no repeats within a day; no exam instead of a repeat
# ---------------------------------------------------------------------------

def _fail_today(db, questions: dict) -> MasteryAttempt:
    a = state.start_exam_attempt(db, 1, "G_ast101", json.dumps(questions))
    state.record_exam_result(db, a, False)
    return a


def test_attempt_three_never_redraws_attempt_one(db):
    pool = _pool(7, 2)                                   # 9 questions: exactly 3 attempts
    goal = db.get(LearningGoal, "G_ast101")
    seen: list[set[str]] = []
    with patch.object(mastery, "_question_pool", return_value=pool):
        for _ in range(3):
            a = mastery.start_exam(1, goal, db)
            assert a is not None
            seen.append(_texts(a))
            state.record_exam_result(db, a, False)
            db.commit()
    assert not (seen[0] & seen[1]) and not (seen[0] & seen[2]) and not (seen[1] & seen[2])


def test_no_exam_today_instead_of_a_repeat(db):
    pool = _pool(4, 2)                                   # 6 questions: two attempts' worth
    goal = db.get(LearningGoal, "G_ast101")
    with patch.object(mastery, "_question_pool", return_value=pool):
        for _ in range(2):
            a = mastery.start_exam(1, goal, db)
            state.record_exam_result(db, a, False)
            db.commit()
        assert mastery.start_exam(1, goal, db) is None   # stands aside for the window


def test_a_new_day_may_reuse_questions_from_a_small_pool(db):
    pool = _pool(2, 1)                                   # legacy 3-question pool
    goal = db.get(LearningGoal, "G_ast101")
    with patch.object(mastery, "_question_pool", return_value=pool):
        a = mastery.start_exam(1, goal, db)
        state.record_exam_result(db, a, False)
        a.completed_at = utcnow() - datetime.timedelta(days=1)   # yesterday
        db.commit()
        again = mastery.start_exam(1, goal, db)
    assert again is not None and _texts(again) == _texts(a)


def test_requeue_after_a_failure_excludes_everything_failed_today(db):
    pool = _pool(7, 2)
    goal = db.get(LearningGoal, "G_ast101")
    with patch.object(mastery, "_question_pool", return_value=pool):
        first = mastery.start_exam(1, goal, db)
        state.record_exam_result(db, first, False)
        second = mastery.start_exam(1, goal, db)
        db.commit()

        async def grade():
            events = []
            with patch.object(mastery, "_grade_with_api",
                              return_value={"passed": False, "score": 0, "feedback": "no"}):
                async for e in mastery.stream_grade_exam(second, "Q1: x", 1, db):
                    events.append(e)
            return events
        asyncio.run(grade())
    third = db.query(MasteryAttempt).filter_by(status="pending").one()
    assert not (_texts(third) & _texts(first)) and not (_texts(third) & _texts(second))


def test_requeue_with_no_fresh_questions_says_so(db):
    pool = _pool(4, 2)
    goal = db.get(LearningGoal, "G_ast101")
    with patch.object(mastery, "_question_pool", return_value=pool):
        first = mastery.start_exam(1, goal, db)
        state.record_exam_result(db, first, False)
        second = mastery.start_exam(1, goal, db)
        db.commit()

        async def grade():
            with patch.object(mastery, "_grade_with_api",
                              return_value={"passed": False, "score": 1, "feedback": "no"}):
                return [e async for e in mastery.stream_grade_exam(second, "Q1: x", 1, db)]
        events = asyncio.run(grade())
    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "out of fresh questions" in text and "reopens in about" in text
    assert db.query(MasteryAttempt).filter_by(status="pending").count() == 0


# ---------------------------------------------------------------------------
# B21: infrastructure closes and classifier outages
# ---------------------------------------------------------------------------

def test_missing_goal_voids_the_attempt_without_spending_a_try(db):
    a = state.start_exam_attempt(db, 1, "G_ast101", json.dumps({}))
    db.commit()

    async def grade():
        with patch.object(db, "get", side_effect=lambda model, key: None):
            return [e async for e in mastery.stream_grade_exam(a, "Q1: x", 1, db)]
    asyncio.run(grade())
    assert a.status == "voided" and a.completed_at is None
    assert mastery.attempts_today(1, "G_ast101", db) == 0
    assert mastery.get_active_exam(1, db) is None


@pytest.mark.parametrize("message,expected", [
    ("can we do this later?", "DEFER"),
    ("Not now, I want to ask about eclipses", "DEFER"),
    ("could we talk about eclipses instead?", "DEFER"),
    ("what does Q2 mean?", "CLARIFY"),
    ("I don't understand the second question", "CLARIFY"),
    ("Q1: axial tilt\nQ2: 23.5 degrees\nCode: return d;", "ANSWER"),
    ("I would use parallax instead of brightness", "ANSWER"),
    ("the later phases rise after midnight", "ANSWER"),
])
def test_classifier_outage_falls_back_to_keywords(message, expected):
    broken = MagicMock()
    broken.messages.create.side_effect = RuntimeError("timeout")
    assert mastery.classify_exam_message(message, client=broken) == expected
    garbled = MagicMock()
    garbled.messages.create.return_value = MagicMock(content=[MagicMock(text='{"intent": "MAYBE"}')])
    assert mastery.classify_exam_message(message, client=garbled) == expected


def test_missing_key_uses_the_keyword_fallback(monkeypatch):
    import tutor.config as config

    monkeypatch.setattr(config, "get_config", lambda: {})
    assert mastery.classify_exam_message("can we do this later?") == "DEFER"
