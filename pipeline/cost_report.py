"""Cost report from the telemetry ledger (Session 23 [DECIDED: instrument first]).

The budget decision was deferred until cost could be measured rather than
guessed. This reads the TurnEvent ledger (the single source of cost truth) and
reports cost per turn by kind, cost per student-day, cache-read share, and the
supervisor-overhead share (the Haiku classify/judge/strategist calls that wrap
each Sonnet actor turn), then recommends a per-student-quarter budget and the two
cap settings.

When the ledger is thin (no live volume yet), the recommendation falls back on
a baseline per-turn cost (BASELINE_PER_TURN_USD, measured on early live
turns), or pass --per-turn-usd to give your own.

    python -m pipeline cost-report                       # from the ledger
    python -m pipeline cost-report --days 30
    python -m pipeline cost-report --per-turn-usd 0.03   # baseline-based fallback
    python -m pipeline cost-report --dry-run             # print, do not write
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from tutor.utctime import utcnow

ROOT = Path(__file__).parent.parent
REPORT_PATH = ROOT / "docs" / "cost-report.md"
console = Console()

# Which kinds are the Haiku supervisors vs the Sonnet actor. Other kinds
# (exam, exercise, conduct, restriction) are state transitions; their cost is
# attributed under "other".
# Every supervisor-tier ledger kind; since plan step 2.1 the exam and review
# graders, the exam labeller and clarifier, and the rubric evaluator are
# ledgered too ("evaluate" runs on the actor model but is tool-side work).
SUPERVISOR_KINDS = frozenset({
    "classify", "judge", "strategist", "exam_grade", "exam_classify",
    "exam_clarify", "review", "exam", "evaluate",
})
ACTOR_KINDS = frozenset({"chat"})


@dataclass
class CostStats:
    window_days: int
    n_turns: int = 0
    total_cost: float = 0.0
    by_kind: dict = field(default_factory=dict)        # kind -> {"n", "cost"}
    actor_cost: float = 0.0
    supervisor_cost: float = 0.0
    other_cost: float = 0.0
    in_tokens: int = 0
    cache_tokens: int = 0
    cache_write_tokens: int = 0
    student_days: int = 0

    @property
    def per_turn_avg(self) -> float:
        return self.total_cost / self.n_turns if self.n_turns else 0.0

    @property
    def cache_share(self) -> float:
        """Cache reads over all input (uncached, cache reads, cache writes).
        Cache writes count as input: a cold cache must lower the share."""
        denom = self.in_tokens + self.cache_tokens + self.cache_write_tokens
        return self.cache_tokens / denom if denom else 0.0

    @property
    def supervisor_share(self) -> float:
        return self.supervisor_cost / self.total_cost if self.total_cost else 0.0

    @property
    def cost_per_student_day(self) -> float:
        return self.total_cost / self.student_days if self.student_days else 0.0


def aggregate(rows: list[dict], window_days: int) -> CostStats:
    """Aggregate ledger rows. Each row: kind, cost, in_tokens, cache_tokens,
    student_id, day (a date). Pure."""
    st = CostStats(window_days=window_days)
    seen_student_days = set()
    for r in rows:
        kind = r["kind"]
        cost = float(r.get("cost") or 0.0)
        st.n_turns += 1
        st.total_cost += cost
        bucket = st.by_kind.setdefault(kind, {"n": 0, "cost": 0.0})
        bucket["n"] += 1
        bucket["cost"] += cost
        if kind in ACTOR_KINDS:
            st.actor_cost += cost
        elif kind in SUPERVISOR_KINDS:
            st.supervisor_cost += cost
        else:
            st.other_cost += cost
        st.in_tokens += int(r.get("in_tokens") or 0)
        st.cache_tokens += int(r.get("cache_tokens") or 0)
        st.cache_write_tokens += int(r.get("cache_write_tokens") or 0)
        if r.get("student_id") is not None and r.get("day") is not None:
            seen_student_days.add((r["student_id"], r["day"]))
    st.student_days = len(seen_student_days)
    return st


@dataclass
class CapRecommendation:
    per_turn_usd: float
    turns_per_active_day: int
    active_days_per_quarter: int
    students: int
    basis: str  # "ledger" | "baseline"

    @property
    def per_student_quarter_usd(self) -> float:
        return self.per_turn_usd * self.turns_per_active_day * self.active_days_per_quarter

    @property
    def daily_exchange_limit(self) -> int:
        # Cap a single student's chat turns/day at ~2x the expected use, so a
        # runaway loop is bounded without throttling normal study.
        return max(20, int(math.ceil(self.turns_per_active_day * 2 / 10.0) * 10))

    @property
    def monthly_cost_cap_usd(self) -> float:
        # Class-wide monthly ceiling = quarterly per-student budget * students / 3
        # months, rounded up to a round number with ~30% headroom.
        raw = self.per_student_quarter_usd * self.students / 3.0 * 1.3
        return float(max(10, int(math.ceil(raw / 5.0) * 5)))


def recommend_caps(per_turn_usd: float, *, students: int = 30,
                   turns_per_active_day: int = 15, active_days_per_quarter: int = 30,
                   basis: str = "ledger") -> CapRecommendation:
    return CapRecommendation(
        per_turn_usd=per_turn_usd,
        turns_per_active_day=turns_per_active_day,
        active_days_per_quarter=active_days_per_quarter,
        students=students,
        basis=basis,
    )


def render_report(stats: CostStats, rec: CapRecommendation,
                  generated_at: Optional[str] = None) -> str:
    generated_at = generated_at or datetime.date.today().isoformat()
    thin = stats.n_turns < 50
    lines = [
        f"# Cost report — {generated_at}",
        "",
        "Generated by `python -m pipeline cost-report` from the TurnEvent ledger",
        "(the single source of cost truth). Closes the deferred budget decision",
        "with data [DECIDED: instrument first].",
        "",
        f"## Ledger window: last {stats.window_days} days",
        "",
        f"- turns recorded: **{stats.n_turns}**",
        f"- total cost: **${stats.total_cost:.4f}**",
        f"- avg cost/turn: **${stats.per_turn_avg:.4f}**",
        f"- cost per student-day: **${stats.cost_per_student_day:.4f}** "
        f"({stats.student_days} student-days)",
        f"- cache-read share of input: **{stats.cache_share:.0%}**"
        + ("  ⚠️ below 60%" if 0 < stats.cache_share < 0.60 else ""),
        f"- supervisor overhead share: **{stats.supervisor_share:.0%}** "
        f"(Haiku classify/judge/strategist vs Sonnet actor)",
        "",
        "### Cost by kind",
        "",
        "| kind | turns | total $ | avg $/turn |",
        "|------|-------|---------|------------|",
    ]
    for kind in sorted(stats.by_kind, key=lambda k: -stats.by_kind[k]["cost"]):
        b = stats.by_kind[kind]
        avg = b["cost"] / b["n"] if b["n"] else 0.0
        lines.append(f"| {kind} | {b['n']} | ${b['cost']:.4f} | ${avg:.4f} |")
    lines += [
        "",
        "## Recommended budget and caps",
        "",
        f"Basis: **{rec.basis}** "
        + ("(ledger too thin; the baseline per-turn cost)"
           if rec.basis == "baseline" or thin else "(measured)"),
        "",
        f"Assumptions: {rec.students} students, ~{rec.turns_per_active_day} turns "
        f"per active day, ~{rec.active_days_per_quarter} active days/quarter, "
        f"${rec.per_turn_usd:.4f}/turn.",
        "",
        f"- **Per-student-quarter budget: ~${rec.per_student_quarter_usd:.2f}**",
        f"- `DAILY_EXCHANGE_LIMIT` = **{rec.daily_exchange_limit}** "
        "(bounds a single runaway student without throttling study)",
        f"- `MONTHLY_COST_CAP_USD` = **{rec.monthly_cost_cap_usd:.0f}** "
        "(class-wide ceiling with ~30% headroom)",
        "",
        "Set both on Render (`render.yaml` env). `usage.over_caps` enforces them",
        "inside `run_turn`, so every entry point inherits the limit.",
        "",
    ]
    if thin:
        lines += [
            "> NOTE: fewer than 50 turns in the ledger. The per-turn cost and the",
            "> recommendation lean on the Session 1/6 live baseline",
            "> (~$0.02 no-tool, ~$0.03 tool, ~$0.05 revise per turn all-in).",
            "> Re-run after a few weeks of volunteer use to",
            "> replace the assumptions with measured values.",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB collection
# ---------------------------------------------------------------------------

def collect_rows(db, days: int) -> list[dict]:
    from tutor.db.models import TurnEvent

    since = utcnow() - datetime.timedelta(days=days)
    out = []
    for te in db.query(TurnEvent).filter(TurnEvent.ts >= since).all():
        out.append({
            "kind": te.kind,
            "cost": te.cost_usd,
            "in_tokens": te.in_tokens,
            "cache_tokens": te.cache_read_tokens,
            "cache_write_tokens": te.cache_write_tokens or 0,
            "student_id": te.student_id,
            "day": te.ts.date() if te.ts else None,
        })
    return out


# Baseline per-turn cost (all-in), measured on the first live turns, for when
# the ledger is too thin to measure: a blend of no-tool/tool/revise turns.
BASELINE_PER_TURN_USD = 0.03


@click.command("cost-report")
@click.option("--days", default=30, show_default=True)
@click.option("--students", default=30, show_default=True)
@click.option("--per-turn-usd", default=None, type=float,
              help="Override per-turn cost (else measured, else baseline).")
@click.option("--dry-run", is_flag=True, help="Print, do not write docs/cost-report.md.")
def cost_report(days: int, students: int, per_turn_usd: Optional[float], dry_run: bool) -> None:
    from tutor.db.models import get_engine, get_session_factory
    from tutor.db.target import prepare_schema, print_target

    engine = get_engine()
    prepare_schema(engine, print_target(engine))   # read-only report
    db = get_session_factory(engine)()
    try:
        rows = collect_rows(db, days)
    finally:
        db.close()

    stats = aggregate(rows, window_days=days)

    if per_turn_usd is not None:
        ptc, basis = per_turn_usd, "override"
    elif stats.n_turns >= 50:
        ptc, basis = stats.per_turn_avg, "ledger"
    else:
        ptc, basis = BASELINE_PER_TURN_USD, "baseline"

    rec = recommend_caps(ptc, students=students, basis=basis)
    report = render_report(stats, rec)

    console.print(f"[bold]{stats.n_turns}[/bold] turns, ${stats.total_cost:.4f} total, "
                  f"${stats.per_turn_avg:.4f}/turn, supervisor share {stats.supervisor_share:.0%}")
    console.print(f"Recommend DAILY_EXCHANGE_LIMIT={rec.daily_exchange_limit}, "
                  f"MONTHLY_COST_CAP_USD={rec.monthly_cost_cap_usd:.0f} (basis: {basis})")
    if dry_run:
        console.print("[yellow]--dry-run: not written[/yellow]")
        return
    REPORT_PATH.write_text(report + "\n")
    console.print(f"[green]Wrote {REPORT_PATH.relative_to(ROOT)}[/green]")


if __name__ == "__main__":
    cost_report()
