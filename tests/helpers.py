"""Helpers the test modules share (plan step 6.2a, finding F17).

Fixtures live in conftest.py (``db``, ``no_strategist``, the isolation that
runs around every test); these plain helpers are imported:

    from tests.helpers import applied, login_cookie, make_session, pool_bank, scripted_rounds, ToolUse, Usage
"""

from __future__ import annotations

import contextlib
from typing import Iterable, Iterator, Optional
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@contextlib.contextmanager
def applied(patches: Iterable) -> Iterator[None]:
    """Start every patch, and stop them in reverse order whatever happens.

    Two tests stopped their patches in start order. When two patches target
    the same attribute, that restores the wrong original: the first patch's
    stub stayed in place for the rest of the session, and a dozen later tests
    failed in a shuffled order (plan step 6.2a).
    """
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def make_session(*rows) -> Session:
    """An in-memory SQLite session with the schema and ``rows`` committed.

    StaticPool with ``check_same_thread=False``: one connection that every
    thread and session shares. The plain ``sqlite:///:memory:`` recipe gives a
    second thread (a threadpool route, the streaming task) an empty database.
    """
    from tutor.db.models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add_all(rows)
    s.commit()
    return s


def login_cookie(db: Session, student_id: int = 1, method: str = "magic") -> str:
    """A live session cookie for the student, as a real login makes one."""
    from tutor.auth.jwt_utils import create_token
    from tutor.db.models import Session as SessionModel

    token, jti, expires = create_token(student_id)
    db.add(SessionModel(student_id=student_id, jwt_jti=jti, expires_at=expires, method=method))
    db.commit()
    return token


class ToolUse:
    """A tool_use content block, as the SDK's final message carries it."""

    type = "tool_use"

    def __init__(self, name: str, tool_id: str, **tool_input):
        self.name = name
        self.id = tool_id
        self.input = tool_input


class Usage:
    """A response's token counts."""

    def __init__(self, inp: int = 0, out: int = 0, read: int = 0, write: int = 0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = read
        self.cache_creation_input_tokens = write


def scripted_rounds(script: list, captured: Optional[list] = None, usage=None):
    """A fake of the actor's stream_round seam: one (chunks, stop_reason,
    content_blocks) entry per call, the last one repeated. ``captured``
    collects each call's system, messages, and tools."""
    calls = {"n": 0}

    async def fake(system, messages, tools):
        i = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        if captured is not None:
            captured.append({"system": system, "messages": messages, "tools": tools})
        chunks, stop, content = script[i]
        for c in chunks:
            yield ("chunk", c)
        final = MagicMock()
        final.stop_reason = stop
        final.content = list(content)
        final.usage = usage if usage is not None else Usage(100, 20, 30000)
        yield ("final", final)

    return fake


def pool_bank(goal_id: str = "G1", n: int = 8) -> dict:
    """An exam bank entry in the pooled format: n - 2 concept questions and
    two code questions."""
    qs = [{"id": f"c{i}", "kind": "concept", "question": f"concept-{i}?", "rubric": "r"} for i in range(n - 2)]
    qs += [{"id": f"k{i}", "kind": "code", "question": f"code-{i}?", "rubric": "r", "starter": ""} for i in range(2)]
    return {goal_id: {"questions": qs}}
