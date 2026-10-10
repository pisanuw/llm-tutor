"""Nightly auditor: a Sonnet re-judge of recent turns (Session 22, Task 7).

The live judge is Haiku and runs only on risky turns (cost). The auditor is the
higher tier (Sonnet, the AUDITOR registry entry) and runs offline: it re-judges
every risky turn from the last 24h plus a sample of routine turns (covering the
S17 grounding gap on routine concept chat), records each verdict as a
JudgeSample, and reports the disagreement rates and cache health. The live
pipeline is never blocked by it; this is pure observability (P6).

Definitions:
  under-block  the live system was too lenient: the auditor would revise/block a
               turn the live judge allowed. The dangerous direction.
  over-block   the live system was too strict: the auditor would allow a turn the
               live judge revised/blocked. Tracked because over-blocking pushes
               students to unguarded ChatGPT (P4).

Usage:
    python -m pipeline audit-turns --dry-run       # count what would be audited; no calls, no writes
    python -m pipeline audit-turns                 # last 24h on local SQLite, write docs/audit/<date>.md
    python -m pipeline audit-turns --allow-prod    # the same against production (the nightly Action)
    python -m pipeline audit-turns --hours 48 --sample-rate 0.1

Cost: one Sonnet call per audited turn. Off by default in CI; the GitHub Action
cron runs it where ANTHROPIC_API_KEY is set. --dry-run never builds a client, so
it makes no model calls and writes no JudgeSample rows. A real run against a
non-SQLite database refuses without --allow-prod (tutor/db/target.py).

Reports name students only as ``s<id>`` (student_pseudonym). The nightly Action
uploads the report as a short-lived workflow artifact; it never commits it, and
dated reports under docs/audit/ are gitignored.
"""

from __future__ import annotations

import datetime
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from tutor.agent.models import AUDITOR
from tutor.utctime import utcnow

ROOT = Path(__file__).parent.parent
AUDIT_DIR = ROOT / "docs" / "audit"
console = Console()

_SEVERITY = {"allow": 0, "revise": 1, "block": 2}


# ---------------------------------------------------------------------------
# Pure helpers (no DB, no network) — the testable core
# ---------------------------------------------------------------------------

def _parse(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def live_verdict(turn_event_verdicts_json: Optional[str]) -> str:
    """The verdict the live pipeline reached, or 'allow' for an unjudged turn."""
    v = _parse(turn_event_verdicts_json).get("verdict")
    return v if v in _SEVERITY else "allow"


def student_pseudonym(student_id: Optional[int]) -> str:
    """How a report names a student: ``s<id>``, never an email or name.

    Reports leave the database (workflow artifacts, a local docs/audit/ file),
    so they carry only the row id. An operator can look the id up in admin;
    anyone else holding the report cannot, and erasing the student deletes the
    rows the id would lead to.
    """
    return f"s{student_id}" if student_id is not None else "s?"


def is_risky(verdicts_json: Optional[str], det_check_json: Optional[str]) -> bool:
    """A turn the live judge ran on, or the detector cut."""
    verd = _parse(verdicts_json)
    det = _parse(det_check_json)
    if verd.get("verdict") in ("revise", "block"):
        return True
    if det and (det.get("cut") or det.get("risky")):
        return True
    return "verdict" in verd


def classify(live: str, audit: str) -> str:
    a, l = _SEVERITY.get(audit, 0), _SEVERITY.get(live, 0)
    if a > l:
        return "under_block"
    if a < l:
        return "over_block"
    return "agree"


def sample_routine(ids: list[int], rate: float, seed: int = 0) -> list[int]:
    """Deterministically sample a fraction of routine-turn ids."""
    if rate <= 0 or not ids:
        return []
    if rate >= 1:
        return list(ids)
    k = max(1, round(len(ids) * rate))
    rng = random.Random(seed)
    return sorted(rng.sample(list(ids), min(k, len(ids))))


def reconstruct_envelope(envelope_json: Optional[str]):
    """Rebuild an Envelope from its stored JSON for re-rendering to the judge."""
    from tutor.agent.policy import Envelope, Move

    d = _parse(envelope_json)
    move = None
    if d.get("move"):
        try:
            move = Move(d["move"])
        except ValueError:
            move = None
    return Envelope(
        max_rung=int(d.get("max_rung", 1)),
        goals=list(d.get("goals", [])),
        exam_lockdown=list(d.get("exam_lockdown", [])),
        must_ground=bool(d.get("must_ground", False)),
        allow_exercise=bool(d.get("allow_exercise", True)),
        escalate_review=bool(d.get("escalate_review", False)),
        reason=str(d.get("reason", "")),
        move=move,
        move_reason=str(d.get("move_reason", "")),
    )


@dataclass
class AuditReport:
    generated_at: str
    hours: int
    n_risky: int
    n_routine_sampled: int
    n_audited: int
    agree: int = 0
    over_block: int = 0
    under_block: int = 0
    move_turns: int = 0
    move_noncompliant: int = 0
    cache_read_share: float = 0.0
    cache_alarm: bool = False
    rows: list[dict] = field(default_factory=list)

    @property
    def agree_rate(self) -> float:
        return self.agree / self.n_audited if self.n_audited else 1.0

    @property
    def over_block_rate(self) -> float:
        return self.over_block / self.n_audited if self.n_audited else 0.0

    @property
    def under_block_rate(self) -> float:
        return self.under_block / self.n_audited if self.n_audited else 0.0

    @property
    def move_compliance(self) -> float:
        return 1.0 - (self.move_noncompliant / self.move_turns) if self.move_turns else 1.0


def render_report(report: AuditReport) -> str:
    """Render the report as Markdown (pure)."""
    cache_flag = " ⚠️ BELOW 60%" if report.cache_alarm else ""
    lines = [
        f"# Turn audit — {report.generated_at}",
        "",
        f"Window: last {report.hours}h. Auditor: `{AUDITOR.id}`.",
        "",
        "## Coverage",
        f"- risky turns re-judged: {report.n_risky}",
        f"- routine turns sampled: {report.n_routine_sampled}",
        f"- total audited: {report.n_audited}",
        "",
        "## Agreement with the live judge",
        f"- agree: {report.agree} ({report.agree_rate:.0%})",
        f"- under-block (live too lenient): {report.under_block} ({report.under_block_rate:.0%})",
        f"- over-block (live too strict): {report.over_block} ({report.over_block_rate:.0%})",
        "",
        "## Move compliance",
        f"- turns with an assigned move: {report.move_turns}",
        f"- compliance: {report.move_compliance:.0%}",
        "",
        "## Cache health (last 7d)",
        f"- cache-read share of input tokens: {report.cache_read_share:.0%}{cache_flag}",
        "",
    ]
    disagreements = [r for r in report.rows if r["category"] != "agree"]
    if disagreements:
        lines.append("## Disagreements")
        lines.append("")
        lines.append("| turn | student | live | audit | category | reason |")
        lines.append("|------|---------|------|-------|----------|--------|")
        for r in disagreements[:50]:
            reason = (r.get("reason", "") or "").replace("|", "/")[:80]
            lines.append(
                f"| {r['turn_event_id']} | {r['student']} | {r['live']} | "
                f"{r['audit']} | {r['category']} | {reason} |"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB-touching orchestration
# ---------------------------------------------------------------------------

def cache_health(db, days: int = 7) -> tuple[float, bool]:
    """Return (cache_read_share, alarm) over the last ``days`` of turns.

    Cache writes count as input (plan step 2.1): a cache that keeps being
    rewritten instead of read must lower the share, not vanish from it.
    """
    from sqlalchemy import func
    from tutor.db.models import TurnEvent

    since = utcnow() - datetime.timedelta(days=days)
    in_tok, cache_tok, write_tok = (
        db.query(func.coalesce(func.sum(TurnEvent.in_tokens), 0),
                 func.coalesce(func.sum(TurnEvent.cache_read_tokens), 0),
                 func.coalesce(func.sum(TurnEvent.cache_write_tokens), 0))
        .filter(TurnEvent.ts >= since)
        .one()
    )
    denom = (in_tok or 0) + (cache_tok or 0) + (write_tok or 0)
    share = (cache_tok or 0) / denom if denom else 0.0
    return share, (denom > 0 and share < 0.60)


def _find_draft(db, student_id: int, ts: datetime.datetime, window_sec: int = 300) -> str:
    """The assistant Message for this student closest in time to the turn."""
    from tutor.db.models import Message

    lo = ts - datetime.timedelta(seconds=window_sec)
    hi = ts + datetime.timedelta(seconds=window_sec)
    rows = (
        db.query(Message)
        .filter(Message.student_id == student_id, Message.role == "assistant",
                Message.created_at >= lo, Message.created_at <= hi)
        .all()
    )
    if not rows:
        return ""
    return min(rows, key=lambda m: abs((m.created_at - ts).total_seconds())).content


def _audit_one(db, te, client) -> Optional[dict]:
    """Re-judge one turn with the auditor model; record a JudgeSample. Returns a
    row dict, or None if the draft could not be reconstructed."""
    from tutor.agent import judge as judge_mod, state

    draft = _find_draft(db, te.student_id, te.ts)
    if not draft:
        return None

    envelope = reconstruct_envelope(te.envelope_json)
    # Re-judge grounding against the sources the live judge saw (persisted on the
    # TurnEvent). Without them the auditor judged sourceless and over-flagged
    # must_ground. Legacy rows have none -> [].
    try:
        sources = json.loads(te.sources_json) if te.sources_json else []
        if not isinstance(sources, list):
            sources = []
    except (ValueError, TypeError):
        sources = []
    user = judge_mod._render_input(envelope, draft, sources)
    try:
        resp = client.messages.create(
            model=AUDITOR.id, max_tokens=300, temperature=0,
            system=judge_mod._SYSTEM, messages=[{"role": "user", "content": user}],
        )
        verdict, rule, reason = judge_mod._parse(resp.content[0].text)
    except Exception as exc:  # an audit failure must never crash the batch
        return {"turn_event_id": te.id, "error": str(exc)}

    state.add_judge_sample(db, te.id, judge=f"auditor:{AUDITOR.id}", verdict=verdict, reason=reason)

    live = live_verdict(te.verdicts_json)
    return {
        "turn_event_id": te.id,
        "student": student_pseudonym(te.student_id),
        "live": live,
        "audit": verdict,
        "rule": rule,
        "reason": reason,
        "category": classify(live, verdict),
        "has_move": reconstruct_envelope(te.envelope_json).move is not None,
    }


def run_audit(db, hours: int = 24, sample_rate: float = 0.1, client=None,
              now: Optional[datetime.datetime] = None, seed: int = 0,
              judge: bool = True) -> AuditReport:
    """Select, re-judge, and aggregate. ``client`` is injected in tests.

    With ``judge=False`` (the CLI's --dry-run) it only selects and counts: no
    auditor client, no model calls, no JudgeSample rows.
    """
    from tutor.db.models import TurnEvent

    now = now or utcnow()
    since = now - datetime.timedelta(hours=hours)
    turns = (
        db.query(TurnEvent)
        .filter(TurnEvent.ts >= since, TurnEvent.kind == "chat")
        .order_by(TurnEvent.id)
        .all()
    )
    risky = [t for t in turns if is_risky(t.verdicts_json, t.det_check_json)]
    routine = [t for t in turns if t not in risky]
    sampled_ids = set(sample_routine([t.id for t in routine], sample_rate, seed))
    sampled = [t for t in routine if t.id in sampled_ids]
    selected = risky + sampled

    report = AuditReport(
        generated_at=now.strftime("%Y-%m-%d"),
        hours=hours,
        n_risky=len(risky),
        n_routine_sampled=len(sampled),
        n_audited=0,
    )

    if not judge:
        selected = []
    if selected and client is None:
        import anthropic
        from tutor.config import get_config
        key = get_config().get("anthropic_api_key", "")
        if not key:
            console.print("[yellow]No ANTHROPIC_API_KEY; skipping re-judge.[/yellow]")
            selected = []
        else:
            client = anthropic.Anthropic(api_key=key)

    for te in selected:
        row = _audit_one(db, te, client)
        if row is None or "error" in row:
            continue
        report.rows.append(row)
        report.n_audited += 1
        report.agree += row["category"] == "agree"
        report.over_block += row["category"] == "over_block"
        report.under_block += row["category"] == "under_block"
        if row["has_move"]:
            report.move_turns += 1
            if "move" in (row.get("rule", "") or "").lower():
                report.move_noncompliant += 1
    db.flush()

    report.cache_read_share, report.cache_alarm = cache_health(db)
    return report


def write_report(report: AuditReport, out_dir: Path = AUDIT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.generated_at}.md"
    path.write_text(render_report(report) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command("audit-turns")
@click.option("--hours", default=24, show_default=True)
@click.option("--sample-rate", default=0.1, show_default=True)
@click.option("--dry-run", is_flag=True,
              help="Count the turns that would be audited: no model calls, no writes.")
@click.option("--allow-prod", is_flag=True,
              help="Allow writing JudgeSample rows to a non-SQLite (production) database.")
def audit_turns(hours: int, sample_rate: float, dry_run: bool, allow_prod: bool) -> None:
    from tutor.db.models import get_engine, get_session_factory
    from tutor.db.target import prepare_schema, print_target, require_prod_ok

    engine = get_engine()
    target = print_target(engine)
    if not dry_run:
        require_prod_ok(target, allow_prod=allow_prod, action="write auditor JudgeSample rows")
    prepare_schema(engine, target)
    db = get_session_factory(engine)()
    try:
        report = run_audit(db, hours=hours, sample_rate=sample_rate, judge=not dry_run)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        db.close()

    console.print(f"[bold]Audited {report.n_audited}[/bold] turns "
                  f"({report.n_risky} risky + {report.n_routine_sampled} routine)")
    console.print(f"  agree {report.agree_rate:.0%}  under-block {report.under_block_rate:.0%}  "
                  f"over-block {report.over_block_rate:.0%}")
    console.print(f"  cache share {report.cache_read_share:.0%}"
                  + ("  [red]ALARM[/red]" if report.cache_alarm else ""))
    if dry_run:
        console.print("[yellow]--dry-run: report not written[/yellow]")
        return
    path = write_report(report)
    console.print(f"[green]Wrote {path.relative_to(ROOT)}[/green]")


if __name__ == "__main__":
    audit_turns()
