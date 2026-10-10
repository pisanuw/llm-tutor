"""The pedagogy strategist (supervisor 2): one move per turn, never the wheel.

One Haiku call, temperature 0. Its inputs are ONLY trusted state: envelope
facts, the trajectory summary (tutor.agent.trajectory, itself prose-free),
and the classified intent label. It never sees the student's words — the
function signature cannot even accept them — so it cannot be talked into
anything (P2).

It proposes exactly one instructional move from the policy core's vocabulary
plus a one-sentence rationale. The policy core disposes (policy.merge_move):
a move can never raise the rung ceiling or override an exam lockdown.
Any failure — bad JSON, unknown move, API error, no key — defaults to
no move (the actor chooses), so the strategist can only ever add guidance,
never block a turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from tutor.agent.llm_response import strip_code_fence, text_from
from tutor.agent.models import SUPERVISOR as _SUPERVISOR
from tutor.agent.playbook import strategist_system as _playbook_system
from tutor.agent.policy import Envelope, Intent, Move, fading_stage
from tutor.agent.trajectory import Trajectory

log = logging.getLogger("tutor.strategist")

_VALID_MOVES = {m.value for m in Move}


@dataclass(frozen=True)
class MoveChoice:
    move: Optional[Move]
    reason: str = ""
    # Usage of the call, returned so the caller can ledger it on its own
    # thread (this module never touches the DB).
    in_tokens: int = 0
    out_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0


NO_MOVE = MoveChoice(move=None, reason="")


_SYSTEM_FALLBACK = """You are the pedagogy strategist inside a supervised tutor. \
Each turn you pick ONE instructional move for the tutor's reply, from this \
vocabulary (and nothing else):

- ELICIT_PREDICTION: ask the student to predict before explaining.
- PROBE_PRIOR: probe what they already know about the prerequisite idea.
- WORKED_ANALOGY: walk a worked example on a DIFFERENT problem.
- LOCATED_OBSERVATION: point at the exact slide/source and ask what they notice.
- SELF_EXPLAIN: have the student explain it back in their own words.
- CHECK_UNDERSTANDING: one quick check question before moving on.
- ENCOURAGE_ATTEMPT: push warmly for a first attempt now.
- SUGGEST_REVIEW: route back to the earlier topic this traces to.
- ADVANCE: acknowledge mastery, pull forward to the next idea.
- MODALITY_SWITCH: change representation (diagram, table, trace, plain words).
- COMPLETION_PROBLEM: a partially worked example with one key step left blank.
- FULL_PROBLEM: pose the full problem, unaided; no worked steps.

Faded worked examples: each goal below carries a fading stage from its \
evidence. Prefer WORKED_ANALOGY at stage=worked, COMPLETION_PROBLEM at \
stage=completion, FULL_PROBLEM (or ENCOURAGE_ATTEMPT) at stage=full. Never \
move DOWN the ladder unless the student is stuck.

Heuristics:
- repeated_error on a goal (stuck): WORKED_ANALOGY, MODALITY_SWITCH, or
  SUGGEST_REVIEW; do not keep explaining the same way.
- many passive turns, no attempts: ENCOURAGE_ATTEMPT or ELICIT_PREDICTION.
- strong recent scores, low help ceiling: ADVANCE or CHECK_UNDERSTANDING.
- brand-new goal (no history): PROBE_PRIOR or ELICIT_PREDICTION.
- exam fails piling up: SUGGEST_REVIEW (the gap is earlier).
- nothing stands out: return null and let the tutor choose.

You only see ledger facts, never the student's words. Respect the help
ceiling: WORKED_ANALOGY needs H5+, LOCATED_OBSERVATION needs H2+.

Return ONLY this JSON, nothing else:
{"move": "<MOVE or null>", "reason": "<one short sentence>"}"""


def _facts(
    envelope: Envelope,
    trajectory: Trajectory,
    intent: Intent,
    nav_facts: Optional[dict] = None,
) -> str:
    lines = [
        f"intent: {intent.value}",
        f"help ceiling: H{envelope.max_rung}",
        f"goals in scope: {', '.join(envelope.goals) or '(none)'}",
        f"exercises allowed: {envelope.allow_exercise}",
        f"session: {trajectory.session_turns} turns "
        f"({trajectory.attempts} attempts, {trajectory.questions} questions, "
        f"{trajectory.passive} passive)",
        "minutes since last attempt: "
        + (f"{trajectory.minutes_since_last_attempt:.0f}"
           if trajectory.minutes_since_last_attempt is not None else "never attempted"),
    ]
    for g in trajectory.goals[:6]:
        stage = fading_stage(g.graded_scores, g.rungs_used, g.exam_passes)
        lines.append(
            f"goal {g.goal_id}: {g.turns} turns, rungs {list(g.rungs_used)}, "
            f"moves {g.attempts}a/{g.questions}q/{g.passive}p, "
            f"stuck={g.repeated_error}, exams {g.exam_passes}pass/{g.exam_fails}fail, "
            f"scores {[round(s, 2) for s in g.graded_scores[-4:]]}, fading stage={stage}"
        )
    if nav_facts:
        lines.append("")
        lines.append("--- navigator context (deterministic, DB-sourced) ---")
        if nav_facts.get("top_goal_title"):
            lines.append(f"next recommended goal: {nav_facts['top_goal_title']!r}")
        if nav_facts.get("decayed_title") and nav_facts.get("decayed_score") is not None:
            lines.append(
                f"most-decayed mastered goal: {nav_facts['decayed_title']!r} "
                f"(effective mastery {nav_facts['decayed_score']:.0%})"
            )
        pacing_ok = nav_facts.get("pacing_ok", True)
        if not pacing_ok:
            lines.append(
                "PACING CAP: a pacing move (ADVANCE/SUGGEST_REVIEW/MODALITY_SWITCH) "
                "was already used recently — do NOT emit one this turn; choose "
                "a content move instead."
            )
    return "\n".join(lines)


def choose_move(
    envelope: Envelope,
    trajectory: Trajectory,
    intent: Intent,
    client=None,
    nav_facts: Optional[dict] = None,
) -> MoveChoice:
    """One Haiku call proposing a move. Defaults to NO_MOVE on any failure.

    nav_facts (optional): deterministic navigator output from pacing.navigator_facts().
    When pacing_ok=False, the pacing-cap instruction is injected so the model
    never nags within the same window.
    """
    if envelope.exam_lockdown:
        return NO_MOVE   # lockdown turns take no move at all

    if client is None:
        from tutor.agent.clients import supervisor_client
        from tutor.config import get_config

        api_key = get_config().get("anthropic_api_key", "")
        if not api_key:
            return NO_MOVE
        client = supervisor_client(api_key=api_key)

    import time

    t0 = time.monotonic()
    try:
        resp = client.messages.create(
            model=_SUPERVISOR.id,
            max_tokens=120,
            temperature=0,
            system=_playbook_system() or _SYSTEM_FALLBACK,
            messages=[{"role": "user", "content": _facts(envelope, trajectory, intent, nav_facts)}],
        )
        usage = getattr(resp, "usage", None)
        usage_kw = dict(
            in_tokens=getattr(usage, "input_tokens", 0) or 0,
            out_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        raw = strip_code_fence(text_from(resp))
        data = json.loads(raw)
        move_raw = data.get("move")
        if move_raw is None or str(move_raw).upper() in ("NULL", "NONE", ""):
            return MoveChoice(move=None, **usage_kw)
        move_name = str(move_raw).upper()
        if move_name not in _VALID_MOVES:
            log.warning("strategist proposed unknown move: %s", move_name)
            return MoveChoice(move=None, **usage_kw)
        return MoveChoice(
            move=Move(move_name), reason=str(data.get("reason", ""))[:200], **usage_kw
        )
    except Exception as exc:
        log.warning("strategist call failed (no move): %s", exc)
        return NO_MOVE
