"""Repository guards, run by CI before the tests (plan step 6.2c, F1 and F18).

For every push and pull request, over the commits it adds:

- each commit subject follows Conventional Commits (the pattern in
  .githooks/commit-msg, which is client-side and opt-in, so it never saw the
  bulk commits);
- no tracked file is over 10 MB;
- the tracked-file count grows by at most MAX_NEW_FILES, unless a commit in the
  range carries an ``Allow-Bulk-Add: <reason>`` trailer;
- no path the range adds, and no tracked path, looks like a student record
  (submissions/, resumes/, GradePage, gradebook, grading_spreadsheet) or the
  course source materials.

A bulk commit of student files, the accident these guard against, breaks four
of them at once (the subjects, the count, the size, and the paths).

    python scripts/repo_guard.py --event-path "$GITHUB_EVENT_PATH"   # CI
    python scripts/repo_guard.py --base origin/main --head HEAD        # locally

CI needs the range's commits and trees: tests.yml checks out full history
without blobs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "commit-msg"

MAX_FILE_BYTES = 10 * 1024 * 1024
# An ordinary addition stays well under this (a course's whole set of Teach-me
# openers is under a hundred files); a bulk import of student files runs to
# thousands.
MAX_NEW_FILES = 200
BULK_TRAILER = re.compile(r"^Allow-Bulk-Add:\s*\S", re.M)

# Case-insensitive; the directories match anywhere in the tree. The guard
# reports how many paths match each, never the paths: they can name students,
# and a CI log is readable by everyone with access to the repository.
RISKY = {
    "submissions/": re.compile(r"(^|/)submissions/", re.I),
    "resumes/": re.compile(r"(^|/)resumes/", re.I),
    "GradePage": re.compile(r"gradepage", re.I),
    "gradebook": re.compile(r"gradebook", re.I),
    "grading_spreadsheet": re.compile(r"grading_spreadsheet", re.I),
    "source-materials/": re.compile(r"(^|/)source-materials2?/", re.I),
}
_ZERO_SHA = re.compile(r"^0+$")


def _hook_pattern(hook: Path = HOOK) -> str:
    """The subject pattern the commit-msg hook enforces, so the two never drift."""
    m = re.search(r"^pattern='([^']+)'", hook.read_text(), re.M)
    if m is None:
        raise SystemExit(f"no pattern='...' line in {hook}")
    return m.group(1)


SUBJECT = re.compile(_hook_pattern())


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _exists(rev: str, cwd: Path) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"], cwd=cwd,
                          capture_output=True).returncode == 0


def _merge_base(a: str, b: str, cwd: Path) -> Optional[str]:
    r = subprocess.run(["git", "merge-base", a, b], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def resolve_range(event_name: str, event: dict, cwd: Path = ROOT) -> Optional[tuple[Optional[str], str]]:
    """``(base, head)`` for a GitHub event: the commits after ``base`` up to
    ``head`` are the ones it adds. ``base`` is None when nothing to compare
    against is known (only ``head`` itself is checked); None when the push
    deletes a branch."""
    if event_name.startswith("pull_request"):
        pr = event["pull_request"]
        head = pr["head"]["sha"]
        return _merge_base(pr["base"]["sha"], head, cwd), head
    head = event.get("after") or git("rev-parse", "HEAD", cwd=cwd).strip()
    if _ZERO_SHA.match(head):
        return None
    before = event.get("before") or ""
    if before and not _ZERO_SHA.match(before) and _exists(before, cwd):
        base = _merge_base(before, head, cwd)      # a force push rewrites; compare with what they share
        if base is not None:
            return base, head
    default = (event.get("repository") or {}).get("default_branch") or "main"
    if _exists(f"origin/{default}", cwd):
        return _merge_base(f"origin/{default}", head, cwd), head
    return None, head


@dataclass
class Commit:
    sha: str
    subject: str
    body: str


def commits(base: Optional[str], head: str, cwd: Path = ROOT) -> list[Commit]:
    spec = [f"{base}..{head}"] if base else ["-1", head]
    out = git("log", "--format=%H%x1f%s%x1f%B%x1e", *spec, cwd=cwd)
    rows = [r.strip("\n") for r in out.split("\x1e") if r.strip()]
    return [Commit(*r.split("\x1f", 2)) for r in rows]


def bad_subjects(found: list[Commit]) -> list[Commit]:
    """Commits whose subject is not a Conventional Commit. Merges, and the
    reverts git writes itself, are exempt, as in the hook."""
    return [c for c in found
            if not c.subject.startswith(("Merge ", 'Revert "')) and not SUBJECT.match(c.subject)]


def tracked(rev: str, cwd: Path = ROOT) -> list[str]:
    return git("ls-tree", "-r", "--name-only", "-z", rev, cwd=cwd).split("\0")[:-1]


def added(base: Optional[str], head: str, cwd: Path = ROOT) -> set[str]:
    """Every path a commit in the range adds, kept or not: history keeps it.
    A rename counts as adding its new path (and needs no file contents, which
    a blobless clone would otherwise fetch to score similarity)."""
    spec = [f"{base}..{head}"] if base else ["-1", head]
    out = git("log", "--format=", "--name-only", "--no-renames", "--diff-filter=A", "-z", *spec, cwd=cwd)
    return {p.strip("\n") for p in out.split("\0") if p.strip("\n")}


def category(path: str) -> Optional[str]:
    """The first RISKY pattern ``path`` matches, or None."""
    return next((name for name, rx in RISKY.items() if rx.search(path)), None)


def risky(paths) -> list[str]:
    return sorted(p for p in paths if category(p))


def large(head: str, limit: int = MAX_FILE_BYTES, cwd: Path = ROOT) -> list[tuple[str, int]]:
    """Tracked files at ``head`` over ``limit`` bytes."""
    out = git("ls-tree", "-r", "-l", "-z", head, cwd=cwd)
    found = []
    for entry in out.split("\0")[:-1]:
        meta, path = entry.split("\t", 1)
        size = meta.split()[3]
        if size != "-" and int(size) > limit:
            found.append((path, int(size)))
    return found


@dataclass
class Report:
    base: Optional[str]
    head: str
    checked: int = 0
    failures: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)   # never printed in CI


def check(base: Optional[str], head: str, *, cwd: Path = ROOT, max_new: int = MAX_NEW_FILES,
          max_bytes: int = MAX_FILE_BYTES) -> Report:
    report = Report(base, head)
    found = commits(base, head, cwd)
    report.checked = len(found)
    for c in bad_subjects(found):
        report.failures.append(f"{c.sha[:10]}: subject is not a Conventional Commit: {c.subject!r}")
    big = large(head, max_bytes, cwd)
    for path, size in big:
        if not category(path):
            report.failures.append(f"{path}: {size / 1048576:.1f} MB is over the {max_bytes / 1048576:g} MB limit")
    hidden_big = sum(1 for path, _ in big if category(path))
    if base is not None:
        grown = len(tracked(head, cwd)) - len(tracked(base, cwd))
        if grown > max_new and not any(BULK_TRAILER.search(c.body) for c in found):
            report.failures.append(
                f"the tracked-file count grows by {grown} (limit {max_new}); a deliberate bulk addition "
                "names its reason in an 'Allow-Bulk-Add: <reason>' trailer")
    report.flagged = risky(added(base, head, cwd) | set(tracked(head, cwd)))
    if report.flagged:
        counts: dict[str, int] = {}
        for path in report.flagged:
            key = category(path) or ""
            counts[key] = counts.get(key, 0) + 1
        breakdown = ", ".join(f"{counts[name]} {name}" for name in RISKY if name in counts)
        extra = f"; {hidden_big} of them over {max_bytes / 1048576:g} MB" if hidden_big else ""
        report.failures.append(
            f"{len(report.flagged)} path(s) look like student records or course source material "
            f"({breakdown}{extra}). The paths are not printed because they can name students: "
            "list them on your own machine with --show-paths")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event-path", help="the GitHub event JSON ($GITHUB_EVENT_PATH)")
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", "push"))
    parser.add_argument("--base", help="check the commits after this revision")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", type=Path, default=ROOT, help="the repository (default: this one)")
    parser.add_argument("--show-paths", action="store_true",
                        help="print the flagged paths (never in CI: they can name students)")
    args = parser.parse_args(argv)

    if args.event_path:
        rng = resolve_range(args.event_name, json.loads(Path(args.event_path).read_text()), args.repo)
        if rng is None:
            print("repo guard: a branch deletion, nothing to check")
            return 0
        base, head = rng
    else:
        base, head = args.base, args.head

    report = check(base, head, cwd=args.repo)
    span = f"{base[:10]}..{head[:10]}" if base else f"{head[:10]} alone"
    if not report.failures:
        print(f"repo guard: {report.checked} commit(s) in {span}: subjects, sizes, file count, and paths OK")
        return 0
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for failure in report.failures:
        print(f"::error::{failure}" if annotate else f"repo guard: {failure}")
    if args.show_paths and not annotate:
        for path in report.flagged:
            print(f"  {path}")
    print(f"repo guard: {len(report.failures)} problem(s) in {span}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
