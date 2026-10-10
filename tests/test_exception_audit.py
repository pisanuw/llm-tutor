"""Plan step 5.6 (finding A13): the exception audit.

A13: broad ``except Exception`` handlers swallowed failures silently, some
around database writes (a logout that did not revoke, a crash that was not
ledgered), and fail-soft reads left the session unusable on Postgres, where
a failed statement aborts the whole transaction. Now a write's handler rolls
back and logs at warning with the turn id, and an optional read runs in a
savepoint (tutor.db.guard.best_effort).
Done when: no bare pass around a database write (the first test, by AST).
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tutor.db.guard import best_effort
from tutor.db.models import Base, Message, Student, TurnEvent

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Names that write: session methods, the single writer's transitions, the
# turn's persistence steps.
_WRITE_NAMES = {"add", "add_all", "commit", "flush", "delete", "merge", "bulk_save_objects", "db_step",
                "save_user_message", "save_assistant_message", "save_reply", "record_learning"}
_WRITE_PREFIXES = ("record_", "_record_", "save_", "finalize_", "defer_", "set_", "mark_", "unlock_",
                   "resolve_", "enroll", "unenroll", "create_", "delete_", "add_to_", "remove_from",
                   "start_exam", "_stage")
_NOT_WRITES = {"set_cookie", "delete_cookie", "setdefault"}


def _broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    names = [] if t is None else list(t.elts) if isinstance(t, ast.Tuple) else [t]
    return t is None or any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in names)


def _silent(handler: ast.ExceptHandler) -> bool:
    for s in handler.body:
        if isinstance(s, (ast.Pass, ast.Continue, ast.Break)):
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
            continue
        if isinstance(s, ast.Return) and (s.value is None or isinstance(s.value, (ast.Constant, ast.Name))):
            continue
        return False
    return True


def _writes(body: list) -> bool:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name and name not in _NOT_WRITES and (name in _WRITE_NAMES or name.startswith(_WRITE_PREFIXES)):
            return True
    return False


def test_no_silent_handler_around_a_database_write():
    offenders = []
    for path in sorted((ROOT / "tutor").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Try) and _writes(node.body):
                for h in node.handlers:
                    if _broad(h) and _silent(h):
                        offenders.append(f"{path.relative_to(ROOT)}:{h.lineno}")
    assert offenders == []


def test_the_scan_would_catch_one():
    """The detector flags the pattern it exists for (the old logout)."""
    tree = ast.parse("try:\n    sess.revoked = True\n    db.commit()\nexcept Exception:\n    pass\n")
    node = tree.body[0]
    assert _writes(node.body) and _broad(node.handlers[0]) and _silent(node.handlers[0])


# ---------------------------------------------------------------------------
# best_effort: a savepoint, a warning, the default
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.commit()
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cur, stmt, params, ctx, many: statements.append(stmt))
    s.info["statements"] = statements
    yield s
    s.close()


def test_best_effort_keeps_the_session_and_its_pending_writes(db, tutor_log):
    db.add(Message(student_id=1, role="user", content="pending", course="AST101"))
    db.flush()
    got = best_effort(db, lambda: db.execute(text("SELECT * FROM no_such_table")).all(), "fallback",
                      "the optional read", turn_id="t123")
    assert got == "fallback"
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in db.info["statements"])
    assert "the optional read failed (turn t123)" in tutor_log.text
    assert best_effort(db, lambda: db.query(Student).count(), 0, "a good read") == 1
    db.commit()
    assert db.query(Message).filter_by(content="pending").count() == 1


def test_a_failed_optional_read_does_not_fail_the_turns_step(db, tutor_log):
    from tutor.agent.turn_stages import context
    from tutor.agent.turn_stages.request import TurnRequest

    req = TurnRequest(student=db.get(Student, 1), user_message="what is a light-year?", course="AST101")

    def broken_summary(student_id, session, course):
        session.execute(text("SELECT * FROM no_such_table"))

    with patch("tutor.agent.progress.progress_summary", side_effect=broken_summary):
        ctx, messages = asyncio.run(context.read(db, req))
    assert "STUDENT CONTEXT" in ctx and messages[-1]["content"].endswith("what is a light-year?")
    assert f"the progress summary failed (turn {req.turn_id})" in tutor_log.text
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in db.info["statements"])


def test_the_navigator_facts_fall_back_one_at_a_time(db, tutor_log):
    from tutor.agent import pacing

    with patch("tutor.agent.pacing._most_decayed_mastered", side_effect=RuntimeError("bad row")), \
         patch("tutor.agent.navigator.next_goals", return_value=[]):
        facts = pacing.navigator_facts(1, "AST101", db)
    assert facts["decayed_goal_id"] is None and facts["top_goal_id"] is None
    assert facts["pacing_ok"] is True
    assert "navigator facts: most-decayed goal failed" in tutor_log.text
    assert "next goal failed" not in tutor_log.text


# ---------------------------------------------------------------------------
# Writes: roll back, and say so
# ---------------------------------------------------------------------------

def test_logout_clears_the_cookie_and_logs_when_the_revoke_fails(db, tutor_log):
    from fastapi.testclient import TestClient

    import tutor.main as main_mod
    from tutor.auth.deps import get_db
    from tutor.auth.jwt_utils import create_token
    from tutor.db.models import Session as SessionModel

    token, jti, expires = create_token(1)
    db.add(SessionModel(student_id=1, jwt_jti=jti, expires_at=expires, method="magic"))
    db.commit()
    main_mod.app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(main_mod.app)
        client.cookies.set("session_token", token)
        with patch.object(db, "commit", side_effect=RuntimeError("paused DB")):
            resp = client.post("/auth/logout", follow_redirects=False)
    finally:
        main_mod.app.dependency_overrides.clear()
    assert resp.status_code == 303
    assert any(c.startswith('session_token=""') or c.startswith("session_token=;")
               for c in resp.headers.get_list("set-cookie"))
    assert "logout: could not revoke the session" in tutor_log.text


def test_a_crash_that_cannot_be_ledgered_is_logged(monkeypatch, tutor_log):
    from fastapi.testclient import TestClient

    import tutor.agent.turn as turn_mod
    from tutor.auth.deps import get_current_student, get_db
    from tutor.main import app
    from tutor.routers import chat as chat_router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    student = Student(email="crash@example.edu", name="Crash")
    session.add(student)
    session.commit()

    async def crashing_turn(*, student, db, user_message, course, **_):
        yield {"type": "token", "text": "Partial "}
        raise RuntimeError("the tutor broke")

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: student
    monkeypatch.setattr(chat_router, "_turn_session", lambda: Session())
    monkeypatch.setattr(turn_mod, "run_turn", crashing_turn)
    try:
        with patch("tutor.agent.state.record_turn_event", side_effect=RuntimeError("ledger down")):
            resp = TestClient(app).post("/chat/message", data={"message": "what is a light-year?", "course": "AST101"})
    finally:
        app.dependency_overrides.clear()
        session.close()
    assert '"type": "error"' in resp.text                       # the student still sees the error
    assert "could not record the crash" in tutor_log.text
    assert session.query(TurnEvent).count() == 0


def test_the_strategist_and_the_summary_log_their_failures(db, tutor_log):
    from tutor.agent.envelope import EnvelopeBundle
    from tutor.agent.policy import Envelope, Intent
    from tutor.agent.preclassify import Classification
    from tutor.agent.turn_stages import persist, preflight
    from tutor.agent.turn_stages.request import TurnRequest

    req = TurnRequest(student=db.get(Student, 1), user_message="hi", course="AST101")
    bundle = EnvelopeBundle(envelope=Envelope(max_rung=4, goals=[], exam_lockdown=[], must_ground=True,
                                              allow_exercise=True, escalate_review=False, reason="t"),
                            goal_titles={}, gate_goal=None)
    with patch("tutor.agent.trajectory.compute_trajectory", side_effect=RuntimeError("db hiccup")), \
         patch("tutor.agent.memory.maybe_summarize_async", side_effect=RuntimeError("summarizer down")):
        asyncio.run(preflight.consult_strategist(db, req, bundle, Classification(intent=Intent.QUESTION)))
        asyncio.run(persist.summarize(db, req))
    assert f"turn {req.turn_id}: the strategist failed" in tutor_log.text
    assert f"turn {req.turn_id}: the rolling summary was not updated" in tutor_log.text
