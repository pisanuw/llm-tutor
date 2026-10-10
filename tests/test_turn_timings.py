"""Where a turn's time goes (latency plan, Phase A).

The daily report could say a turn took 45 s, but not whether that was the
model, a course-materials search, or the judge. Each turn now stores its
stage timings on the TurnEvent (verdicts "timings"), and the report shows the
time to the first word and the cold-cache turns.
"""
from __future__ import annotations

import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tutor import reporting
from tutor.db.models import Student, TurnEvent
from tests.test_two_stage_persistence import _Usage, _patches, _run, db  # noqa: F401


def _tool_then_answer():
    """Round 1 asks for find_topic, round 2 answers."""
    calls = {"n": 0}

    async def fake(system, messages, tools):
        calls["n"] += 1
        final = MagicMock()
        final.usage = _Usage()
        final.model = None
        if calls["n"] == 1:
            block = MagicMock()
            block.type, block.name, block.id = "tool_use", "find_topic", "t1"
            block.input = {"query": "seasons"}
            final.stop_reason, final.content = "tool_use", [block]
        else:
            yield ("chunk", "Trace the Sun's path first.")
            final.stop_reason, final.content = "end_turn", []
        yield ("final", final)
    return fake


def test_a_turn_stores_its_stage_timings(db):  # noqa: F811
    tool = patch("tutor.agent.tools.dispatch_tool", return_value={"results": []})
    _run(db, _patches(_tool_then_answer(), [tool]))
    ev = db.query(TurnEvent).filter_by(kind="chat").one()
    timings = json.loads(ev.verdicts_json)["timings"]
    for key in ("preflight_done_at_ms", "first_model_text_at_ms", "first_token_at_ms",
                "actor_ms", "tool_find_topic_ms"):
        assert isinstance(timings[key], int) and timings[key] >= 0, key
    assert timings["preflight_done_at_ms"] <= timings["first_token_at_ms"]
    assert "judge_ms" not in timings            # a routine turn is not judged


def _event(student_id, *, latency, ttft=None, cache_write=0):
    verdicts = {"verdict": "allow"}
    if ttft is not None:
        verdicts["timings"] = {"first_token_at_ms": ttft}
    return SimpleNamespace(student_id=student_id, kind="chat", cost_usd=0.1, latency_ms=latency,
                           verdicts_json=json.dumps(verdicts), cache_write_tokens=cache_write,
                           ts=datetime.datetime(2026, 10, 2, 12))


def test_the_report_shows_first_word_and_cold_turns():
    students = [Student(id=1, email="a@example.edu", name="Ann")]
    events = [_event(1, latency=40000, ttft=31000, cache_write=44303),
              _event(1, latency=9000, ttft=2500),
              _event(1, latency=8000)]               # from before timings were stored
    (u,) = reporting.build_user_dailies(students, [], events, [], slow_ms=25000)
    assert u.max_ttft_ms == 31000 and u.cold_turns == 1 and u.slow_turns == 1
    digest = reporting.render_digest_markdown([u], "2026-10-02")
    assert "| First word | Cold |" in digest
    assert "| 31.0s | 1 | 1 | 0 |" in digest
    assert "Max time to first word | 31.0s" in reporting.render_user_markdown(u, "2026-10-02")


def test_old_turns_without_timings_show_a_dash():
    (u,) = reporting.build_user_dailies([Student(id=1, email="a@example.edu", name="Ann")], [],
                                        [_event(1, latency=5000)], [], slow_ms=25000)
    assert u.max_ttft_ms is None and u.cold_turns == 0
    assert "| — | 0 |" in reporting.render_digest_markdown([u], "d")


def test_the_strategist_reads_run_while_the_classifier_is_in_flight(db):  # noqa: F811
    """B2: the classifier's model call and the strategist's ledger reads
    overlap. The fake classifier waits for the reads to start; run one after
    the other, it would time out and the turn would see no overlap."""
    import threading

    from tutor.agent.policy import Intent
    from tutor.agent.preclassify import Classification
    from tutor.agent import trajectory

    reads_started = threading.Event()
    overlapped = []
    real_compute = trajectory.compute_trajectory

    def compute(db_, sid):
        reads_started.set()
        return real_compute(db_, sid)

    def classify(*a, **kw):
        overlapped.append(reads_started.wait(timeout=5))
        return Classification(intent=Intent.QUESTION)

    from tests.test_two_stage_persistence import _round

    patches = _patches(_round()) + [
        patch("tutor.agent.preclassify.classify", side_effect=classify),
        patch("tutor.agent.trajectory.compute_trajectory", side_effect=compute),
    ]
    _run(db, patches)
    assert overlapped == [True]
    timings = json.loads(db.query(TurnEvent).filter_by(kind="chat").one().verdicts_json)["timings"]
    assert "classify_ms" in timings and "strategist_ms" in timings


def test_a_failed_strategist_read_still_answers_with_no_move(db, tutor_log):  # noqa: F811
    from tests.test_two_stage_persistence import _round

    boom = patch("tutor.agent.trajectory.compute_trajectory", side_effect=RuntimeError("db hiccup"))
    events = _run(db, _patches(_round(), [boom]))
    assert events[-1]["type"] == "done"
    assert "the strategist failed; no move this turn" in tutor_log.text
