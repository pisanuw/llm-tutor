"""Small shared helpers for parsing Anthropic Messages API responses.

The supervisor models (judge, pre-classifier, strategist) and the graders all
ask the model for a JSON object and read it back, and two failure modes recur:

  - ``resp.content`` can be empty or lead with a non-text block (a stop on
    max_tokens, a tool/other block), so ``resp.content[0].text`` raises and the
    caller's broad ``except`` degrades the verdict to its OPEN default
    (judge -> allow, classifier -> OTHER). :func:`text_from` concatenates every
    text block instead, returning ``""`` only when there is genuinely no text,
    and recovering text that does not happen to sit in block 0.
  - the model often wraps its JSON in a ```json fence; :func:`strip_code_fence`
    removes a leading/trailing fence so ``json.loads`` sees the object. The same
    regex pair was previously copy-pasted in judge/preclassify/strategist.
"""

from __future__ import annotations

import re

_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```$")


def text_from(resp) -> str:
    """Concatenate the text of every text block in a Messages response.

    Tolerates an empty content list or a leading non-text block (either of which
    would make ``resp.content[0].text`` raise), returning ``""`` only when no
    text is present at all.
    """
    content = getattr(resp, "content", None) or []
    parts = [
        getattr(block, "text", None)
        for block in content
        if isinstance(getattr(block, "text", None), str)
    ]
    return "".join(parts)


def strip_code_fence(raw: str) -> str:
    """Strip a leading ```/```json and trailing ``` fence from a model reply."""
    raw = raw.strip()
    raw = _FENCE_OPEN_RE.sub("", raw)
    raw = _FENCE_CLOSE_RE.sub("", raw)
    return raw


def usage_counts(usage) -> dict:
    """Token counts of one call as plain ints (0 for anything missing or
    not an int, e.g. a test double)."""
    def _n(name: str) -> int:
        v = getattr(usage, name, 0)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    # The part of the cache write that went to the 1-hour cache (priced at
    # 2x input, not 1.25x): the actor's persona prefix since the latency plan.
    breakdown = getattr(usage, "cache_creation", None)
    write_1h = getattr(breakdown, "ephemeral_1h_input_tokens", 0) if breakdown is not None else 0
    return {
        "in_tokens": _n("input_tokens"),
        "out_tokens": _n("output_tokens"),
        "cache_read_tokens": _n("cache_read_input_tokens"),
        "cache_write_tokens": _n("cache_creation_input_tokens"),
        "cache_write_1h_tokens": write_1h if isinstance(write_1h, int)
        and not isinstance(write_1h, bool) else 0,
    }


def record_usage(db, student_id, usage, model, kind, latency_ms=None) -> None:
    """Record one supervisor LLM call's token usage as a TurnEvent.

    Shared by the judge ('judge') and pre-classifier ('classify'). Tolerates a
    missing ``usage`` (its fields default to 0).
    """
    from tutor.agent import state, usage as usage_mod

    state.record_turn_event(
        db,
        student_id=student_id,
        kind=kind,
        latency_ms=latency_ms,
        **usage_mod.cost_fields(model, usage_counts(usage)),
    )


def record_usage_calls(db, student_id, calls: list) -> None:
    """Ledger the calls a classifier or judge reported through its
    ``usage_sink`` (plan step 2.6, finding A19), then empty the list."""
    for call in calls:
        record_usage(db, student_id, call.get("usage"), model=call["model"],
                     kind=call["kind"], latency_ms=call.get("latency_ms"))
    calls.clear()
