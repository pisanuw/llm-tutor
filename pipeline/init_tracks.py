"""Write each course's default track: ``python -m pipeline init-tracks``.

A track is an ordered list of a course's learning goals; the navigator
suggests the next goal from the student's track, never before its
prerequisites. Every course needs ``<course>-lecture-order`` (the default a
student starts on, tutor/agent/navigator.py). This command writes that track
for each course from curriculum/learning_goals.json: goals in lecture order,
and within a lecture after their prerequisites, otherwise in file order.

Tracks already in tracks.json are kept as they are (pass --force to rewrite
the lecture-order ones too); add other tracks ("Big Ideas First", "Exam
Prep") by hand, then run ``python -m pipeline order-tracks --check``.
"""

from __future__ import annotations

import json

import click
from rich.console import Console

from tutor.courses import COURSES
from tutor.paths import CURRICULUM_DIR

console = Console()
GOALS_PATH = CURRICULUM_DIR / "learning_goals.json"
TRACKS_PATH = CURRICULUM_DIR / "tracks.json"


def lecture_order(goals_data: dict, course: str) -> list[str]:
    """The course's goals by lecture, each after its same-lecture prerequisites."""
    from pipeline.order_tracks import _stable_topo, lecture_prereqs

    goals = [g for g in goals_data["goals"] if g["course"] == course]
    prereqs = lecture_prereqs(goals_data)
    out: list[str] = []
    for lec in sorted({int(g["lecture"]) for g in goals}):
        ids = [g["id"] for g in goals if int(g["lecture"]) == lec]
        out += _stable_topo(ids, prereqs)
    return out


def build(goals_data: dict, tracks_data: dict, force: bool = False) -> tuple[dict, list[str]]:
    """tracks.json's new content and the ids written."""
    tracks = list(tracks_data.get("tracks", []))
    by_id = {t["id"]: i for i, t in enumerate(tracks)}
    written = []
    for course in COURSES:
        tid = f"{course}-lecture-order"
        if tid in by_id and not force:
            continue
        track = {"id": tid, "course": course, "title": "Lecture Order",
                 "description": "Goals in the order the lectures teach them.",
                 "goal_ids": lecture_order(goals_data, course)}
        if tid in by_id:
            tracks[by_id[tid]] = track
        else:
            tracks.append(track)
        written.append(tid)
    return {"tracks": tracks, "graph_version": goals_data.get("graph_version", 0)}, written


@click.command("init-tracks")
@click.option("--force", is_flag=True, help="Rewrite existing <course>-lecture-order tracks too")
def init_tracks_cmd(force: bool) -> None:
    """Write a <course>-lecture-order track for every course in courses.yaml."""
    goals_data = json.loads(GOALS_PATH.read_text())
    tracks_data = json.loads(TRACKS_PATH.read_text()) if TRACKS_PATH.exists() else {"tracks": []}
    out, written = build(goals_data, tracks_data, force)
    TRACKS_PATH.write_text(json.dumps(out, indent=2) + "\n")
    if written:
        console.print(f"[green]Wrote {', '.join(written)} to {TRACKS_PATH}[/green]")
    else:
        console.print("Every course already has a lecture-order track (use --force to rewrite).")
