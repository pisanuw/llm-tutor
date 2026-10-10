"""The pedagogy playbook loader (Session 11).

curriculum/pedagogy_moves.yaml is the instructor-editable source for what
each instructional move means, when it applies, and how it sounds in
the instructor's voice. This module parses it once, validates it against the policy
core's Move vocabulary, and renders the two prompt fragments that consume
it: the strategist's system prompt and the actor's cached move-vocabulary
block.

Fail-soft contract: a missing, unparseable, or invalid playbook logs a
warning and the renderers return None; callers fall back to their static
text. The playbook can refine guidance, never block a turn — the same
posture as the strategist itself.

policy.py stays authoritative on enforcement: move names must match
policy.Move exactly and any min_rung here must match policy.MOVE_MIN_RUNG
(both are load-time validation errors, and tests assert them).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from tutor.agent.policy import MOVE_MIN_RUNG, Move
from tutor.paths import CURRICULUM_DIR

log = logging.getLogger("tutor.playbook")

PLAYBOOK_PATH = CURRICULUM_DIR / "pedagogy_moves.yaml"


@dataclass(frozen=True)
class PlaybookMove:
    move: Move
    definition: str
    when: tuple[str, ...]
    avoid_when: tuple[str, ...]
    openers: tuple[str, ...]
    min_rung: int = 0


@dataclass(frozen=True)
class Playbook:
    version: int
    moves: tuple[PlaybookMove, ...]
    heuristics: tuple[str, ...]

    def get(self, move: Move) -> Optional[PlaybookMove]:
        for m in self.moves:
            if m.move is move:
                return m
        return None


def _parse(raw: dict) -> Playbook:
    """Strict parse: raises on anything malformed (caller catches)."""
    moves_raw = raw["moves"]
    names = set(moves_raw)
    expected = {m.value for m in Move}
    if names != expected:
        raise ValueError(
            f"playbook moves do not match policy.Move: "
            f"missing={sorted(expected - names)} unknown={sorted(names - expected)}"
        )

    moves = []
    for move in Move:  # policy enum order, deterministic
        entry = moves_raw[move.value]
        definition = " ".join(str(entry["definition"]).split())
        when = tuple(str(s).strip() for s in entry["when"])
        avoid = tuple(str(s).strip() for s in entry.get("avoid_when", ()))
        openers = tuple(str(s).strip() for s in entry["openers"])
        min_rung = int(entry.get("min_rung", 0))
        if not definition or not when or len(openers) < 2:
            raise ValueError(f"playbook {move.value}: needs definition, when, >=2 openers")
        if min_rung != MOVE_MIN_RUNG.get(move, 0):
            raise ValueError(
                f"playbook {move.value}: min_rung {min_rung} != policy "
                f"{MOVE_MIN_RUNG.get(move, 0)} (policy.py is authoritative)"
            )
        moves.append(
            PlaybookMove(
                move=move, definition=definition, when=when,
                avoid_when=avoid, openers=openers, min_rung=min_rung,
            )
        )

    heuristics = tuple(str(s).strip() for s in raw["strategist_heuristics"])
    if not heuristics:
        raise ValueError("playbook: strategist_heuristics is empty")
    return Playbook(version=int(raw["version"]), moves=tuple(moves), heuristics=heuristics)


@lru_cache(maxsize=1)
def load_playbook() -> Optional[Playbook]:
    """Parse and validate the playbook once; None (with a warning) on any failure."""
    try:
        import yaml

        with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
            return _parse(yaml.safe_load(fh))
    except Exception as exc:
        log.warning("pedagogy playbook unavailable, using static prompts: %s", exc)
        return None


def strategist_system() -> Optional[str]:
    """The strategist system prompt rendered from the playbook (None on failure).

    The vocabulary, per-move signals, and heuristics come from the YAML;
    the ledger-facts framing, rung-floor line, and JSON output contract are
    plumbing and stay fixed here.
    """
    pb = load_playbook()
    if pb is None:
        return None

    vocab = []
    for m in pb.moves:
        vocab.append(f"- {m.move.value}: {m.definition}")
        vocab.append(f"  use when: {'; '.join(m.when)}.")
        if m.avoid_when:
            vocab.append(f"  avoid when: {'; '.join(m.avoid_when)}.")

    floors = "; ".join(
        f"{m.move.value} needs H{m.min_rung}+" for m in pb.moves if m.min_rung > 0
    )
    heuristics = "\n".join(f"- {h}" for h in pb.heuristics)

    return (
        "You are the pedagogy strategist inside a supervised tutor. "
        "Each turn you pick ONE instructional move for the tutor's reply, "
        "from this vocabulary (and nothing else):\n\n"
        + "\n".join(vocab)
        + "\n\nHeuristics, in priority order:\n"
        + heuristics
        + "\n\nYou only see ledger facts, never the student's words. "
        f"Respect the help ceiling: {floors}.\n\n"
        "Return ONLY this JSON, nothing else:\n"
        '{"move": "<MOVE or null>", "reason": "<one short sentence>"}'
    )


def actor_move_vocabulary() -> Optional[str]:
    """The actor's move-vocabulary lines for the cached prefix (None on failure).

    One line of guidance plus one example opener per move, so the actor
    hears the move in the instructor's voice instead of inventing a register for it.
    """
    pb = load_playbook()
    if pb is None:
        return None
    lines = []
    for m in pb.moves:
        lines.append(f"  {m.move.value}: {m.definition}")
        lines.append(f'    e.g. "{m.openers[0]}"')
    return "\n".join(lines)
