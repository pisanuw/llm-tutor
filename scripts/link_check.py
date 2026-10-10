"""Crawl the live site's public pages and check every link they hand out
(student-facing review, step 0.3; findings F10, F13).

    python -m scripts.link_check                      # against BASE_URL, else tutor.site_url in courses.yaml
    python -m scripts.link_check --site http://localhost:8000 --no-external

Three checks, each a line per problem and a non-zero exit when any fails:

1. The public pages (the login page, the help page, the book's index and
   every chapter it links, the explorables the curriculum names) and every
   same-site link, script, stylesheet and image on them answer 2xx or 3xx.
   Signed-in pages are covered by tests/test_internal_links.py in CI, which
   resolves their links against the app's routes.
2. The external links in the cached Teach-me openers (curriculum/teachme)
   answer; a site that refuses robots (403, 405, 429) is reported as blocked,
   not dead, since a student's browser may still get in.
3. With ADMIN_TOKEN, ``GET /admin/export/decks`` says every lecture of every
   course in courses.yaml has its deck on the instance (the slide seed is
   complete).

Read-only against the site; nothing here needs database credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from tutor.courses import IDENTITY, course_label
from tutor.paths import CURRICULUM_DIR

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITE = IDENTITY.site_url or "http://localhost:8000"


def _user_agent() -> str:
    """Names the tutor and, when courses.yaml gives one, its help page; ASCII
    only, as a header must be."""
    name = IDENTITY.name.encode("ascii", "ignore").decode().strip() or "llm-tutor"
    help_url = f" (+{IDENTITY.site_url}/help)" if IDENTITY.site_url else ""
    return f"{name} link check{help_url}"


USER_AGENT = _user_agent()
START_PAGES = ("/auth/login", "/help", "/book/index.html")
# Links under these prefixes need a session; the crawl stops at them.
GATED = ("/slides/", "/lecture-audio/", "/chat", "/map", "/review", "/lectures", "/instructor", "/admin",
         "/track-picker", "/welcome", "/path/", "/api/", "/auth/logout", "/errors/")
BLOCKED = {403, 405, 429}
_URL_IN_TEXT = re.compile(r"https?://[^\s)\]>\"'`]+")


class _Links(HTMLParser):
    """href, src and action attributes of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("href", "src", "action") and value:
                self.found.append(value)


def links_in(html: str, base: str) -> list[str]:
    parser = _Links()
    parser.feed(html)
    out = []
    for raw in parser.found:
        raw = raw.strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        out.append(urljoin(base, raw).split("#", 1)[0])
    return out


def same_site(url: str, site: str) -> bool:
    return urlsplit(url).netloc.lower() == urlsplit(site).netloc.lower()


def gated(path: str) -> bool:
    return path.startswith(GATED) or path in ("/", "")


@dataclass
class Report:
    checked: int = 0
    dead: list[str] = field(default_factory=list)       # "status url (from page)"
    blocked: list[str] = field(default_factory=list)
    missing_decks: dict[str, list[int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.dead and not any(self.missing_decks.values())


def _fetch(client: httpx.Client, url: str, head_first: bool = False) -> tuple[int, str]:
    """(status, body) with a HEAD first when asked (external sites), a GET
    otherwise or when the HEAD was refused; 0 for a connection failure."""
    try:
        if head_first:
            resp = client.head(url)
            if resp.status_code in (200, 301, 302, 303, 307, 308) or resp.status_code in BLOCKED:
                return resp.status_code, ""
        resp = client.get(url)
        return resp.status_code, resp.text if "text/html" in resp.headers.get("content-type", "") else ""
    except httpx.HTTPError as exc:
        return 0, f"{type(exc).__name__}"


def crawl(client: httpx.Client, site: str, report: Report, extra_pages: Iterable[str] = ()) -> None:
    """Every public page reachable from the start pages, and every link on it."""
    site = site.rstrip("/")
    queue = [site + p for p in START_PAGES] + [site + p for p in extra_pages]
    seen: set[str] = set()
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        status, body = _fetch(client, url)
        report.checked += 1
        if status == 0 or status >= 400:
            report.dead.append(f"{status or 'no answer'} {url}")
            continue
        if not body:
            continue
        for link in links_in(body, url):
            if not same_site(link, site):
                continue
            path = urlsplit(link).path
            if gated(path) or link in seen:
                continue
            if path.startswith("/book/") or path.startswith("/explorables/") or path in ("/help", "/auth/login"):
                queue.append(link)
            else:
                # A static asset or another public path: fetch once, do not crawl into it.
                seen.add(link)
                status, _ = _fetch(client, link)
                report.checked += 1
                if status == 0 or status >= 400:
                    report.dead.append(f"{status or 'no answer'} {link} (from {urlsplit(url).path})")


def opener_links(teachme_dir: Path = CURRICULUM_DIR / "teachme") -> dict[str, list[str]]:
    """External URLs in the cached Teach-me openers, with the files naming them."""
    found: dict[str, list[str]] = {}
    for path in sorted(teachme_dir.rglob("*.json")):
        text = path.read_text(errors="ignore")
        for url in _URL_IN_TEXT.findall(text):
            url = url.rstrip(".,;:")
            found.setdefault(url, []).append(str(path.relative_to(ROOT)))
    return found


def check_external(client: httpx.Client, report: Report, links: dict[str, list[str]]) -> None:
    for url, files in sorted(links.items()):
        status, detail = _fetch(client, url, head_first=True)
        report.checked += 1
        where = ", ".join(sorted(set(files))[:3])
        if status in BLOCKED:
            report.blocked.append(f"{status} {url} ({where})")
        elif status == 0 or status >= 400:
            report.dead.append(f"{status or detail} {url} ({where})")


def check_decks(client: httpx.Client, site: str, token: Optional[str], report: Report) -> None:
    if not token:
        report.notes.append("ADMIN_TOKEN unset: the decks on the instance were not counted.")
        return
    try:
        resp = client.get(site.rstrip("/") + "/admin/export/decks", headers={"X-Admin-Token": token})
    except httpx.HTTPError as exc:
        report.dead.append(f"{type(exc).__name__} /admin/export/decks")
        return
    if resp.status_code != 200:
        report.dead.append(f"{resp.status_code} /admin/export/decks")
        return
    data = resp.json()
    for course, missing in (data.get("missing") or {}).items():
        if missing:
            report.missing_decks[course] = list(missing)


def explorable_pages() -> list[str]:
    goals = json.loads((CURRICULUM_DIR / "learning_goals.json").read_text())["goals"]
    return [f"/explorables/{g['id']}.html" for g in goals if g.get("threshold")]


def render(report: Report) -> str:
    lines = [f"link check: {report.checked} checked, {len(report.dead)} dead, {len(report.blocked)} blocked"]
    for line in report.dead:
        lines.append(f"  DEAD     {line}")
    for line in report.blocked:
        lines.append(f"  blocked  {line}")
    for course, missing in sorted(report.missing_decks.items()):
        lines.append(f"  DECKS    {course_label(course)}: {len(missing)} lecture(s) without a deck on the instance: "
                     + ", ".join(str(n) for n in missing))
    for note in report.notes:
        lines.append(f"  note     {note}")
    return "\n".join(lines)


def run(site: str, token: Optional[str], external: bool = True, client: Optional[httpx.Client] = None) -> Report:
    report = Report()
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=20.0, headers={"User-Agent": USER_AGENT})
    try:
        crawl(client, site, report, extra_pages=explorable_pages())
        if external:
            check_external(client, report, opener_links())
        check_decks(client, site, token, report)
    finally:
        if own:
            client.close()
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", default=os.getenv("BASE_URL") or DEFAULT_SITE,
                        help=f"the site to crawl (default: $BASE_URL, else {DEFAULT_SITE})")
    parser.add_argument("--no-external", action="store_true", help="skip the openers' external links")
    args = parser.parse_args(argv)
    report = run(args.site, os.getenv("ADMIN_TOKEN", "").strip() or None, external=not args.no_external)
    print(render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
