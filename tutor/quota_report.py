"""The quota roll-up email: who hit a tutor quota in the last day, week, or
month (tutor/quotas.py).

Pure gather and render, like the other reports; scripts/quota_report.py does
the sending and .github/workflows/quota-report.yml runs it every morning.
Which periods go out is an admin setting (Supervision tab): the daily
roll-up covers yesterday, the weekly one last week and goes out on Monday,
the monthly one last month and goes out on the 1st. A period with no hits
sends nothing.
"""

from __future__ import annotations

import datetime
import html as _html
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor import quotas
from tutor.courses import IDENTITY
from tutor.utctime import window_label

_PERIOD_WORDS = {"daily": "yesterday", "weekly": "last week", "monthly": "last month"}


@dataclass
class RollUp:
    period: str
    since: datetime.datetime
    until: datetime.datetime
    quotas: dict
    students: list = field(default_factory=list)      # quotas.summarize_hits rows
    total_hits: int = 0

    @property
    def label(self) -> str:
        return window_label(self.since, self.until, times=False)

    @property
    def subject(self) -> str:
        n = len(self.students)
        return (f"{IDENTITY.name}: {n} student{'' if n == 1 else 's'} hit a tutor quota "
                f"({_PERIOD_WORDS[self.period]}, {self.label})")


def due_periods(db: DBSession, now: Optional[datetime.datetime] = None, force: Optional[str] = None) -> list[str]:
    """The periods to send today: ``force`` alone when given, else every
    period that is both switched on and due on this local day."""
    if force:
        return [force]
    flags = quotas.load_notify(db)
    return [p for p in quotas.PERIODS if flags.get(p) and quotas.period_is_due(p, now)]


def gather(db: DBSession, period: str, now: Optional[datetime.datetime] = None) -> RollUp:
    since, until = quotas.previous_period(period, now)
    rows = quotas.hits(db, since, until)
    return RollUp(period=period, since=since, until=until, quotas=quotas.load_quotas(db),
                  students=quotas.summarize_hits(rows), total_hits=len(rows))


def _windows(entry: dict) -> str:
    return ", ".join(f"{w} x{n}" if n > 1 else w for w, n in sorted(entry["windows"].items()))


def render_markdown(roll: RollUp, base_url: str) -> str:
    lines = [f"# Tutor quota hits, {_PERIOD_WORDS[roll.period]} ({roll.label})", "",
             f"Quotas in force: {quotas.describe(roll.quotas)} per student.", ""]
    if not roll.students:
        lines += ["Nobody hit a quota.", ""]
        return "\n".join(lines)
    lines += [f"{len(roll.students)} student(s), {roll.total_hits} hit(s). A hit is one student reaching "
              "one quota once in its period; the message told them when it comes back.", ""]
    for e in roll.students:
        who = e["name"] or e["email"]
        lines.append(f"- **{who}** ({e['email']}): {_windows(e)}; last on {e['last']:%Y-%m-%d}; "
                     f"spend at the hit ${e['spent_usd']:.2f}")
    lines += ["", f"Adjust the quotas: {base_url.rstrip('/')}/admin?tab=supervision", ""]
    return "\n".join(lines)


def render_html(roll: RollUp, base_url: str) -> str:
    esc = _html.escape
    font = "font-family:system-ui,-apple-system,Segoe UI,sans-serif"
    parts = [f"<div style='{font};max-width:680px'>",
             f"<h2 style='margin:0 0 4px'>Tutor quota hits, {esc(_PERIOD_WORDS[roll.period])}</h2>",
             f"<p style='color:#666;margin:0 0 12px'>{esc(roll.label)} · quotas in force: "
             f"{esc(quotas.describe(roll.quotas))} per student</p>"]
    if not roll.students:
        parts.append("<p>Nobody hit a quota.</p></div>")
        return "\n".join(parts)
    parts.append(f"<p>{len(roll.students)} student(s), {roll.total_hits} hit(s).</p>"
                 "<table style='border-collapse:collapse;font-size:14px'>"
                 "<tr><th style='text-align:left;padding:4px 8px'>Student</th>"
                 "<th style='text-align:left;padding:4px 8px'>Windows</th>"
                 "<th style='text-align:left;padding:4px 8px'>Last</th>"
                 "<th style='text-align:right;padding:4px 8px'>Spend at the hit</th></tr>")
    for e in roll.students:
        who = esc(e["name"] or e["email"])
        parts.append(f"<tr><td style='padding:4px 8px'>{who}<br><span style='color:#666;font-size:12px'>"
                     f"{esc(e['email'])}</span></td><td style='padding:4px 8px'>{esc(_windows(e))}</td>"
                     f"<td style='padding:4px 8px'>{e['last']:%Y-%m-%d}</td>"
                     f"<td style='padding:4px 8px;text-align:right'>${e['spent_usd']:.2f}</td></tr>")
    link = esc(base_url.rstrip("/") + "/admin?tab=supervision")
    parts.append(f"</table><p style='margin-top:14px'><a href='{link}'>Adjust the quotas</a></p></div>")
    return "\n".join(parts)
