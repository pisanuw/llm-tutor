"""Order each track so no goal comes before a prerequisite from its own
lecture (plan step 5.8, finding B25).

A course's lecture-order track used to sort each lecture's goals
alphabetically, which put goals ahead of a prerequisite taught in the same
lecture (the other tracks had some too). Within each lecture, the goals now
follow their prerequisites and otherwise keep the track's order (a stable
topological sort: of the goals whose prerequisites are placed, the one that
came first goes next). Each lecture keeps the slots it had in the track, so
a track's order across lectures, which is its design (a track may start
with goals from later lectures), does not change; the navigator
never offers a goal before its prerequisites anyway. tracks.json records the
graph_version of learning_goals.json it was ordered against.

    python -m pipeline order-tracks            # rewrite curriculum/tracks.json
    python -m pipeline order-tracks --check    # exit 1 when a track is out of order
"""

from __future__ import annotations

import heapq
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from tutor.paths import CURRICULUM_DIR

ROOT = Path(__file__).resolve().parent.parent
GOALS_PATH = CURRICULUM_DIR / "learning_goals.json"
TRACKS_PATH = CURRICULUM_DIR / "tracks.json"

console = Console()


def lecture_prereqs(goals_data: dict) -> dict[str, list[str]]:
    """``{goal: [prerequisite, ...]}`` for the edges within one lecture of
    one course (lecture numbers repeat across courses)."""
    lecture = {g["id"]: (g["course"], g["lecture"]) for g in goals_data["goals"]}
    out: dict[str, list[str]] = {}
    for e in goals_data["edges"]:
        if e["from"] in lecture and lecture.get(e["from"]) == lecture.get(e["to"]):
            out.setdefault(e["to"], []).append(e["from"])
    return out


def _stable_topo(goal_ids: list[str], prereqs: dict[str, list[str]]) -> list[str]:
    """``goal_ids`` with every goal after its prerequisites (those in the
    list), otherwise in the given order. Raises ValueError on a cycle."""
    pos = {g: i for i, g in enumerate(goal_ids)}
    waiting = {g: {p for p in prereqs.get(g, []) if p in pos} for g in goal_ids}
    dependents: dict[str, list[str]] = {}
    for g, ps in waiting.items():
        for p in ps:
            dependents.setdefault(p, []).append(g)
    ready = [pos[g] for g, ps in waiting.items() if not ps]
    heapq.heapify(ready)
    out: list[str] = []
    while ready:
        g = goal_ids[heapq.heappop(ready)]
        out.append(g)
        for d in dependents.get(g, []):
            waiting[d].discard(g)
            if not waiting[d]:
                heapq.heappush(ready, pos[d])
    if len(out) != len(goal_ids):
        stuck = sorted(set(goal_ids) - set(out))
        raise ValueError(f"the prerequisite graph has a cycle among {', '.join(stuck)}")
    return out


def order_track(goal_ids: list[str], prereqs: dict[str, list[str]], lecture_of: dict[str, object]) -> list[str]:
    """Each lecture's goals, sorted by ``_stable_topo`` over ``prereqs``
    (same-lecture edges), back in the slots that lecture had in the track."""
    groups: dict[object, list[str]] = {}
    for g in goal_ids:
        groups.setdefault(lecture_of[g], []).append(g)
    sorted_groups = {lec: iter(_stable_topo(gs, prereqs)) for lec, gs in groups.items()}
    return [next(sorted_groups[lecture_of[g]]) for g in goal_ids]


def violations(goal_ids: list[str], prereqs: dict[str, list[str]]) -> list[tuple[str, str]]:
    """``(prerequisite, goal)`` pairs where the goal comes first."""
    pos = {g: i for i, g in enumerate(goal_ids)}
    return [(p, g) for g in goal_ids for p in prereqs.get(g, []) if p in pos and pos[p] > pos[g]]


def ordered(goals_data: dict, tracks_data: dict) -> dict:
    """tracks.json's content with every track ordered and the graph version
    it was ordered against."""
    prereqs = lecture_prereqs(goals_data)
    lecture_of = {g["id"]: (g["course"], g["lecture"]) for g in goals_data["goals"]}
    return {
        "tracks": [{**t, "goal_ids": order_track(t["goal_ids"], prereqs, lecture_of)}
                   for t in tracks_data["tracks"]],
        "graph_version": goals_data.get("graph_version", 0),
    }


@click.command("order-tracks")
@click.option("--check", is_flag=True, help="Report tracks out of order and exit 1; write nothing")
def order_tracks_cmd(check: bool) -> None:
    """Order every track in curriculum/tracks.json after its prerequisites."""
    goals_data = json.loads(GOALS_PATH.read_text())
    tracks_data = json.loads(TRACKS_PATH.read_text())
    prereqs = lecture_prereqs(goals_data)
    stale = tracks_data.get("graph_version") != goals_data.get("graph_version", 0)
    bad = {t["id"]: violations(t["goal_ids"], prereqs) for t in tracks_data["tracks"]}
    bad = {tid: v for tid, v in bad.items() if v}
    for tid, v in bad.items():
        console.print(f"  {tid}: {len(v)} goal(s) ahead of a same-lecture prerequisite, "
                      f"e.g. {v[0][1]} before {v[0][0]}")
    if stale:
        console.print(f"  tracks.json graph_version {tracks_data.get('graph_version')} != "
                      f"learning_goals.json {goals_data.get('graph_version', 0)}")
    if check:
        if bad or stale:
            sys.exit(1)
        console.print("[green]Every track follows its same-lecture prerequisites.[/green]")
        return
    new = ordered(goals_data, tracks_data)
    if new == tracks_data:
        console.print("[green]Tracks already in order; nothing written.[/green]")
        return
    TRACKS_PATH.write_text(json.dumps(new, indent=2))
    console.print(f"[green]Wrote {TRACKS_PATH} (graph_version {new['graph_version']}).[/green]")
