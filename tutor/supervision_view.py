"""What the admin Supervision table shows in each cell, and which rows it shows.

``turn_events`` holds two unlike kinds of row. A *turn* (``chat``) carries the
whole supervision record: the classified intent, the ceiling that was
enforced, the move, the verdict, whether the detector cut anything, whether
the answer was grounded. A *call* (``classify``, ``strategist``, ``judge``,
``cache_warm``, ``evaluate``, ``exam_classify``) is written so one supervisor
model call's tokens and cost land in the ledger of their own; it has no
intent, no ceiling and no verdict, and never had one.

In production that is 97 turns among 376 rows, so the table showed six empty
columns on three rows out of four and read as missing data. Two fixes live
here: the tab filters by kind and defaults to the turns, and a cell that
cannot apply says so ("n/a") instead of "—".

The wording is deliberate:

    verdict     allow / revise / block, the three the judge can return.
                A detector cut reads "revise": the reply the student saw is
                not the one the actor wrote. "running" is a turn still in
                flight, "error" one that never produced a reply.
    detector    "clean" means it ran and removed nothing (the common case),
                "cut" that it removed something, "not run" that the turn
                never reached it.
    rung        the ceiling. "H2→H4" when a floor or a cap moved the
                mastery-band ceiling, a single "H5" when nothing moved it.
                It is NOT the rung the reply actually used: nothing in the
                system measures that (the actor is told a ceiling and never
                asked what it used), so the table does not claim it.

Pure: the route flattens a row, this module decides the cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

# Kinds that carry a supervision record. A new supervised kind goes here.
TURN_KINDS = frozenset({"chat"})
# Kinds whose verdicts hold a grade rather than a judge ruling.
GRADED_KINDS = frozenset({"exam", "exercise", "review"})

ALL = "all"
DEFAULT_KIND = "chat"

# The three verdicts the judge can return, plus the states that are not a
# verdict at all. A detector cut is a revise: the student did not get the
# draft the actor wrote.
VERDICT_TEXT: dict[str, str] = {
    "allow": "allow",
    "revise": "revise",
    "block": "block",
    "detector_cut": "revise",
    "in_progress": "running",
    "stream_error": "error",
    "empty_reply": "error",
    "crash": "error",
}
# Verdicts that mean the turn never produced a supervised reply, so the
# detector never ruled on one.
UNREACHED = frozenset({"in_progress", "stream_error", "empty_reply", "crash"})
NA = "n/a"


@dataclass(frozen=True)
class Cell:
    """One table cell: what it reads, what hovering it explains, and how it
    is coloured ("" plain, "bad" red, "warn" amber, "muted" grey)."""
    text: str
    title: str = ""
    tone: str = ""


def is_turn(kind: Optional[str]) -> bool:
    return (kind or "") in TURN_KINDS


# ---------------------------------------------------------------------------
# The cells
# ---------------------------------------------------------------------------

def verdict_cell(kind: Optional[str], verdicts: Optional[dict]) -> Cell:
    """allow / revise / block for a turn; the score for a graded row; "n/a"
    for a supervisor call, which is not a turn and has no verdict."""
    verdicts = verdicts or {}
    raw = verdicts.get("verdict")
    if not is_turn(kind):
        if (kind or "") in GRADED_KINDS:
            score = verdicts.get("score")
            if score is not None:
                return Cell(f"score {score}", "a graded answer, not a judge verdict", "muted")
        return Cell(NA, f"a {kind or 'ledger'} row: one supervisor model call, not a turn", "muted")
    if raw is None:
        score = verdicts.get("score")
        if score is not None:
            return Cell(f"score {score}", "", "muted")
        return Cell(NA, "this turn recorded no verdict", "muted")
    text = VERDICT_TEXT.get(str(raw), str(raw))
    title = ""
    tone = ""
    if str(raw) == "detector_cut":
        title = "the detector cut something: the student did not get the draft the actor wrote"
        tone = "warn"
    elif text == "revise":
        title = str(verdicts.get("judge_reason") or "the judge asked for a revision")
        tone = "warn"
    elif text == "block":
        title = str(verdicts.get("judge_reason") or "the judge blocked the draft")
        tone = "bad"
    elif text == "running":
        title = "the turn was still in flight when this row was written"
        tone = "muted"
    elif text == "error":
        title = f"the turn failed ({raw})"
        tone = "bad"
    elif not verdicts.get("judged"):
        title = "the detector gate allowed it; the LLM judge does not run on a routine turn"
    else:
        title = "the judge allowed it"
    return Cell(text, title, tone)


def detector_cell(kind: Optional[str], verdicts: Optional[dict], det: Optional[dict]) -> Cell:
    """"clean" when it ran and removed nothing, "cut" when it removed
    something, "not run" when the turn never reached it, "n/a" off a turn."""
    verdicts, det = verdicts or {}, det or {}
    if not is_turn(kind):
        return Cell(NA, f"a {kind or 'ledger'} row: no reply to scan", "muted")
    cut = bool(verdicts.get("detector_cut")) or det.get("cut") is True
    if cut:
        return Cell("cut", "the detector removed something from the draft", "bad")
    if str(verdicts.get("verdict") or "") in UNREACHED:
        return Cell("not run", "the turn produced no reply for the detector to scan", "muted")
    return Cell("clean", "the detector ran on this reply and removed nothing")


def rung_cell(kind: Optional[str], rung_used: Optional[int], envelope: Optional[dict]) -> Cell:
    """The ceiling: "H2→H4" when a floor or a cap moved the mastery band's
    ceiling, a single "H5" when nothing did.

    ``rung_used`` on the row is the ceiling that was *enforced* (what the
    actor was allowed), which is why this is one number and not a pair of
    used-over-ceiling: nothing measures the rung a reply actually reached.
    """
    envelope = envelope or {}
    if not is_turn(kind):
        return Cell(NA, f"a {kind or 'ledger'} row: no envelope", "muted")
    enforced = rung_used if rung_used is not None else envelope.get("max_rung")
    base = envelope.get("rung_base")
    reason = str(envelope.get("reason") or "")
    if enforced is None:
        return Cell("—", reason or "this turn recorded no ceiling", "muted")
    if base is not None and int(base) != int(enforced):
        return Cell(f"H{base}→H{enforced}", reason or "the ceiling moved from the mastery band", "warn")
    return Cell(f"H{enforced}", reason)


# ---------------------------------------------------------------------------
# The kind filter
# ---------------------------------------------------------------------------

def normalize_kind(raw: Optional[str], available: Iterable[str]) -> str:
    """The kind to show: what was asked for when the table holds it, ``all``
    when asked for, else the default (the turns). An unknown or absent value
    is the default, so a hand-edited address opens a page."""
    known = set(available)
    value = (raw or "").strip()
    if value == ALL:
        return ALL
    if value and value in known:
        return value
    return DEFAULT_KIND


@dataclass(frozen=True)
class KindChoice:
    kind: str
    label: str
    count: int
    active: bool


def kind_choices(counts: Sequence[tuple[str, int]], selected: str) -> list[KindChoice]:
    """One entry per kind in the table, turns first, then ``all``; each with
    how many rows it holds, so the filter says what it is hiding."""
    rows = sorted(counts, key=lambda kv: (not is_turn(kv[0]), -kv[1], kv[0]))
    out = [KindChoice(k, k, n, k == selected) for k, n in rows]
    total = sum(n for _k, n in counts)
    out.append(KindChoice(ALL, "all kinds", total, selected == ALL))
    return out


def shown_counts(rows: Iterable[Any]) -> tuple[int, int]:
    """(turns, calls) among the rows on the page, for the caption."""
    turns = sum(1 for r in rows if is_turn(getattr(r, "kind", None)))
    return turns, sum(1 for _ in rows) - turns
