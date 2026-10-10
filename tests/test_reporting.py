"""Daily per-user activity report aggregation + markdown rendering.

Network-free. Exercises the pure functions in tutor/reporting.py with lightweight
stand-in rows (SimpleNamespace) so no DB or ORM is needed.
"""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace as NS

from tutor.reporting import (
    build_user_dailies,
    render_digest_markdown,
    render_user_markdown,
    safe_filename,
)

T0 = datetime.datetime(2026, 7, 17, 10, 0, 0)


def _msg(sid, role, minute=0):
    return NS(student_id=sid, role=role, created_at=T0 + datetime.timedelta(minutes=minute))


def _turn(sid, kind="chat", cost=0.01, latency=1000, verdicts=None, minute=0):
    return NS(
        student_id=sid, kind=kind, cost_usd=cost, latency_ms=latency,
        verdicts_json=json.dumps(verdicts) if verdicts is not None else None,
        ts=T0 + datetime.timedelta(minutes=minute),
    )


def _attempt(sid, status):
    return NS(student_id=sid, status=status)


STUDENTS = [
    NS(id=1, email="a@example.edu", name="Ana"),
    NS(id=2, email="b@example.edu", name="Bo"),
    NS(id=3, email="c@example.edu", name="Cy"),   # no activity
]


def test_aggregates_per_student():
    messages = [_msg(1, "user"), _msg(1, "user"), _msg(1, "assistant"), _msg(2, "user")]
    turns = [
        _turn(1, cost=0.02, latency=1000),
        _turn(1, cost=0.03, latency=30000),                       # slow
        _turn(1, kind="error", cost=0.0, latency=500),            # error
        _turn(2, cost=0.01, latency=2000, verdicts={"miscalibrated": True}),
    ]
    attempts = [_attempt(1, "passed"), _attempt(1, "failed"), _attempt(2, "passed")]

    out = build_user_dailies(STUDENTS, messages, turns, attempts, slow_ms=25000)
    by = {u.student_id: u for u in out}

    assert set(by) == {1, 2}                     # student 3 (no activity) excluded
    a = by[1]
    assert a.turns == 2                           # only kind=="chat" counts as a turn
    assert a.user_messages == 2
    assert a.exams_passed == 1 and a.exams_failed == 1
    assert round(a.total_cost_usd, 4) == 0.05     # 0.02 + 0.03 + 0.0
    assert a.slow_turns == 1
    assert a.errors == 1
    assert a.max_latency_ms == 30000
    assert a.avg_latency_ms == 10500              # (1000+30000+500)/3

    b = by[2]
    assert b.high_conf_misses == 1
    assert b.slow_turns == 0


def test_sorted_busiest_first():
    messages = [_msg(2, "user")]
    turns = [_turn(1), _turn(1), _turn(1), _turn(2)]
    out = build_user_dailies(STUDENTS, messages, turns, [], slow_ms=25000)
    assert [u.student_id for u in out] == [1, 2]  # 3 turns before 1 turn


def test_user_markdown_has_fields_and_flags():
    turns = [_turn(1, latency=40000)]  # slow
    out = build_user_dailies([STUDENTS[0]], [], turns, [], slow_ms=25000)
    md = render_user_markdown(out[0], "2026-07-17")
    assert "# Activity — Ana" in md
    assert "a@example.edu" in md
    assert "Chat turns" in md
    assert "## Flags" in md and "slow turn" in md.lower()


def test_digest_totals_and_rows():
    turns = [_turn(1, cost=0.02), _turn(2, cost=0.03, latency=40000)]
    out = build_user_dailies(STUDENTS, [], turns, [], slow_ms=25000)
    digest = render_digest_markdown(out, "2026-07-17")
    assert "Daily tutor report — 2026-07-17" in digest
    assert "Active students:** 2" in digest
    assert "Ana" in digest and "Bo" in digest
    assert "⚠️" in digest  # Bo had a slow turn


def test_digest_empty_window():
    digest = render_digest_markdown([], "2026-07-17")
    assert "No student activity" in digest


def test_safe_filename():
    assert safe_filename("a.b+c@example.edu") == "a.b_c@example.edu"
    assert safe_filename("") == "unknown"
    assert "/" not in safe_filename("../../etc/passwd")


# ---------------------------------------------------------------------------
# Quiet-day suppression (no activity, no learning signals -> no email)
# ---------------------------------------------------------------------------

def test_is_quiet_day_rules():
    import datetime
    from scripts.daily_report import is_quiet_day
    from tutor.learning_metrics import LearningMetrics

    now = datetime.datetime(2026, 9, 6, 12)
    empty = LearningMetrics(since=now - datetime.timedelta(days=1), until=now)
    assert is_quiet_day([], empty) is True
    assert is_quiet_day([object()], empty) is False            # any active student -> send
    assert is_quiet_day([], None) is False                     # metrics failure -> send the note
    busy = LearningMetrics(since=empty.since, until=now, retention_reviews=1)
    assert is_quiet_day([], busy) is False                     # a review-only day still reports
    pred = LearningMetrics(since=empty.since, until=now, predictions=2)
    assert is_quiet_day([], pred) is False


def test_quiet_day_sends_one_line_and_writes_files(tmp_path, monkeypatch):
    import scripts.daily_report as dr
    import tutor.config as config
    from sqlalchemy import create_engine
    from tutor.db.models import Base

    db_path = tmp_path / "empty.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    monkeypatch.setenv("TUTOR_DB", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")   # email IS configured...
    config.get_config.cache_clear()
    sent, quiet = [], []
    monkeypatch.setattr(dr, "_email_digest", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(dr, "_email_quiet", lambda *a, **k: quiet.append(a))
    try:
        assert dr.main(["--out", str(tmp_path / "reports"), "--hours", "24"]) == 0
        assert sent == []                                       # ...but nothing to say:
        assert len(quiet) == 1                                  # the one-line note instead (decision 10)
        assert list((tmp_path / "reports").rglob("digest.md"))  # files still written
        assert dr.main(["--out", str(tmp_path / "reports"), "--hours", "24", "--always-email"]) == 0
        assert len(sent) == 1 and len(quiet) == 1               # override forces the full digest
    finally:
        config.get_config.cache_clear()


def test_every_failed_turn_counts_as_an_error():
    """The digest used to count kind=error rows and a verdicts 'error' key
    only, so an actor outage (stream_error), an empty reply, a crash
    finalized on the provisional row, a grader outage and a fail-closed judge
    were not errors (finding F48)."""
    turns = [
        _turn(1, verdicts={"verdict": "stream_error", "stream_error": True, "stream_error_kind": "overloaded"}),
        _turn(1, verdicts={"verdict": "empty_reply"}),
        _turn(1, verdicts={"verdict": "crash", "error": "paused DB"}),
        _turn(1, kind="exam", verdicts={"grading_error": "no API key configured"}),
        _turn(1, verdicts={"verdict": "block", "judge_rule": "judge_unavailable", "judge_unavailable": True}),
        _turn(1, verdicts={"verdict": "allow"}),
        _turn(1, kind="conduct", cost=0.0, verdicts={"action": "restrict"}),
    ]
    (a,) = build_user_dailies(STUDENTS, [], turns, [], slow_ms=25000)
    assert a.errors == 5
    assert a.restricted == 1
    md = render_user_markdown(a, "2026-10-07")
    assert "5 error turn(s)" in md and "restricted after repeated hostile messages" in md


def test_the_digest_links_flagged_students_when_it_knows_the_site():
    turns = [_turn(1, kind="error", cost=0.0, latency=500), _turn(2, latency=1000)]
    reports = build_user_dailies(STUDENTS, [_msg(2, "user")], turns, [], slow_ms=25000)
    plain = render_digest_markdown(reports, "2026-10-07")
    assert "Flagged conversations" not in plain
    linked = render_digest_markdown(reports, "2026-10-07", base_url="https://tutor.example.edu/")
    assert "## Flagged conversations" in linked
    assert "- Ana: https://tutor.example.edu/admin?tab=conversations&student=a%40example.edu" in linked
    assert "Bo:" not in linked.split("## Flagged conversations")[1]
