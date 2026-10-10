"""Session 2 invariant: one pipeline, no bypasses (findings S3, S8).

Mechanical enforcement, in the spirit of the single-writer AST test:
- Anthropic clients are constructed only in two places: the actor client
  (agent/turn_stages/actor_client.py) and the supervisor factory
  (agent/clients.py, plan step 1.3), which every auxiliary caller uses. A new
  endpoint cannot quietly talk to the model directly, and no supervisor call
  runs without a timeout.
- The turn's stages (agent/turn_stages/, plan step 5.2) are imported only by
  turn.py and by each other, and the actor is called only from the actor
  loop and the revise call: nothing can run a stage without the rest of the
  pipeline around it.
- The historical bypass entry points (ask, stream_tokens, the old tutor.py
  module, /api/chat/anon) stay deleted.
- Routers never import anthropic.
- The spend caps live inside run_turn, so every entry point inherits them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from tutor.db.models import Student, TurnEvent
from tests.helpers import make_session

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "tutor"

STAGES = APP / "agent" / "turn_stages"
ACTOR_CLIENT = STAGES / "actor_client.py"

# The only modules allowed to construct an Anthropic client. actor_client.py
# is the actor (streaming, its own timeout); clients.py is the supervisor
# factory with a bounded timeout and one retry. Classifier, judge, strategist,
# summarizer, exam grader, rubric evaluator, vision, misconception mining, and
# the health ping all call supervisor_client().
_CLIENT_ALLOWED = {
    ACTOR_CLIENT,
    APP / "agent" / "clients.py",
}


def _py_files():
    return [p for p in APP.rglob("*.py")]


def test_anthropic_client_constructed_only_in_allowed_modules():
    offenders = []
    for path in _py_files():
        src = path.read_text()
        if ("anthropic.Anthropic(" in src or "anthropic.AsyncAnthropic(" in src) \
                and path not in _CLIENT_ALLOWED:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"unexpected Anthropic client construction in: {offenders}"


def test_supervisor_client_is_bounded():
    import httpx

    from tutor.agent.clients import SUPERVISOR_TIMEOUT, supervisor_client

    c = supervisor_client(api_key="test-key")
    assert c.max_retries == 1
    assert isinstance(c.timeout, httpx.Timeout)
    assert c.timeout.read == 30.0 and c.timeout.connect == 5.0
    assert SUPERVISOR_TIMEOUT.read == 30.0
    longer = supervisor_client(api_key="test-key", timeout=60.0)
    assert longer.timeout.read == 60.0 and longer.timeout.connect == 5.0
    assert longer.max_retries == 1


def test_async_actor_client_only_in_the_actor_client():
    """The streaming single-call actor (AsyncAnthropic) lives only in
    actor_client.py; the auxiliary callers (classifier, judge, grader,
    evaluator) stay sync."""
    for path in _py_files():
        if "AsyncAnthropic(" in path.read_text():
            assert path == ACTOR_CLIENT, \
                f"unexpected async client in {path.relative_to(ROOT)}"


def _imported_modules(tree) -> set[str]:
    """Every module a file imports, as dotted names (``from a import b``
    counts as both ``a`` and ``a.b``)."""
    import ast

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_turn_stages_are_reached_only_through_run_turn():
    """Only the orchestrator (turn.py) and the stages themselves import a
    stage, so no endpoint can run the actor loop without the supervision and
    persistence around it (plan step 5.2)."""
    import ast

    offenders = []
    for path in _py_files():
        if path == APP / "agent" / "turn.py" or path.parent == STAGES:
            continue
        imported = _imported_modules(ast.parse(path.read_text()))
        if any(m == "tutor.agent.turn_stages" or m.startswith("tutor.agent.turn_stages.")
               for m in imported):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_the_actor_is_called_only_by_the_loop_and_the_revise():
    """stream_round (the actor's streaming rounds) is used only by the actor
    loop, and generate_once (the buffered revise) only by supervision."""
    import ast

    users: dict[str, set[str]] = {"stream_round": set(), "generate_once": set()}
    for path in _py_files():
        if path == ACTOR_CLIENT:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            name = (node.attr if isinstance(node, ast.Attribute)
                    else node.id if isinstance(node, ast.Name) else None)
            if name in users:
                users[name].add(path.name)
    assert users == {"stream_round": {"actor_loop.py"}, "generate_once": {"supervision.py"}}


def test_run_turn_stays_an_orchestrator():
    """run_turn and its body only sequence the stages (plan step 5.2: under
    200 lines together, down from 827 in the review)."""
    import ast

    tree = ast.parse((APP / "agent" / "turn.py").read_text())
    sizes = {node.name: node.end_lineno - node.lineno + 1 for node in tree.body
             if isinstance(node, ast.AsyncFunctionDef)}
    assert set(sizes) == {"run_turn", "_run_turn_body"}
    assert sum(sizes.values()) < 200, sizes


def test_bypass_entry_points_stay_deleted():
    from tutor.agent import turn

    assert not hasattr(turn, "ask")
    assert not hasattr(turn, "stream_tokens")
    assert not (APP / "agent" / "tutor.py").exists()
    api_src = (APP / "routers" / "api.py").read_text()
    assert "chat/anon" not in api_src and "chat_anon" not in api_src


def test_routers_never_import_anthropic():
    for path in (APP / "routers").glob("*.py"):
        src = path.read_text()
        assert "import anthropic" not in src, f"{path.name} imports anthropic"


def test_run_turn_enforces_caps_before_anything_else():
    db = make_session()
    db.add(Student(id=1, email="capped@example.edu", name="Capped"))
    db.commit()
    student = db.get(Student, 1)

    from tutor.agent import turn

    with patch("tutor.agent.usage.over_caps", return_value="limit reached, friend"):
        async def run():
            return [e async for e in turn.run_turn(
                student=student, db=db, user_message="hi", course="AST101",
            )]
        events = asyncio.run(run())

    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert text == "limit reached, friend"
    # Nothing ran: no chat TurnEvent, no persisted exchange.
    assert db.query(TurnEvent).count() == 0
    db.close()


def test_cli_mode_refuses_production_database(monkeypatch):
    import tutor.config as config

    monkeypatch.setenv("USE_CLAUDE_CLI", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/prod")
    config.get_config.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="unsupervised local dev mode"):
            config.get_config()
    finally:
        config.get_config.cache_clear()
