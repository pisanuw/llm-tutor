"""Goal graph audit and edge-proposal tool (Session 12).

Two commands:

  python -m pipeline audit-goals [--propose]
      Analyse the prerequisite graph and (optionally) call Claude to propose
      new edges for orphan goals and missing cross-course connections: for
      each pair of courses, from the one listed earlier in courses.yaml to
      the one listed later.
      Writes curriculum/goal_edges_proposed.json.

  python -m pipeline apply-goals-diff [--dry-run]
      Applies the reviewed diff to learning_goals.json, bumps graph_version,
      and runs seed_goals.py to update the DB.

Design rules that match the rest of the pipeline:
  - Pure analysis functions take only data, return only data (no I/O).
  - LLM calls go through the local `claude` CLI, never the API key.
  - Nothing is auto-applied: the instructor always reviews the diff first.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from tutor.courses import COURSES, IDENTITY
from tutor.paths import CURRICULUM_DIR

from pipeline.generate._utils import course_title

ROOT = Path(__file__).parent.parent
GOALS_PATH = CURRICULUM_DIR / "learning_goals.json"
PROPOSED_PATH = CURRICULUM_DIR / "goal_edges_proposed.json"

console = Console()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    id: str
    title: str
    description: str
    course: str
    lecture: int


@dataclass
class Edge:
    from_id: str
    to_id: str
    rationale: str = ""


@dataclass
class AuditReport:
    goals: list[Goal]
    edges: list[Edge]
    in_deg: dict[str, int]
    out_deg: dict[str, int]
    orphans: list[Goal]          # in=0 AND out=0
    sources: list[Goal]          # in=0 (includes L01 intro goals)
    unreachable: list[Goal]      # not reachable from any source
    cross_course: list[Edge]     # edges between goals of two different courses
    same_lecture: list[Edge]     # edges within the same lecture (unusual)
    in_deg_dist: dict[int, int]
    out_deg_dist: dict[int, int]


# ---------------------------------------------------------------------------
# Pure analysis
# ---------------------------------------------------------------------------

def _load(path: Path) -> tuple[list[Goal], list[Edge]]:
    d = json.loads(path.read_text())
    goals = [Goal(**{k: v for k, v in g.items() if k in Goal.__dataclass_fields__})
             for g in d["goals"]]
    edges = [Edge(from_id=e["from"], to_id=e["to"]) for e in d["edges"]]
    return goals, edges


def _course_of(goal_id: str, course_of: dict[str, str]) -> str:
    """A goal's course: from its row, else its id's prefix (``AST101-L02-...``)."""
    return course_of.get(goal_id) or goal_id.split("-", 1)[0]


def build_report(goals: list[Goal], edges: list[Edge]) -> AuditReport:
    in_deg: dict[str, int] = defaultdict(int)
    out_deg: dict[str, int] = defaultdict(int)
    for e in edges:
        in_deg[e.to_id] += 1
        out_deg[e.from_id] += 1

    orphans = [g for g in goals if in_deg[g.id] == 0 and out_deg[g.id] == 0]
    sources = [g for g in goals if in_deg[g.id] == 0]

    # Reachability: BFS from all sources
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e.from_id].append(e.to_id)
    visited = {g.id for g in sources}
    queue = list(visited)
    while queue:
        node = queue.pop()
        for nbr in adj[node]:
            if nbr not in visited:
                visited.add(nbr)
                queue.append(nbr)
    unreachable = [g for g in goals if g.id not in visited]

    course_of = {g.id: g.course for g in goals}
    cross_course = [e for e in edges
                    if _course_of(e.from_id, course_of) != _course_of(e.to_id, course_of)]

    goal_lec_key = {g.id: (g.course, g.lecture) for g in goals}
    same_lecture = [e for e in edges
                    if goal_lec_key.get(e.from_id) == goal_lec_key.get(e.to_id)]

    in_dist: dict[int, int] = defaultdict(int)
    out_dist: dict[int, int] = defaultdict(int)
    for g in goals:
        in_dist[in_deg[g.id]] += 1
        out_dist[out_deg[g.id]] += 1

    return AuditReport(
        goals=goals, edges=edges,
        in_deg=dict(in_deg), out_deg=dict(out_deg),
        orphans=orphans, sources=sources, unreachable=unreachable,
        cross_course=cross_course, same_lecture=same_lecture,
        in_deg_dist=dict(in_dist), out_deg_dist=dict(out_dist),
    )


def check_exit_gates(report: AuditReport) -> list[str]:
    """Return a list of unmet exit gate conditions (empty = all green)."""
    failures = []

    if report.unreachable:
        failures.append(
            f"NOT ALL GOALS REACHABLE: {len(report.unreachable)} unreachable "
            f"({', '.join(g.id for g in report.unreachable[:5])}{'...' if len(report.unreachable) > 5 else ''})"
        )

    # Median in-degree >= 1 for non-introductory goals (lecture > 1)
    non_intro = [g for g in report.goals if g.lecture > 1]
    if non_intro:
        sorted_degs = sorted(report.in_deg.get(g.id, 0) for g in non_intro)
        mid = len(sorted_degs) // 2
        median_in = sorted_degs[mid] if len(sorted_degs) % 2 else (sorted_degs[mid - 1] + sorted_degs[mid]) / 2
        if median_in < 1:
            failures.append(
                f"MEDIAN IN-DEGREE FOR NON-INTRO GOALS IS {median_in:.1f} (need >= 1)"
            )

    return failures


# ---------------------------------------------------------------------------
# LLM edge-proposal pass
# ---------------------------------------------------------------------------

def _goals_compact(goals: list[Goal]) -> str:
    return "\n".join(
        f'  {g.id}: "{g.title}" — {g.description}'
        for g in goals
    )


def _edges_compact(edges: list[Edge]) -> str:
    return "\n".join(f"  {e.from_id} -> {e.to_id}" for e in edges)


def _call_claude(prompt: str, timeout: int = 300) -> str | None:
    """Haiku's reply to ``prompt``, or None on a failure (printed). Through the
    pipeline's one hardened CLI helper (plan step 6.8, finding E14): no tools,
    no API key in its environment. The prompt is built from LLM-written JSON,
    which is why tools stay off."""
    from pipeline.generate.script_generator import PLAIN_SYSTEM, _generate_via_cli

    try:
        return _generate_via_cli(PLAIN_SYSTEM, prompt, model="haiku", timeout=timeout).strip()
    except FileNotFoundError:
        console.print("[red]`claude` CLI not found: is it installed?[/red]")
        return None
    except (RuntimeError, OSError) as exc:
        console.print(f"[red]claude call failed: {escape(str(exc))}[/red]")
        return None


def _parse_proposed_edges(raw: str, valid_ids: set[str]) -> list[dict]:
    """Extract a list of edge dicts from Claude's response. Tolerant."""
    import re

    # Try to find a JSON array or object anywhere in the output
    for pattern in [
        r'\{[^{}]*"proposed_edges"\s*:\s*\[.*?\]\s*\}',
        r'\[.*?\]',
    ]:
        match = re.search(pattern, raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                edges = parsed.get("proposed_edges", parsed) if isinstance(parsed, dict) else parsed
                valid = []
                for e in edges:
                    if not isinstance(e, dict):
                        continue
                    f = e.get("from") or e.get("from_id")
                    t = e.get("to") or e.get("to_id")
                    r = e.get("rationale", "")
                    if f in valid_ids and t in valid_ids and f != t:
                        valid.append({"from": f, "to": t, "rationale": r})
                return valid
            except (json.JSONDecodeError, KeyError):
                continue
    return []


def _propose_intra_course(
    goals: list[Goal], edges: list[Edge], course: str
) -> list[dict]:
    """Ask Claude to propose intra-course edges for orphan goals in `course`."""
    course_goals = [g for g in goals if g.course == course]
    ids = {g.id for g in course_goals}
    course_edges = [e for e in edges if e.from_id in ids and e.to_id in ids]
    orphan_ids = {g.id for g in course_goals if
                  not any(e.to_id == g.id for e in course_edges) and
                  not any(e.from_id == g.id for e in course_edges)}
    orphans = [g for g in course_goals if g.id in orphan_ids]
    if not orphans:
        return []

    name = course_title(course)
    prompt = f"""You are building a prerequisite graph for {name}, a course in {IDENTITY.subject}.

All {name} learning goals (id: title — description):
{_goals_compact(course_goals)}

Existing prerequisite edges (from = prerequisite, to = depends-on-it):
{_edges_compact(course_edges) or '  (none)'}

These {len(orphans)} goals have no prerequisite edges at all and need to be connected:
{_goals_compact(orphans)}

Propose prerequisite edges for these orphan goals. Rules:
- Each edge means "you should master FROM before attempting TO"
- Only connect goals that have a real pedagogical dependency
- from and to must be goal ids listed above
- Do not create cycles (if A->B exists, do not propose B->A)
- Introductory goals in lecture 1 may remain as roots (no prerequisites)
- Return ONLY valid JSON, nothing else

{{"proposed_edges": [{{"from": "goal-id", "to": "goal-id", "rationale": "one sentence"}}]}}"""

    console.print(f"[dim]  Calling claude for {escape(name)} intra-course edges ({len(orphans)} orphans)...[/dim]")
    raw = _call_claude(prompt)
    if raw is None:
        return []
    return _parse_proposed_edges(raw, ids)


def course_pairs(courses=COURSES) -> list[tuple[str, str]]:
    """Every ordered pair of courses (earlier, later) in courses.yaml order:
    a later course may build on an earlier one, never the reverse."""
    courses = list(courses)
    return [(a, b) for i, a in enumerate(courses) for b in courses[i + 1:]]


def _propose_cross_course(goals: list[Goal], edges: list[Edge], earlier: str, later: str) -> list[dict]:
    """Ask Claude to propose prerequisite edges from ``earlier``'s goals to ``later``'s."""
    goals_a = [g for g in goals if g.course == earlier]
    goals_b = [g for g in goals if g.course == later]
    if not goals_a or not goals_b:
        return []
    ids_a = {g.id for g in goals_a}
    ids_b = {g.id for g in goals_b}
    existing_cc = [e for e in edges if e.from_id in ids_a and e.to_id in ids_b]
    name_a, name_b = course_title(earlier), course_title(later)

    prompt = f"""{name_a} comes before {name_b} (both are courses in {IDENTITY.subject}). You are building a prerequisite graph connecting them.

{name_a} learning goals:
{_goals_compact(goals_a)}

{name_b} learning goals:
{_goals_compact(goals_b)}

Existing cross-course edges ({earlier} -> {later}):
{_edges_compact(existing_cc) or '  (none)'}

Propose prerequisite edges from {name_a} goals to {name_b} goals where a real pedagogical dependency exists.
Focus on: {later} goals that directly build on specific {earlier} skills (not just "{earlier} in general").
A {later} goal that revisits an {earlier} topic at a deeper level should point to the {earlier} version.
If the two courses share no real dependency, propose no edges.

Rules:
- from must be a {earlier} goal id, to must be a {later} goal id
- Do not repeat edges that already exist above
- Return ONLY valid JSON, nothing else

{{"proposed_edges": [{{"from": "{earlier}-goal-id", "to": "{later}-goal-id", "rationale": "one sentence"}}]}}"""

    console.print(f"[dim]  Calling claude for cross-course {earlier}->{later} edges...[/dim]")
    raw = _call_claude(prompt)
    if raw is None:
        return []
    return [e for e in _parse_proposed_edges(raw, ids_a | ids_b)
            if e["from"] in ids_a and e["to"] in ids_b]


def propose_edges(goals: list[Goal], edges: list[Edge]) -> list[dict]:
    """Run the LLM passes (one per course, one per ordered pair of courses)
    and merge them into a single proposed-edges list."""
    proposed: list[dict] = []
    existing_pairs = {(e.from_id, e.to_id) for e in edges}

    for course in COURSES:
        result = _propose_intra_course(goals, edges, course)
        proposed.extend(result)
        console.print(f"  [green]{len(result)}[/green] intra-course edges proposed for {escape(course_title(course))}")

    for earlier, later in course_pairs():
        cc = _propose_cross_course(goals, edges, earlier, later)
        proposed.extend(cc)
        console.print(f"  [green]{len(cc)}[/green] cross-course {earlier}->{later} edges proposed")

    # De-duplicate and filter out edges that already exist
    seen: set[tuple[str, str]] = set()
    deduped = []
    for e in proposed:
        key = (e["from"], e["to"])
        if key not in seen and key not in existing_pairs:
            seen.add(key)
            deduped.append(e)
    return deduped


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------

@click.command("audit-goals")
@click.option("--goals", "goals_path", default=str(GOALS_PATH), show_default=True,
              help="Path to learning_goals.json")
@click.option("--output", "output_path", default=str(PROPOSED_PATH), show_default=True,
              help="Where to write the proposed-edges JSON")
@click.option("--propose", is_flag=True, default=False,
              help="Run the LLM pass to propose new edges (requires claude CLI)")
def audit_goals_cmd(goals_path: str, output_path: str, propose: bool):
    """Audit the prerequisite graph and optionally propose new edges."""
    gpath = Path(goals_path)
    opath = Path(output_path)

    console.print(f"\n[bold]Goal graph audit[/bold] — {gpath.name}\n")

    goals, edges = _load(gpath)
    report = build_report(goals, edges)

    # Summary table
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("Total goals", str(len(goals)))
    t.add_row("Total edges", str(len(edges)))
    t.add_row("Orphan goals (in=0, out=0)", f"[{'red' if report.orphans else 'green'}]{len(report.orphans)}[/]")
    t.add_row("Sources (in=0)", str(len(report.sources)))
    t.add_row("Unreachable goals", f"[{'red' if report.unreachable else 'green'}]{len(report.unreachable)}[/]")
    t.add_row("Cross-course edges", str(len(report.cross_course)))
    t.add_row("Same-lecture edges", str(len(report.same_lecture)))
    console.print(t)

    console.print("\n[bold]In-degree distribution:[/bold]")
    for deg, count in sorted(report.in_deg_dist.items()):
        bar = "█" * count
        console.print(f"  H{deg:2d}: {count:3d}  {bar}")

    if report.orphans:
        console.print(f"\n[bold red]Orphan goals ({len(report.orphans)}):[/bold red]")
        for g in report.orphans:
            console.print(f"  {g.id}  ({g.course} L{g.lecture})")

    if report.unreachable:
        console.print(f"\n[bold red]Unreachable goals ({len(report.unreachable)}):[/bold red]")
        for g in report.unreachable[:20]:
            console.print(f"  {g.id}")
        if len(report.unreachable) > 20:
            console.print(f"  ... and {len(report.unreachable) - 20} more")

    # Exit gate check
    failures = check_exit_gates(report)
    console.print("\n[bold]Exit gate status:[/bold]")
    if not failures:
        console.print("  [green]All gates pass.[/green]")
    else:
        for f in failures:
            console.print(f"  [red]FAIL:[/red] {f}")

    if not propose:
        console.print(
            "\n[dim]Run with --propose to call Claude and generate "
            f"{opath.name}.[/dim]\n"
        )
        return

    # LLM pass
    console.print("\n[bold]Proposing new edges via Claude...[/bold]")
    proposed = propose_edges(goals, edges)
    console.print(f"\n[bold green]{len(proposed)} unique new edges proposed.[/bold green]")

    out = {
        "note": (
            "Proposed edges from pipeline/audit_goals.py LLM pass. "
            "Review, edit, then run: python -m pipeline apply-goals-diff"
        ),
        "proposed_edges": proposed,
    }
    opath.write_text(json.dumps(out, indent=2))
    console.print(f"Written to [bold]{opath}[/bold]\n")


@click.command("apply-goals-diff")
@click.option("--diff", "diff_path", default=str(PROPOSED_PATH), show_default=True,
              help="Path to goal_edges_proposed.json")
@click.option("--goals", "goals_path", default=str(GOALS_PATH), show_default=True,
              help="Path to learning_goals.json")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would change without writing anything")
@click.option("--reseed", is_flag=True, default=False,
              help="Run seed_goals.py after applying (requires a working DB)")
@click.option("--allow-prod", is_flag=True, default=False,
              help="Let --reseed write to a non-SQLite (production) database")
def apply_goals_diff_cmd(diff_path: str, goals_path: str, dry_run: bool, reseed: bool,
                         allow_prod: bool):
    """Apply the reviewed edge diff to learning_goals.json and bump graph_version."""
    gpath = Path(goals_path)
    dpath = Path(diff_path)

    if not dpath.exists():
        console.print(f"[red]Diff file not found: {dpath}[/red]")
        sys.exit(1)

    data = json.loads(gpath.read_text())
    diff = json.loads(dpath.read_text())

    proposed = diff.get("proposed_edges", [])
    goals = data["goals"]
    existing_edges = data.get("edges", [])
    valid_ids = {g["id"] for g in goals}
    existing_pairs = {(e["from"], e["to"]) for e in existing_edges}

    # Validate
    bad = [e for e in proposed if e["from"] not in valid_ids or e["to"] not in valid_ids]
    if bad:
        console.print(f"[red]{len(bad)} edges reference unknown goal IDs — fix the diff first:[/red]")
        for e in bad[:5]:
            console.print(f"  {e['from']} -> {e['to']}")
        sys.exit(1)

    new_edges = [e for e in proposed if (e["from"], e["to"]) not in existing_pairs]
    skipped = len(proposed) - len(new_edges)

    console.print(f"\nProposed: {len(proposed)}  New: {len(new_edges)}  Skipped (already exist): {skipped}")

    if not new_edges:
        console.print("[yellow]Nothing to apply.[/yellow]")
        return

    if dry_run:
        console.print("\n[bold]Dry run — edges that would be added:[/bold]")
        for e in new_edges:
            r = e.get("rationale", "")
            console.print(f"  {e['from']} -> {e['to']}  [{r}]")
        return

    # Apply
    for e in new_edges:
        existing_edges.append({"from": e["from"], "to": e["to"]})

    version = int(data.get("graph_version", 0)) + 1
    data["edges"] = existing_edges
    data["graph_version"] = version

    gpath.write_text(json.dumps(data, indent=2))
    console.print(
        f"[green]Applied {len(new_edges)} edges. "
        f"graph_version bumped to {version}.[/green]"
    )
    # The tracks are ordered against the graph (plan step 5.8, B25).
    console.print("Next: [bold]python -m pipeline order-tracks[/bold] re-orders the tracks "
                  "against the new edges and records the version.")

    # Re-run audit to show gate status
    goals_obj, edges_obj = _load(gpath)
    report = build_report(goals_obj, edges_obj)
    failures = check_exit_gates(report)
    console.print("\n[bold]Exit gate status after apply:[/bold]")
    if not failures:
        console.print("  [green]All gates pass.[/green]")
    else:
        for f in failures:
            console.print(f"  [red]FAIL:[/red] {f}")

    if reseed:
        console.print("\n[bold]Reseeding DB...[/bold]")
        result = subprocess.run(
            [sys.executable, str(ROOT / "pipeline" / "seed_goals.py"), "--json", str(gpath)]
            + (["--allow-prod"] if allow_prod else []),
            capture_output=False,
        )
        if result.returncode != 0:
            console.print("[red]seed_goals.py failed[/red]")
            sys.exit(1)
        console.print("[green]DB reseeded.[/green]")
