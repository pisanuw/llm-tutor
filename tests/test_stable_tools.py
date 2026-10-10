"""Plan step 2.3 (finding A9): one tool list on every actor call.

Tool definitions sit in front of the cached persona prefix, so a call that
declared fewer tools (an envelope filter) or none (the revise call) paid a
fresh ~40K-token cache write on exactly the expensive turns. Now every call
declares TOOL_DEFINITIONS; a call that must not start a tool round uses
tool_choice "none"; the envelope's limits are refused server-side.
Done when: cache reads are non-zero on a revise regeneration, which here
means the revise call's cacheable prefix (tools + persona block) is
identical to the streaming round's.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


from tutor.agent import turn as agent
from tutor.agent.envelope import EnvelopeBundle
from tutor.agent.judge import Verdict
from tutor.agent.policy import Envelope, Intent
from tutor.agent.preclassify import Classification
from tutor.agent.tools import TOOL_DEFINITIONS, dispatch_tool, tools_for_envelope
from tutor.db.models import Student
from tutor.agent.turn_stages import actor_client
from tests.helpers import ToolUse


def _env(lockdown=(), allow_exercise=True, max_rung=3, escalate=False):
    return Envelope(max_rung=max_rung, goals=["G1"], exam_lockdown=list(lockdown),
                    must_ground=True, allow_exercise=allow_exercise,
                    escalate_review=escalate, reason="t")


def test_dispatch_refuses_a_tool_outside_the_allowed_set():
    out = dispatch_tool("find_topic", {"query": "eclipses"}, allowed={"evaluate_code"})
    assert out["refused"] is True and "not available" in out["error"]


def test_envelope_names_the_tools_that_are_off():
    from tutor.agent.prompt_builder import render_envelope

    text = render_envelope(_env(lockdown=["G1"], allow_exercise=False), {})
    assert "Tools off this turn (calls are refused): get_exercise, find_topic." in text \
        or "Tools off this turn (calls are refused): find_topic, get_exercise." in text
    assert "Tools off this turn" not in render_envelope(_env(), {})


def test_generate_once_declares_every_tool_with_choice_none():
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text="draft")]
    resp.usage = None
    resp.model = "claude-sonnet-4-6"
    client.messages.create.return_value = resp
    with patch.object(actor_client, "client", return_value=client):
        actor_client.generate_once([{"type": "text", "text": "PERSONA"}], [{"role": "user", "content": "hi"}])
    kw = client.messages.create.call_args.kwargs
    assert kw["tools"] is TOOL_DEFINITIONS
    assert kw["tool_choice"] == {"type": "none"}


class _FakeStream:
    def __init__(self):
        self.text_stream = self._gen()

    async def _gen(self):
        yield "hi"

    async def get_final_message(self):
        m = MagicMock()
        m.stop_reason = "end_turn"
        m.content = []
        m.usage = None
        m.model = "claude-sonnet-4-6"
        return m

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_stream_round_declares_every_tool_even_when_the_envelope_filters():
    captured: list = []
    client = MagicMock()
    client.messages.stream = lambda **kw: (captured.append(kw), _FakeStream())[1]
    filtered = tools_for_envelope(_env(lockdown=["G1"], allow_exercise=False))
    assert len(filtered) < len(TOOL_DEFINITIONS)

    async def go():
        return [x async for x in actor_client.stream_round("sys", [{"role": "user", "content": "q"}], filtered)]

    with patch.object(actor_client, "async_client", return_value=client):
        asyncio.run(go())
    assert captured[0]["tools"] is TOOL_DEFINITIONS
    assert captured[0]["tool_choice"] == {"type": "auto"}


# ---------------------------------------------------------------------------
# The turn: refusal on the single-call path, and a cache-stable revise call
# ---------------------------------------------------------------------------


def _turn(db, envelope, script, judge_verdict, generate_once=None):
    from tutor.agent.strategist import NO_MOVE

    captured: list = []
    calls = {"n": 0}

    async def rounds(system, messages, tools):
        captured.append({"system": system, "tools": tools, "messages": messages})
        chunks, stop, content = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for c in chunks:
            yield ("chunk", c)
        final = MagicMock()
        final.stop_reason = stop
        final.content = list(content)
        final.usage = None
        final.model = None
        yield ("final", final)

    bundle = EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message="walk me through it",
            lecture_num=None, course="AST101",
        )]

    with patch("tutor.agent.preclassify.classify",
               return_value=Classification(intent=Intent.ATTEMPT, has_attempt=True)), \
         patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE), \
         patch("tutor.agent.mastery.get_active_exam", return_value=None), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", rounds), \
         patch("tutor.agent.tools.dispatch_tool", return_value={"results": []}) as dispatched, \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
         patch("tutor.agent.judge.judge", return_value=judge_verdict):
        if generate_once is not None:
            with patch("tutor.agent.turn_stages.actor_client.client", return_value=generate_once):
                asyncio.run(go())
        else:
            asyncio.run(go())
    return captured, dispatched


def test_a_forbidden_tool_is_refused_on_the_single_call_path(db):
    script = [
        ([], "tool_use", [ToolUse("find_topic", "tu_1", query="exam answer")]),
        (["Show me what you tried."], "end_turn", []),
    ]
    captured, dispatched = _turn(db, _env(lockdown=["G1"], max_rung=1, escalate=True),
                                 script, Verdict("allow"))
    dispatched.assert_not_called()
    tool_result = captured[1]["messages"][-1]["content"][0]["content"]
    assert "not available this turn" in tool_result


def test_the_revise_call_keeps_the_cacheable_prefix(db):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text="A gentler hint.")]
    resp.usage = None
    resp.model = "claude-sonnet-4-6"
    client.messages.create.return_value = resp
    script = [(["Here is the full solution"], "end_turn", [])]
    captured, _ = _turn(db, _env(max_rung=3), script, Verdict("revise", rule="r", reason="too much"),
                        generate_once=client)
    revise = client.messages.create.call_args.kwargs
    assert revise["tools"] is TOOL_DEFINITIONS                  # same tool list
    assert revise["system"][0] == captured[0]["system"][0]      # same cached persona block
    assert revise["system"][0].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}
