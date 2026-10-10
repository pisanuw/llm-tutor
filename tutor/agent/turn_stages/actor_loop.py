"""The actor phase (S15): ONE streaming call that declares the tools and
handles tool_use mid-stream, in a bounded loop.

Routine turns stream live through the detector gate; risky turns buffer for
the judge (supervision.py). A retrieved slide can widen an empty scope for
the continuation rounds, but it never fires a gate, and supervision only
tightens mid-turn.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from sqlalchemy.orm import Session as DBSession

from tutor.agent import detector
from tutor.agent import envelope as envelope_mod
from tutor.agent import exercises
from tutor.agent import judge as judge_mod
from tutor.agent import policy
from tutor.agent import prompt_builder
from tutor.agent import tools as tools_mod
from tutor.agent.turn_stages import actor_client
from tutor.agent.turn_stages.dbstep import db_step
from tutor.agent.turn_stages.errors import error_kind, friendly_error
from tutor.agent.turn_stages.request import TurnRequest

if TYPE_CHECKING:
    from tutor.agent.envelope import EnvelopeBundle
    from tutor.agent.preclassify import Classification

MAX_TOOL_ROUNDS = 3   # bounded loop so find_topic can chain into get_exercise

# At or below H4 the rung permits NO code (H4 = "describe the approach in
# words"), so the deterministic detector runs in strict mode: it cuts short
# function signatures and blanked code templates that the lenient default lets
# through (the genuine G3 leaks under adversarial pressure). H5+ legitimately
# allows code (worked example / pseudocode / full solution), so the detector
# stays lenient there.
NO_CODE_MAX_RUNG = 4


@dataclass
class ActorRun:
    """The actor phase's state: set up by :func:`prepare`, advanced by
    :func:`run_rounds`, and read by the supervision and persistence stages."""

    bundle: "EnvelopeBundle"
    classification: "Classification"
    code_adjacent: bool
    per_request_ctx: str
    messages: list[dict]              # history + this turn's message
    env_block: str = ""
    turn_tools: list[dict] = field(default_factory=list)
    risky: bool = False               # buffered for the judge
    strict_detect: bool = False       # no-code rung: strict detector
    supervision_tightened: bool = False   # refinement switched the judge/strict detector on
    system: list[dict] = field(default_factory=list)
    convo: list[dict] = field(default_factory=list)
    gate: detector.StreamGate = field(default_factory=detector.StreamGate)
    draft_parts: list[str] = field(default_factory=list)   # raw model text (risky: judged first)
    out_parts: list[str] = field(default_factory=list)     # text already emitted (routine)
    executed: list[tuple[str, str]] = field(default_factory=list)   # (tool, actor-safe payload)
    tool_notes: list[dict] = field(default_factory=list)   # compact memory of what was shown (S16)
    slide_ref: Optional[dict] = None
    found_lecture_num: Optional[int] = None
    eval_result: Optional[dict] = None
    tool_rounds: int = 0
    stop_reasons: list[str] = field(default_factory=list)  # every round's stop_reason (plan 2.4)
    side_calls: list[dict] = field(default_factory=list)   # the rubric evaluator's own calls
    ledger: actor_client.UsageLedger = field(default_factory=actor_client.UsageLedger)
    stream_error: bool = False
    stream_error_kind: str = "other"
    stream_error_message: str = ""    # sent after the gate's held-back tail


def prepare(
    bundle: "EnvelopeBundle",
    classification: "Classification",
    code_adjacent: bool,
    per_request_ctx: str,
    messages: list[dict],
) -> ActorRun:
    """Settle the turn's supervision before the first token: the tools the
    envelope allows, judge or stream, and the detector's strictness."""
    run = ActorRun(bundle=bundle, classification=classification, code_adjacent=code_adjacent,
                   per_request_ctx=per_request_ctx, messages=messages)
    _render_envelope(run)
    run.risky = judge_mod.should_judge(bundle.envelope, classification.intent, code_adjacent)
    # No-code rung -> strict detector (cuts signatures + blanked code templates).
    run.strict_detect = bundle.envelope.max_rung <= NO_CODE_MAX_RUNG
    run.system = _system(run)
    run.convo = list(messages)
    run.gate = detector.StreamGate(strict=run.strict_detect)
    return run


def _render_envelope(run: ActorRun) -> None:
    envelope = run.bundle.envelope
    run.env_block = prompt_builder.render_envelope(
        envelope, run.bundle.goal_titles, run.bundle.calibration_note)
    run.turn_tools = tools_mod.tools_for_envelope(envelope)


def _system(run: ActorRun) -> list[dict]:
    # Envelope block goes LAST in the system prompt (order contract: cached
    # persona prefix, then per-request context, then the envelope), in the
    # uncached trailing block.
    return actor_client.system_blocks(run.per_request_ctx + "\n\n" + run.env_block)


async def run_rounds(db: DBSession, req: TurnRequest, run: ActorRun) -> AsyncGenerator[dict, None]:
    """Stream the actor's rounds. Yields routine tokens, and the slide and
    eval events as their tools return."""
    loop = asyncio.get_running_loop()
    gate = run.gate
    for _ in range(MAX_TOOL_ROUNDS + 1):
        final = None
        round_start = time.monotonic()
        try:
            async for kind, payload in actor_client.stream_round(run.system, run.convo, run.turn_tools):
                if kind == "chunk":
                    req.mark("first_model_text")
                    if run.risky:
                        run.draft_parts.append(payload)
                    else:
                        emitted = gate.feed(payload)
                        if emitted:
                            run.out_parts.append(emitted)
                            req.mark("first_token")
                            yield {"type": "token", "text": emitted}
                elif kind == "final":
                    final = payload
        except Exception as exc:
            run.stream_error = True
            run.stream_error_kind = error_kind(exc)
            run.stream_error_message = friendly_error(exc)
            break
        finally:
            req.add_ms("actor", round_start)
        if final is not None:
            u = getattr(final, "usage", None)
            actor_client.log_cache_usage(u, "actor")
            run.ledger.add(u, getattr(final, "model", None))
            if isinstance(getattr(final, "stop_reason", None), str):
                run.stop_reasons.append(final.stop_reason)
        if final is None or final.stop_reason != "tool_use" or run.tool_rounds >= MAX_TOOL_ROUNDS:
            break
        run.tool_rounds += 1

        # Execute the requested tools (sync, sub-200ms) and continue the
        # stream with proper tool_result blocks (tools are declared, so
        # the continuation call is the SDK-native loop; only the tools-free
        # revise call needs the flattened context).
        tool_results: list[dict] = []
        for block in final.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            async for event in _run_tool(loop, req, run, block, tool_results):
                yield event
        run.convo = run.convo + [
            {"role": "assistant", "content": final.content},
            {"role": "user", "content": tool_results},
        ]
        # Refine the envelope for continuation rounds if retrieval
        # anchored an empty scope (cached persona prefix untouched: the
        # envelope lives in the uncached trailing block).
        await db_step(db, refine_from_slide, db, req, run)
        _tighten(run)
        run.system = _system(run)


async def _run_tool(loop, req: TurnRequest, run: ActorRun, block, tool_results: list[dict]):
    """Run one requested tool; append its tool_result block. Yields the slide
    event for the first retrieved slide and the eval badge for a real score."""
    tool_input = with_turn_course(attach_submitted_code(block.name, block.input, req.user_message),
                                  req.course)
    if block.name in {t["name"] for t in run.turn_tools}:
        tool_start = time.monotonic()
        result = await loop.run_in_executor(None, tools_mod.dispatch_tool, block.name, tool_input)
        req.add_ms(f"tool_{block.name}", tool_start)
    else:
        result = tools_mod.refusal(block.name)   # declared, but off this turn
    if isinstance(result, dict) and result.get("_usage"):
        run.side_calls.append(result["_usage"])
    if run.slide_ref is None and isinstance(result, dict) and result.get("slide_ref"):
        run.slide_ref = result["slide_ref"]
        run.found_lecture_num = run.slide_ref.get("lecture_num")
        if run.slide_ref.get("slide_url"):
            yield {
                "type": "slide",
                "url": run.slide_ref["slide_url"],
                "title": run.slide_ref.get("lecture_title", ""),
                "num": run.slide_ref.get("slide_number", ""),
                "lecture_num": run.found_lecture_num,
            }
    run.tool_notes.append(_tool_note(block.name, tool_input, result))
    actor_result = result
    if block.name == "evaluate_code" and isinstance(result, dict) and "error" not in result:
        # Server-side keeps eval_details; the actor (and the continuation
        # stream) only ever sees student-facing keys (P1).
        actor_result = exercises.student_facing_eval(result)
        if exercises.eval_counts(result):
            # Only a real score is evidence and gets a badge; a failed
            # evaluation is neither (plan step 1.5).
            run.eval_result = {**result, "_input": tool_input}
            yield {
                "type": "eval",
                "score": run.eval_result.get("score", 0),
                "correct": run.eval_result.get("correct", False),
            }
    payload_json = json.dumps(actor_result, default=str)
    run.executed.append((block.name, payload_json))
    tool_results.append({
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": payload_json,
    })


def _tool_note(name: str, tool_input, result) -> dict:
    """What history remembers of a tool call (S16): the query, the top
    source, and the slide shown."""
    note = {"tool": name}
    q = (tool_input or {}).get("query") or (tool_input or {}).get("topic")
    if q:
        note["query"] = str(q)[:120]
    if isinstance(result, dict):
        hits = result.get("results") or []
        if hits and isinstance(hits[0], dict) and hits[0].get("source"):
            note["top_source"] = str(hits[0]["source"])[:120]
        sr = result.get("slide_ref")
        if sr and sr.get("slide_url"):
            note["slide"] = sr["slide_url"]
    return note


def _tighten(run: ActorRun) -> None:
    """Plan 1.1 (A2): the continuation runs under the supervision the final
    envelope calls for. Settings only tighten mid-turn."""
    envelope = run.bundle.envelope
    if not run.strict_detect and envelope.max_rung <= NO_CODE_MAX_RUNG:
        run.strict_detect = True
        run.gate.strict = True
        run.supervision_tightened = True
    if not run.risky and judge_mod.should_judge(envelope, run.classification.intent, run.code_adjacent):
        run.risky = True
        run.supervision_tightened = True
        # What the gate still holds never reached the student: it joins the
        # draft the judge reads. Text already streamed stays (it was produced
        # before the tool result).
        held = run.gate.take_pending()
        if held:
            run.draft_parts.append(held)


def refine_from_slide(db: DBSession, req: TurnRequest, run: ActorRun) -> None:
    """S14 refinement: an empty scope can grow from a retrieved slide's
    lecture (goal-graph numbering via source-lecture-map.json); the gate is
    never revisited here. Rebuilds the envelope block and the turn's tools."""
    slide_lec = (run.slide_ref or {}).get("lecture_num")
    if not slide_lec or run.bundle.envelope.goals:
        return
    refined = envelope_mod.assemble_envelope(
        req.sid, req.course, db,
        lecture_num=slide_lec,
        intent=run.classification.intent,
        code_adjacent=run.code_adjacent,
        gate_eligible=False,   # gates never fire off slide metadata
    )
    if not refined.envelope.goals:
        return
    prior = run.bundle.envelope
    new_env = refined.envelope
    # Plan 1.1 (A2): the turn is already running under prior's ceiling and
    # text may have streamed through a gate set for it, so a mastery-band
    # ceiling never drops mid-turn (the telemetry then records the rung that
    # was enforced). An exam lockdown on the retrieved goals is a hard cap
    # and still applies; the caller re-derives the judge and detector
    # settings so it is enforced.
    if new_env.max_rung < prior.max_rung and not new_env.exam_lockdown:
        new_env = replace(
            new_env,
            max_rung=prior.max_rung,
            reason=f"{new_env.reason}; kept H{prior.max_rung} from the start of the turn",
        )
    # Carry the strategist's move across the refinement (merge_move re-applies
    # the policy constraints against the new ceiling).
    if prior.move is not None:
        new_env = policy.merge_move(new_env, prior.move, prior.move_reason)
    refined.envelope = new_env
    run.bundle = refined
    _render_envelope(run)


def longest_fenced_code(text: str) -> str:
    """The body of the longest fenced block in ``text`` ("" if none)."""
    bodies = [b.body for b in detector.extract_code_blocks(text or "") if b.lang != "indented"]
    return max(bodies, key=lambda b: len(b.strip()), default="")


def with_turn_course(tool_input, course: str) -> dict:
    """A tool call that names no real course runs in the turn's course (plan
    step 5.7, finding A18). dispatch_tool's own fallback is the first course,
    which was wrong for a student in another course whenever the model left the
    course out."""
    out = dict(tool_input or {})
    out["course"] = tools_mod.valid_course(out.get("course"), course)
    return out


def attach_submitted_code(tool_name: str, tool_input, user_message: Optional[str]):
    """For evaluate_code, use the code the student pasted (a fenced block in
    their message) when the call carries none or less of it. The model no
    longer has to echo a long file into the tool call, where the output cap
    used to cut it off (plan step 2.4, finding A5)."""
    if tool_name != "evaluate_code" or not user_message:
        return tool_input
    pasted = longest_fenced_code(user_message)
    if not pasted.strip():
        return tool_input
    current = str((tool_input or {}).get("student_code") or "")
    if len(current.strip()) >= len(pasted.strip()):
        return tool_input
    return {**(tool_input or {}), "student_code": pasted}
