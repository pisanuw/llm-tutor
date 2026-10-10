"""Session 22 (Task 7): the nightly auditor.

Pure helpers run everywhere; the orchestration uses an injected fake client so
no network is touched. Exit gate: a report is generated against seeded data and
the disagreement queue is visible in admin.
"""
from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import pytest

from pipeline import audit_turns as A
from tutor.db.models import JudgeSample, Message, Student, TurnEvent
from tutor.utctime import utcnow
from tests.helpers import make_session


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_live_verdict_defaults_to_allow():
    assert A.live_verdict(None) == "allow"
    assert A.live_verdict(json.dumps({"score": 3})) == "allow"
    assert A.live_verdict(json.dumps({"verdict": "block"})) == "block"


def test_is_risky():
    assert A.is_risky(json.dumps({"verdict": "block"}), None)
    assert A.is_risky(json.dumps({"verdict": "allow"}), None)  # judged at all
    assert A.is_risky(None, json.dumps({"cut": True}))
    assert not A.is_risky(None, None)
    assert not A.is_risky(json.dumps({"score": 2}), json.dumps({"cut": False}))


def test_classify_directions():
    assert A.classify("allow", "block") == "under_block"
    assert A.classify("allow", "revise") == "under_block"
    assert A.classify("block", "allow") == "over_block"
    assert A.classify("revise", "revise") == "agree"


def test_sample_routine_deterministic():
    ids = list(range(1, 101))
    a = A.sample_routine(ids, 0.1, seed=0)
    b = A.sample_routine(ids, 0.1, seed=0)
    assert a == b
    assert len(a) == 10
    assert A.sample_routine(ids, 0.0) == []
    assert A.sample_routine([], 0.5) == []
    assert set(A.sample_routine(ids, 1.0)) == set(ids)


def test_reconstruct_envelope():
    env = A.reconstruct_envelope(json.dumps({
        "max_rung": 3, "move": "ELICIT_PREDICTION", "exam_lockdown": ["G1"],
        "must_ground": True, "allow_exercise": False, "goals": ["G2"],
    }))
    assert env.max_rung == 3
    assert env.move is not None and env.move.value == "ELICIT_PREDICTION"
    assert env.exam_lockdown == ["G1"]
    # bad move value degrades to None, not a crash
    env2 = A.reconstruct_envelope(json.dumps({"max_rung": 1, "move": "NOPE"}))
    assert env2.move is None


def test_render_report_has_sections():
    rep = A.AuditReport(generated_at="2026-06-12", hours=24, n_risky=2,
                        n_routine_sampled=1, n_audited=3, agree=2, under_block=1)
    rep.rows = [{"turn_event_id": 5, "student": "s7", "live": "allow",
                 "audit": "block", "category": "under_block", "reason": "leak"}]
    md = A.render_report(rep)
    assert "# Turn audit" in md
    assert "under-block" in md
    assert "Cache health" in md
    assert "| 5 | s7 | allow | block |" in md  # disagreement row rendered


def test_student_pseudonym():
    assert A.student_pseudonym(42) == "s42"
    assert A.student_pseudonym(None) == "s?"


# ---------------------------------------------------------------------------
# DB orchestration with a fake client
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    s = make_session()
    yield s
    s.close()


def _fake_client(verdict_for):
    class _Msgs:
        def create(self, **kw):
            draft = kw["messages"][0]["content"]
            v = verdict_for(draft)
            text = json.dumps({"verdict": v, "rule": "r", "reason": "auditor said " + v})
            return SimpleNamespace(content=[SimpleNamespace(text=text)], usage=None)
    return SimpleNamespace(messages=_Msgs())


def _seed_turn(db, student_id, ts, *, verdicts=None, draft="hello", envelope=None):
    te = TurnEvent(
        student_id=student_id, ts=ts, kind="chat",
        verdicts_json=json.dumps(verdicts) if verdicts else None,
        envelope_json=json.dumps(envelope or {"max_rung": 3, "goals": []}),
        in_tokens=100, out_tokens=50, cache_read_tokens=400,
    )
    db.add(te)
    db.add(Message(student_id=student_id, role="assistant", content=draft, created_at=ts))
    db.flush()
    return te


def test_cache_health_alarm(db):
    s = Student(email="s@example.edu", name="t")
    db.add(s)
    db.flush()
    now = utcnow()
    # Mostly uncached -> below 60% -> alarm.
    db.add(TurnEvent(student_id=s.id, ts=now, kind="chat", in_tokens=1000, cache_read_tokens=100))
    db.flush()
    share, alarm = A.cache_health(db)
    assert alarm is True
    assert share < 0.6


def test_run_audit_detects_under_block(db):
    s = Student(email="s@example.edu", name="t")
    db.add(s)
    db.flush()
    now = utcnow()
    recent = now - datetime.timedelta(hours=1)

    # One risky turn the live judge allowed, but whose draft leaks a solution.
    _seed_turn(db, s.id, recent, verdicts={"verdict": "allow"}, draft="here is the REVEAL solution")
    # One risky turn both agree to allow (distinct time so draft matching is unambiguous).
    _seed_turn(db, s.id, recent - datetime.timedelta(minutes=2),
               verdicts={"verdict": "allow"}, draft="a gentle hint")
    db.flush()

    client = _fake_client(lambda d: "block" if "REVEAL" in d else "allow")
    report = A.run_audit(db, hours=24, client=client, now=now)

    # Plan 0.3 (E4): rows and the rendered report carry s<id>, never the email.
    assert {r["student"] for r in report.rows} == {f"s{s.id}"}
    md = A.render_report(report)
    assert f"| s{s.id} |" in md
    assert "@" not in md and "example.edu" not in md

    assert report.n_risky == 2
    assert report.n_audited == 2
    assert report.under_block == 1
    assert report.agree == 1
    # JudgeSamples written under the auditor label.
    samples = db.query(JudgeSample).filter(JudgeSample.judge.like("auditor:%")).all()
    assert len(samples) == 2


def test_run_audit_samples_routine(db):
    s = Student(email="s@example.edu", name="t")
    db.add(s)
    db.flush()
    now = utcnow()
    recent = now - datetime.timedelta(hours=2)
    for _ in range(10):
        _seed_turn(db, s.id, recent, verdicts=None, draft="routine chat")
    db.flush()

    client = _fake_client(lambda d: "allow")
    report = A.run_audit(db, hours=24, sample_rate=0.1, client=client, now=now, seed=0)
    assert report.n_risky == 0
    assert report.n_routine_sampled == 1  # 10 * 0.1
    assert report.n_audited == 1


def test_run_audit_skips_old_turns(db):
    s = Student(email="s@example.edu", name="t")
    db.add(s)
    db.flush()
    now = utcnow()
    old = now - datetime.timedelta(hours=48)
    _seed_turn(db, s.id, old, verdicts={"verdict": "allow"}, draft="old")
    db.flush()
    report = A.run_audit(db, hours=24, client=_fake_client(lambda d: "allow"), now=now)
    assert report.n_risky == 0 and report.n_audited == 0


# ---------------------------------------------------------------------------
# Disagreement queue visible in admin
# ---------------------------------------------------------------------------

def test_admin_shows_auditor_disagreement(monkeypatch):
    from fastapi.testclient import TestClient
    import tutor.config as config

    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()
    from tutor.main import app
    from tutor.auth.deps import get_db

    session = make_session()
    try:
        s = Student(email="s@example.edu", name="t")
        session.add(s)
        session.flush()
        te = TurnEvent(student_id=s.id, ts=utcnow(), kind="chat",
                       verdicts_json=json.dumps({"verdict": "allow"}))
        session.add(te)
        session.flush()
        session.add(JudgeSample(turn_event_id=te.id, judge=f"auditor:{A.AUDITOR.id}",
                                verdict="block", reason="auditor caught a leak"))
        session.commit()

        app.dependency_overrides[get_db] = lambda: session
        client = TestClient(app)
        from tutor.routers.admin import issue_admin_session
        client.cookies.set("admin_session", issue_admin_session())
        body = client.get("/admin?tab=supervision").text
        assert "Auditor disagreements" in body
        assert "auditor caught a leak" in body
    finally:
        app.dependency_overrides.clear()
        session.close()
        config.get_config.cache_clear()


# ---------------------------------------------------------------------------
# Plan 0.3 (E4): the report never reaches git
# ---------------------------------------------------------------------------

def test_no_workflow_pushes_and_audit_is_read_only():
    """A push from a workflow lands on main and redeploys production, and git
    history is out of reach of erasure. The audit report is an artifact."""
    import pathlib

    wf_dir = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
    files = sorted(wf_dir.glob("*.yml"))
    assert files, "no workflows found"
    for wf in files:
        text = wf.read_text()
        assert "git push" not in text, f"{wf.name} pushes to the repository"
        assert "contents: write" not in text, f"{wf.name} asks for contents: write"

    audit = (wf_dir / "audit.yml").read_text()
    assert "contents: read" in audit
    assert "actions/upload-artifact@" in audit
    assert "retention-days:" in audit


def test_dated_reports_are_gitignored():
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        pytest.skip("not a git checkout")
    probe = "docs/audit/2026-01-01.md"
    res = subprocess.run(["git", "check-ignore", "-q", probe], cwd=root)
    assert res.returncode == 0, f"{probe} is not gitignored"
    res = subprocess.run(["git", "check-ignore", "-q", "docs/audit/README.md"], cwd=root)
    assert res.returncode == 1, "the audit README must stay tracked"
