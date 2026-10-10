"""Pedagogy simulation (Session 25).

Drives scripted student trajectories and measures the strategist's pedagogy:
move distribution, elicitation rate on passive students, and depth-to-convergence
(turns until a goal is mastered). The strategist's LLM (Haiku) is stood in here
by ``reference_move`` -- a deterministic policy that encodes the same playbook
signals (curriculum/pedagogy_moves.yaml) -- so the metrics are reproducible in
CI. Measure the live strategist with an operator run; the metric machinery is
the same.

Personas
--------
- passive: asks questions, never attempts. The system should *elicit* rather
  than keep telling.
- stuck: attempts repeatedly on one goal with no progress (repeated_error). The
  system should offer a modality switch, not more of the same.
- cruising: attempts and succeeds; mastery climbs and the system advances.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from tutor.agent.policy import Intent, Move
from tutor.agent.trajectory import GoalTrajectory

# Moves that put the student to work rather than telling them the answer.
ELICITATION_MOVES = frozenset({
    Move.ELICIT_PREDICTION, Move.ENCOURAGE_ATTEMPT, Move.PROBE_PRIOR,
    Move.SELF_EXPLAIN, Move.CHECK_UNDERSTANDING,
})


def reference_move(g: GoalTrajectory, intent: Intent,
                   nav_facts: Optional[dict] = None) -> Optional[Move]:
    """Deterministic stand-in for the Haiku strategist, encoding the playbook
    signals. Returns a Move or None (let the actor choose)."""
    nav_facts = nav_facts or {}
    if nav_facts.get("review_due"):
        return Move.SUGGEST_REVIEW
    if g.repeated_error:
        return Move.MODALITY_SWITCH
    # Passive: questions/presence but no attempts -> put them to work.
    if g.attempts == 0 and (g.questions + g.passive) >= 2:
        return Move.ELICIT_PREDICTION if intent == Intent.QUESTION else Move.ENCOURAGE_ATTEMPT
    # Cruising: recent successes -> advance.
    if g.graded_scores and sum(g.graded_scores) / len(g.graded_scores) >= 0.8:
        return Move.ADVANCE
    if g.attempts >= 1:
        return Move.CHECK_UNDERSTANDING
    return None


# ---------------------------------------------------------------------------
# Scripted personas: each turn is a GoalTrajectory snapshot + the intent.
# ---------------------------------------------------------------------------

def _goal(attempts, questions, passive, scores=(), repeated=False) -> GoalTrajectory:
    return GoalTrajectory(
        goal_id="AST101-L04-inverse-square-law", turns=attempts + questions + passive,
        rungs_used=(), attempts=attempts, questions=questions, passive=passive,
        repeated_error=repeated, exam_passes=0, exam_fails=0, graded_scores=tuple(scores),
    )


def passive_persona() -> list[tuple[GoalTrajectory, Intent]]:
    return [
        (_goal(0, 1, 0), Intent.QUESTION),
        (_goal(0, 2, 1), Intent.QUESTION),
        (_goal(0, 3, 1), Intent.QUESTION),
        (_goal(0, 4, 2), Intent.QUESTION),
    ]


def stuck_persona() -> list[tuple[GoalTrajectory, Intent]]:
    return [
        (_goal(1, 0, 0, scores=(0.25,)), Intent.ATTEMPT),
        (_goal(2, 0, 0, scores=(0.25, 0.25)), Intent.ATTEMPT),
        (_goal(3, 1, 0, scores=(0.25, 0.25, 0.25), repeated=True), Intent.ATTEMPT),
        (_goal(4, 1, 0, scores=(0.25, 0.25, 0.25, 0.25), repeated=True), Intent.ATTEMPT),
    ]


def cruising_persona() -> list[tuple[GoalTrajectory, Intent]]:
    return [
        (_goal(1, 0, 0, scores=(0.5,)), Intent.ATTEMPT),
        (_goal(2, 0, 0, scores=(0.5, 0.75)), Intent.ATTEMPT),
        (_goal(3, 0, 0, scores=(0.75, 1.0, 1.0)), Intent.ATTEMPT),    # converges here
        (_goal(4, 0, 0, scores=(1.0, 1.0, 1.0, 1.0)), Intent.ATTEMPT),
    ]


PERSONAS = {
    "passive": passive_persona,
    "stuck": stuck_persona,
    "cruising": cruising_persona,
}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

CONVERGENCE_THRESHOLD = 0.8


@dataclass
class PedagogyReport:
    move_distribution: Counter = field(default_factory=Counter)
    passive_elicitation_rate: float = 0.0
    stuck_modality_rate: float = 0.0
    depth_to_convergence: Optional[int] = None
    per_persona_moves: dict = field(default_factory=dict)


def _moves_for(persona) -> list[Optional[Move]]:
    return [reference_move(g, intent) for g, intent in persona()]


def _depth_to_convergence(persona) -> Optional[int]:
    for i, (g, _intent) in enumerate(persona(), start=1):
        if g.graded_scores and sum(g.graded_scores) / len(g.graded_scores) >= CONVERGENCE_THRESHOLD:
            return i
    return None


def run_pedagogy_sim() -> PedagogyReport:
    report = PedagogyReport()
    for name, persona in PERSONAS.items():
        moves = _moves_for(persona)
        report.per_persona_moves[name] = [m.value if m else None for m in moves]
        for m in moves:
            report.move_distribution[m.value if m else "none"] += 1

    passive_moves = _moves_for(passive_persona)
    report.passive_elicitation_rate = (
        sum(1 for m in passive_moves if m in ELICITATION_MOVES) / len(passive_moves)
    )
    stuck_moves = _moves_for(stuck_persona)
    report.stuck_modality_rate = (
        sum(1 for m in stuck_moves if m == Move.MODALITY_SWITCH) / len(stuck_moves)
    )
    report.depth_to_convergence = _depth_to_convergence(cruising_persona)
    return report


def render_report(report: PedagogyReport, generated_at: str = "") -> str:
    import datetime
    generated_at = generated_at or datetime.date.today().isoformat()
    lines = [
        f"# Pedagogy sim report — {generated_at}",
        "",
        "Generated by `python -m tests.sim.pedagogy`. Scripted personas through a "
        "deterministic stand-in for the Haiku strategist (the same playbook "
        "signals). Live strategist: operator run.",
        "",
        "## Metrics",
        f"- passive-student elicitation rate: **{report.passive_elicitation_rate:.0%}** "
        "(target: high; the system elicits rather than tells)",
        f"- stuck-student modality-switch rate: **{report.stuck_modality_rate:.0%}**",
        f"- depth-to-convergence (cruising): **{report.depth_to_convergence} turns**",
        "",
        "## Move distribution",
        "",
        "| move | count |",
        "|------|-------|",
    ]
    for move, n in report.move_distribution.most_common():
        lines.append(f"| {move} | {n} |")
    lines += ["", "## Per-persona move sequence", ""]
    for name, moves in report.per_persona_moves.items():
        lines.append(f"- **{name}**: {', '.join(m or '(actor choice)' for m in moves)}")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    report = run_pedagogy_sim()
    md = render_report(report)
    print(md)
    if args.write:
        out = Path(__file__).resolve().parents[2] / "docs" / "sim" / "pedagogy-report.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md + "\n")
        print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
