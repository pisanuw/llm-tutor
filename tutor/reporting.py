"""Daily per-user activity report (pure aggregation + markdown rendering).

Turns the raw ledger (Message / TurnEvent / MasteryAttempt) into one summary per
active student for a time window, plus a cohort digest. Pure and network-free so
it is unit-testable; the CLI that queries the DB, writes files, and emails the
digest lives in ``scripts/daily_report.py``.

Design mirrors the rest of the codebase: aggregation is deterministic code (no
LLM), free text is summarised as counts, and a "slow turn" threshold surfaces the
"tutor is thinking forever" failure mode the report exists to catch.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from tutor.courses import IDENTITY


@dataclass
class UserDaily:
    """One student's activity over the window."""
    student_id: int
    email: str
    name: str
    turns: int = 0
    user_messages: int = 0
    exams_passed: int = 0
    exams_failed: int = 0
    total_cost_usd: float = 0.0
    avg_latency_ms: Optional[int] = None
    max_latency_ms: Optional[int] = None
    slow_turns: int = 0
    # Time to the first word the student saw (verdicts.timings, recorded since
    # the latency work); None for turns from before that.
    max_ttft_ms: Optional[int] = None
    # Turns that wrote the prompt cache (it had expired): a few seconds each.
    cold_turns: int = 0
    errors: int = 0
    high_conf_misses: int = 0
    # A second hostile message in a day restricted the student (tutor/agent/conduct.py).
    restricted: int = 0
    first_at: Optional[datetime.datetime] = None
    last_at: Optional[datetime.datetime] = None
    _latencies: list[int] = field(default_factory=list, repr=False)

    @property
    def active(self) -> bool:
        # A student whose only turn failed before stage 1 (a kind=error row)
        # is on the report too: they tried, and the digest is where that shows.
        return bool(self.turns or self.user_messages or self.exams_passed or self.exams_failed
                    or self.errors)


# Every way a turn fails that the student saw as a friendly error, not only
# the crash row: an actor stream error, an empty reply, a crash finalized on
# the provisional row, a grader that was down, a judge that produced no
# verdict on a high-risk turn (F48).
_FAILED_VERDICTS = frozenset({"stream_error", "empty_reply", "crash"})


def _failed_turn(kind: str, verdicts: Any) -> bool:
    if kind == "error":
        return True
    if not isinstance(verdicts, dict):
        return False
    return bool(verdicts.get("error") or verdicts.get("grading_error")
                or verdicts.get("judge_unavailable")
                or verdicts.get("verdict") in _FAILED_VERDICTS)


def _bump_time(cur: Optional[datetime.datetime], ts: Optional[datetime.datetime], newest: bool):
    if ts is None:
        return cur
    if cur is None:
        return ts
    return max(cur, ts) if newest else min(cur, ts)


def build_user_dailies(
    students: list,
    messages: list,
    turn_events: list,
    mastery_attempts: list,
    *,
    slow_ms: int,
) -> list[UserDaily]:
    """Fold pre-fetched rows into one :class:`UserDaily` per active student.

    All inputs are already filtered to the window by the caller. ``students`` is
    the roster (id/email/name); the three ledgers are lists of ORM rows (or any
    objects with the same attributes). Returns only students with activity,
    busiest first.
    """
    by_id: dict[int, UserDaily] = {}
    for s in students:
        by_id[s.id] = UserDaily(student_id=s.id, email=s.email, name=s.name or "")

    def slot(sid: int) -> Optional[UserDaily]:
        return by_id.get(sid)

    for m in messages:
        u = slot(m.student_id)
        if u is None:
            continue
        if m.role == "user":
            u.user_messages += 1
        ts = getattr(m, "created_at", None)
        u.first_at = _bump_time(u.first_at, ts, newest=False)
        u.last_at = _bump_time(u.last_at, ts, newest=True)

    for te in turn_events:
        u = slot(te.student_id)
        if u is None:
            continue
        if te.kind == "chat":
            u.turns += 1
        u.total_cost_usd += te.cost_usd or 0.0
        lat = te.latency_ms
        if lat is not None:
            u._latencies.append(lat)
            if lat >= slow_ms:
                u.slow_turns += 1
        verdicts = _safe_json(te.verdicts_json)
        if te.kind == "chat" and (getattr(te, "cache_write_tokens", None) or 0) > 0:
            u.cold_turns += 1
        timings = verdicts.get("timings") if isinstance(verdicts, dict) else None
        ttft = timings.get("first_token_at_ms") if isinstance(timings, dict) else None
        if isinstance(ttft, int):
            u.max_ttft_ms = ttft if u.max_ttft_ms is None else max(u.max_ttft_ms, ttft)
        if _failed_turn(te.kind, verdicts):
            u.errors += 1
        if te.kind == "conduct" and isinstance(verdicts, dict) and verdicts.get("action") == "restrict":
            u.restricted += 1
        if isinstance(verdicts, dict) and verdicts.get("miscalibrated"):
            u.high_conf_misses += 1
        ts = getattr(te, "ts", None)
        u.first_at = _bump_time(u.first_at, ts, newest=False)
        u.last_at = _bump_time(u.last_at, ts, newest=True)

    for a in mastery_attempts:
        u = slot(a.student_id)
        if u is None:
            continue
        if a.status == "passed":
            u.exams_passed += 1
        elif a.status == "failed":
            u.exams_failed += 1

    for u in by_id.values():
        if u._latencies:
            u.avg_latency_ms = round(sum(u._latencies) / len(u._latencies))
            u.max_latency_ms = max(u._latencies)

    active = [u for u in by_id.values() if u.active]
    active.sort(key=lambda u: (u.turns, u.user_messages), reverse=True)
    return active


def _safe_json(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _fmt_ts(ts: Optional[datetime.datetime]) -> str:
    return ts.strftime("%H:%M UTC") if ts else "—"


def render_user_markdown(u: UserDaily, date: str) -> str:
    """One student's day as a standalone markdown file."""
    lines = [
        f"# Activity — {u.name or u.email}",
        "",
        f"- **Date:** {date}",
        f"- **Email:** {u.email}",
        f"- **Active:** {_fmt_ts(u.first_at)} → {_fmt_ts(u.last_at)}",
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Chat turns | {u.turns} |",
        f"| Messages sent | {u.user_messages} |",
        f"| Exams passed / failed | {u.exams_passed} / {u.exams_failed} |",
        f"| Est. cost (USD) | ${u.total_cost_usd:.4f} |",
        f"| Avg / max latency | {_ms(u.avg_latency_ms)} / {_ms(u.max_latency_ms)} |",
        f"| Max time to first word | {_ms(u.max_ttft_ms)} |",
        f"| Cold-cache turns | {u.cold_turns} |",
        f"| Slow turns | {u.slow_turns} |",
        f"| Errors | {u.errors} |",
        f"| High-confidence misses | {u.high_conf_misses} |",
        "",
    ]
    flags = []
    if u.slow_turns:
        flags.append(f"⚠️ {u.slow_turns} slow turn(s) — possible 'thinking forever' latency.")
    if u.errors:
        flags.append(f"⚠️ {u.errors} error turn(s).")
    if u.restricted:
        flags.append("⚠️ restricted after repeated hostile messages (Admin → Supervision).")
    if flags:
        lines.append("## Flags")
        lines.append("")
        lines.extend(f"- {f}" for f in flags)
        lines.append("")
    return "\n".join(lines)


def render_digest_markdown(reports: list[UserDaily], date: str, learning_md: str = "",
                           base_url: str = "") -> str:
    """Cohort summary used as the email body. ``learning_md`` (from
    tutor.learning_metrics) is placed right after the headline numbers so the
    report leads with learning, not usage (pedagogy item 13). With a
    ``base_url``, every flagged student gets a link to their conversations on
    the admin page, so a flag is one click from its transcript."""
    n = len(reports)
    total_turns = sum(u.turns for u in reports)
    total_cost = sum(u.total_cost_usd for u in reports)
    total_slow = sum(u.slow_turns for u in reports)
    total_err = sum(u.errors for u in reports)
    lines = [
        f"# Daily tutor report — {date}",
        "",
        f"- **Active students:** {n}",
        f"- **Chat turns:** {total_turns}",
        f"- **Est. cost:** ${total_cost:.2f}",
        f"- **Slow turns:** {total_slow}   |   **Error turns:** {total_err}",
        "",
    ]
    if total_slow or total_err:
        lines.append("> ⚠️ Slow/error turns present — check the flagged users below.")
        lines.append("")
    if learning_md:
        lines.append(learning_md.rstrip("\n"))
        lines.append("")
    if not reports:
        lines.append("_No student activity in this window._")
        return "\n".join(lines)
    lines += [
        "## Per student",
        "",
        "| Student | Turns | Msgs | Exams P/F | Cost | Max latency | First word | Cold | Slow | Err |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for u in reports:
        who = u.name or u.email
        flag = " ⚠️" if (u.slow_turns or u.errors or u.restricted) else ""
        lines.append(
            f"| {who}{flag} | {u.turns} | {u.user_messages} | "
            f"{u.exams_passed}/{u.exams_failed} | ${u.total_cost_usd:.3f} | "
            f"{_ms(u.max_latency_ms)} | {_ms(u.max_ttft_ms)} | {u.cold_turns} | "
            f"{u.slow_turns} | {u.errors} |"
        )
    lines.append("")
    flagged = [u for u in reports if u.slow_turns or u.errors or u.restricted]
    if base_url and flagged:
        lines += ["## Flagged conversations", ""]
        for u in flagged:
            lines.append(f"- {u.name or u.email}: {conversations_link(base_url, u.email)}")
        lines.append("")
    return "\n".join(lines)


def conversations_link(base_url: str, email: str) -> str:
    """The admin Conversations tab opened on one student."""
    from urllib.parse import quote

    return f"{base_url.rstrip('/')}/admin?tab=conversations&student={quote(email, safe='')}"


def _ms(v: Optional[int]) -> str:
    if v is None:
        return "—"
    return f"{v/1000:.1f}s" if v >= 1000 else f"{v}ms"


def safe_filename(email: str) -> str:
    """A filesystem-safe stem for a per-user file, derived from the email."""
    keep = [c if (c.isalnum() or c in "-_.@") else "_" for c in (email or "unknown")]
    return "".join(keep)[:100] or "unknown"


def render_errors_markdown(summary: Any) -> str:
    """The day's dead links and error pages (tutor.request_errors.summary):
    what students' browsers were answered with, by path, and who pressed
    the Tell-the-instructor button. Empty when there is nothing to say."""
    if summary is None or summary.empty:
        return ""
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(summary.by_kind.items(), key=lambda kv: -kv[1]))
    lines = [
        "## Dead links and errors",
        "",
        f"- **From students' browsers:** {summary.total}" + (f" ({kinds})" if kinds else "")
        + f", {summary.students} student{'' if summary.students == 1 else 's'}"
        + (f"; {summary.bots} from bots or scanners, not counted" if summary.bots else ""),
    ]
    if summary.top:
        lines += ["", "| Path | Status | Hits | Students | From |", "|---|--:|--:|--:|---|"]
        for t in summary.top:
            lines.append(f"| `{t.path}` | {t.status} | {t.hits} | {t.students} | "
                         f"{', '.join(f'`{r}`' for r in t.referrers) or '—'} |")
    if summary.reports:
        lines.append("")
        for ts, who, path in summary.reports:
            lines.append(f"- **{who}** pressed Tell {IDENTITY.instructor_short} on `{path}` ({_fmt_ts(ts)})")
    lines.append("")
    return "\n".join(lines)
