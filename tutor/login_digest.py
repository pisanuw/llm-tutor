"""Sign-in problems in a window, for the daily digest and the admin Logins
tab (step 0.6; F28, F30, F35).

The trail (``login_attempts``) holds every step; this folds a window of it
into what the instructor acts on: the addresses that failed and never got
in afterwards, the unknown addresses that tried, the browsers that failed
with no address at all, the sign-ins started and never finished, the
rate-limited bursts, and the mail scanner's taps (counted apart, since they
spend nothing). Pure: one query, then Python, so the digest and the tab
show the same numbers.
"""

from __future__ import annotations

import collections
import datetime
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from tutor.auth import attempts as attempts_mod
from tutor.db.models import LoginAttempt, Student
from tutor.utctime import to_local

STARTS = frozenset({attempts_mod.STARTED, attempts_mod.LINK_SENT, attempts_mod.LINK_SHOWN})
TERMINAL = frozenset({attempts_mod.OK}) | attempts_mod.FAILED


@dataclass
class Problem:
    """One address's failures in the window, and whether they got in after."""
    email: str
    name: str = ""
    known: bool = False                 # a students row exists
    failures: int = 0
    outcomes: list[str] = field(default_factory=list)   # in order, newest last
    last_at: Optional[datetime.datetime] = None
    got_in_after: bool = False          # an ok row after the last failure
    scanner_taps: int = 0

    @property
    def last_outcome(self) -> str:
        return self.outcomes[-1] if self.outcomes else ""

    @property
    def when(self) -> str:
        return to_local(self.last_at).strftime("%a %b %d %H:%M") if self.last_at else ""


@dataclass
class LoginSummary:
    since: datetime.datetime
    until: datetime.datetime
    attempts: int = 0
    ok: int = 0
    problems: list[Problem] = field(default_factory=list)      # known or whitelisted addresses, unresolved first
    unknown: list[Problem] = field(default_factory=list)       # addresses with no students row (denied, mostly)
    anonymous_failures: int = 0                                # rows with no address from a browser
    scanner_taps: int = 0
    rate_limited: int = 0
    expired: int = 0
    access_removed: int = 0
    unfinished_magic: int = 0
    unfinished_google: int = 0

    @property
    def unresolved(self) -> list[Problem]:
        return [p for p in self.problems if not p.got_in_after]

    @property
    def empty(self) -> bool:
        return not (self.unresolved or self.unknown or self.anonymous_failures or self.rate_limited
                    or self.unfinished_magic or self.unfinished_google or self.access_removed)


def _scanner(row: LoginAttempt) -> bool:
    from tutor.request_errors import BROWSER, classify_agent

    return classify_agent(row.user_agent) != BROWSER


def summary(db: DBSession, since: datetime.datetime, until: datetime.datetime) -> LoginSummary:
    rows = (
        db.query(LoginAttempt)
        .filter(LoginAttempt.created_at >= since, LoginAttempt.created_at < until)
        .order_by(LoginAttempt.created_at.asc(), LoginAttempt.id.asc())
        .all()
    )
    out = LoginSummary(since=since, until=until, attempts=len(rows))
    by_email: dict[str, Problem] = {}
    open_starts: dict[tuple[str, str], int] = collections.Counter()
    for r in rows:
        email = (r.email or "").strip().lower()
        if r.outcome == attempts_mod.OK:
            out.ok += 1
            if email in by_email:
                by_email[email].got_in_after = True
        if r.outcome in STARTS:
            open_starts[(r.method, email if r.method == attempts_mod.MAGIC else "")] += 1
        elif r.outcome in TERMINAL:
            key = (r.method, email if r.method == attempts_mod.MAGIC else "")
            if open_starts.get(key):
                open_starts[key] -= 1
        if r.outcome == attempts_mod.RATE_LIMITED:
            out.rate_limited += 1
        if r.outcome == attempts_mod.EXPIRED:
            out.expired += 1
        if r.outcome == attempts_mod.ACCESS_REMOVED:
            out.access_removed += 1
        if r.outcome not in attempts_mod.FAILED:
            continue
        if _scanner(r):
            out.scanner_taps += 1
            if email:
                by_email.setdefault(email, Problem(email=email)).scanner_taps += 1
            continue
        if not email:
            out.anonymous_failures += 1
            continue
        p = by_email.setdefault(email, Problem(email=email))
        p.failures += 1
        p.outcomes.append(r.outcome)
        p.last_at = r.created_at
        p.got_in_after = False
    for (method, _), n in open_starts.items():
        if method == attempts_mod.MAGIC:
            out.unfinished_magic += n
        elif method == attempts_mod.GOOGLE:
            out.unfinished_google += n
    emails = [e for e in by_email if e]
    names = {s.email.lower(): s for s in db.query(Student).filter(Student.email.in_(emails or ["-"])).all()}
    for email, p in by_email.items():
        if p.failures == 0:
            continue
        s = names.get(email)
        p.known = s is not None
        p.name = (s.name or "") if s else ""
        (out.problems if p.known else out.unknown).append(p)
    out.problems.sort(key=lambda p: (p.got_in_after, -(p.failures), p.email))
    out.unknown.sort(key=lambda p: (-(p.failures), p.email))
    return out


def render_markdown(s: Optional[LoginSummary]) -> str:
    """The digest's "Sign-in problems" section; empty when there is nothing
    to act on (a window of plain successes says nothing)."""
    if s is None or s.empty:
        return ""
    lines = ["## Sign-in problems", ""]
    for p in s.unresolved:
        who = f"{p.name} <{p.email}>" if p.name else p.email
        lines.append(f"- **{who}**: {p.failures} failure{'' if p.failures == 1 else 's'} "
                     f"({', '.join(p.outcomes[-4:])}), last {p.when}, not signed in since")
    resolved = [p for p in s.problems if p.got_in_after]
    if resolved:
        lines.append(f"- Got in after failing: {', '.join(p.email for p in resolved)}")
    if s.unknown:
        lines.append("- Not on the whitelist and tried: "
                     + ", ".join(f"{p.email} ({p.failures})" for p in s.unknown[:10])
                     + (" ..." if len(s.unknown) > 10 else ""))
    if s.anonymous_failures:
        lines.append(f"- Failures with no address (a dead link opened from an unknown browser): {s.anonymous_failures}")
    if s.rate_limited:
        lines.append(f"- Rate-limited requests: {s.rate_limited}")
    if s.unfinished_magic or s.unfinished_google:
        parts = []
        if s.unfinished_magic:
            parts.append(f"{s.unfinished_magic} magic link{'' if s.unfinished_magic == 1 else 's'}")
        if s.unfinished_google:
            parts.append(f"{s.unfinished_google} Google sign-in{'' if s.unfinished_google == 1 else 's'}")
        lines.append(f"- Started and not completed: {', '.join(parts)}")
    if s.access_removed:
        lines.append(f"- Signed-in students taken off the whitelist: {s.access_removed}")
    if s.scanner_taps:
        lines.append(f"- Mail scanner taps on links (harmless): {s.scanner_taps}")
    lines.append("")
    return "\n".join(lines)
