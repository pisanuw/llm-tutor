"""Check the course content an instructor wrote: ``python -m pipeline check-content``.

Every file the tutor reads to know your course is checked against what the
app and the pipeline expect, and every problem is reported at once, with the
file and the item it is about:

* courses.yaml loads and names at least one course (tutor/courses.py);
* each course's lecture plan exists and has lectures 1..N;
* learning_goals.json: ids shaped ``<course>-L<NN>-<slug>``, known courses,
  lectures in range, no duplicates, edges between real goals, no cycles;
* tracks.json: each course has its default ``<course>-lecture-order`` track,
  tracks name only their own course's goals, and none puts a goal before a
  prerequisite from the same course;
* exercises.json, exercise_tests.json, starter_prompts.json,
  source-lecture-map.json, curriculum/teachme/, pedagogy_moves.yaml;
* persona/, instructor-profile/ and explorables/;
* what is generated and not checked in (decks, the knowledge base, the
  mastery exam bank), reported as notes.

An ERROR stops something from working (the app may refuse to start, a page
may break, a goal can never be reached); the command then exits 1. A WARNING
is worth fixing but the tutor still runs. A NOTE says what is not there yet.
It reads files only: no model call, no database, no network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.console import Console

console = Console()

GOAL_ID = re.compile(r"^(?P<course>[A-Za-z0-9]{1,8})-L(?P<lecture>\d{2})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)$")
DIFFICULTIES = {"concept", "trace", "code", "debug"}
DEPTHS = {"brief", "standard", "thorough"}
PERSONA_FILES = ("persona-distilled.md", "communication.md", "teaching.md")
PROFILE_FILES = ("transcripts-voice.md", "lecture-flow.md", "teaching-patterns.md",
                 "examples-library.md", "topic-explanations.md")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def _load_json(path: Path, report: Report, required: bool = True):
    if not path.exists():
        (report.error if required else report.note)(f"{path.name}: not found ({path})")
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        report.error(f"{path.name}: not valid JSON ({exc})")
        return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def check(root: Path | None = None) -> Report:
    """Run every check against the content root (tutor/paths.py) and return
    the report. ``root`` is for tests; it must match TUTOR_CONTENT_DIR."""
    report = Report()
    try:
        from tutor import courses as courses_mod
        from tutor import paths
    except Exception as exc:  # CourseConfigError, YAML errors: report and stop
        report.error(f"courses.yaml: {exc}")
        return report

    content = root or paths.CONTENT_DIR
    curriculum = content / "curriculum"
    lectures = {c.id: c.lectures for c in courses_mod.COURSE_LIST}
    courses = set(lectures)

    # ---- lecture plans ----------------------------------------------------
    heading = re.compile(r"^## Lecture (\d+):\s*(.+?)\s*$", re.MULTILINE)
    for course, n in lectures.items():
        plan = curriculum / f"{course}-lecture-plan.md"
        if not plan.exists():
            report.error(f"{_rel(plan, content)}: missing (step 4 of ADDING-KNOWLEDGE.md)")
            continue
        text = plan.read_text(errors="ignore")
        nums = [int(x) for x, _ in heading.findall(text)]
        if nums != list(range(1, n + 1)):
            report.error(f"{_rel(plan, content)}: lecture headings are {nums or 'none'}; courses.yaml "
                         f"says {n} lectures, so expected '## Lecture 01: ...' through '## Lecture {n:02d}: ...'")
        for num, body in zip(nums, re.split(r"\n## Lecture \d+:", text)[1:]):
            if "**Learning Objectives:**" not in body:
                report.warn(f"{_rel(plan, content)}: lecture {num:02d} has no **Learning Objectives:** "
                            "list (extract-goals reads it)")

    # ---- learning goals -----------------------------------------------------
    goals_data = _load_json(curriculum / "learning_goals.json", report)
    goals: dict[str, dict] = {}
    edges: list[tuple[str, str]] = []
    if isinstance(goals_data, dict):
        for i, g in enumerate(goals_data.get("goals") or []):
            gid = str(g.get("id", ""))
            where = f"learning_goals.json goal {gid or f'#{i}'}"
            m = GOAL_ID.match(gid)
            if not m:
                report.error(f"{where}: id must look like <course>-L<NN>-<slug> (e.g. "
                             f"{next(iter(courses))}-L01-some-skill)")
                continue
            if gid in goals:
                report.error(f"{where}: duplicate id")
            for key in ("title", "description", "course", "lecture"):
                if g.get(key) in (None, ""):
                    report.error(f"{where}: missing '{key}'")
            course = str(g.get("course", ""))
            if course not in courses:
                report.error(f"{where}: course {course!r} is not in courses.yaml")
            elif m.group("course") != course:
                report.error(f"{where}: id prefix {m.group('course')!r} does not match course {course!r}")
            try:
                lec = int(g.get("lecture", 0))
            except (TypeError, ValueError):
                lec = 0
            if course in lectures and not 1 <= lec <= lectures[course]:
                report.error(f"{where}: lecture {g.get('lecture')} is outside 1..{lectures[course]}")
            elif int(m.group("lecture")) != lec:
                report.error(f"{where}: the L{m.group('lecture')} in the id does not match lecture {lec}")
            if len(str(g.get("title", ""))) > 80:
                report.warn(f"{where}: title is over 80 characters (it is shown on the map and in chips)")
            anchor = g.get("slide_anchor")
            if isinstance(anchor, dict) and anchor.get("lecture") not in (None, lec):
                report.warn(f"{where}: slide_anchor.lecture {anchor.get('lecture')} differs from the goal's lecture")
            if not g.get("hook"):
                report.note(f"{where}: no 'hook' (the one-line why-this-matters shown with the goal)")
            goals[gid] = g
        for e in goals_data.get("edges") or []:
            if not isinstance(e, dict):
                report.error(f"learning_goals.json edge {e!r}: must be {{\"from\": ..., \"to\": ...}}")
                continue
            a, b = str(e.get("from", "")), str(e.get("to", ""))
            if a not in goals or b not in goals:
                report.error(f"learning_goals.json edge {a} -> {b}: names a goal that does not exist")
                continue
            if a == b:
                report.error(f"learning_goals.json edge {a} -> {b}: a goal cannot be its own prerequisite")
                continue
            edges.append((a, b))
        cycle = _find_cycle(goals, edges)
        if cycle:
            report.error("learning_goals.json: prerequisite cycle " + " -> ".join(cycle))
        for course in courses:
            if not any(g["course"] == course for g in goals.values()):
                report.error(f"learning_goals.json: no goals for {course} (run python -m pipeline extract-goals)")
    elif goals_data is not None:
        report.error("learning_goals.json: must be an object with 'goals' and 'edges'")

    prereqs: dict[str, set[str]] = {}
    for a, b in edges:
        prereqs.setdefault(b, set()).add(a)

    # ---- tracks ---------------------------------------------------------------
    tracks_data = _load_json(curriculum / "tracks.json", report)
    track_ids: dict[str, str] = {}
    if isinstance(tracks_data, dict):
        for t in tracks_data.get("tracks") or []:
            tid, course = str(t.get("id", "")), str(t.get("course", ""))
            where = f"tracks.json track {tid}"
            if course not in courses:
                report.error(f"{where}: course {course!r} is not in courses.yaml")
                continue
            if len(tid) > 64:
                report.error(f"{where}: id longer than 64 characters")
            track_ids[tid] = course
            ids = list(t.get("goal_ids") or [])
            unknown = [g for g in ids if g not in goals]
            if unknown:
                report.error(f"{where}: unknown goal ids {unknown[:5]}")
            other = [g for g in ids if g in goals and goals[g]["course"] != course]
            if other:
                report.error(f"{where}: goals from another course {other[:5]}")
            missing = [g for g, v in goals.items() if v["course"] == course and g not in ids]
            if missing:
                report.warn(f"{where}: leaves out {len(missing)} of the course's goals, e.g. {missing[:3]}")
            seen: set[str] = set()
            for g in ids:
                early = [p for p in prereqs.get(g, ()) if p in ids and p not in seen
                         and goals.get(p, {}).get("course") == course]
                if early:
                    report.warn(f"{where}: {g} comes before its prerequisite {early[0]} "
                                "(python -m pipeline order-tracks fixes this within a lecture)")
                seen.add(g)
        for course in courses:
            if f"{course}-lecture-order" not in track_ids:
                report.error(f"tracks.json: no '{course}-lecture-order' track; it is every student's "
                             "default (python -m pipeline init-tracks writes one)")

    # ---- exercises ------------------------------------------------------------
    ex_data = _load_json(curriculum / "exercises.json", report, required=False)
    exercise_ids: set[str] = set()
    if isinstance(ex_data, dict):
        covered: set[str] = set()
        for e in ex_data.get("exercises") or []:
            eid = str(e.get("id", ""))
            where = f"exercises.json {eid or '(no id)'}"
            if not eid:
                report.error(f"{where}: missing id")
                continue
            if eid in exercise_ids:
                report.error(f"{where}: duplicate id")
            exercise_ids.add(eid)
            for key in ("course", "lecture_num", "topic", "difficulty", "prompt"):
                if e.get(key) in (None, ""):
                    report.error(f"{where}: missing '{key}' (the exercise is skipped at runtime)")
            if e.get("course") not in courses:
                report.error(f"{where}: course {e.get('course')!r} is not in courses.yaml")
            if e.get("difficulty") not in DIFFICULTIES:
                report.error(f"{where}: difficulty must be one of {sorted(DIFFICULTIES)}")
            for g in e.get("goal_ids") or []:
                if g not in goals:
                    report.error(f"{where}: unknown goal id {g}")
                covered.add(g)
        uncovered = [g for g in goals if g not in covered]
        if uncovered:
            report.warn(f"exercises.json: {len(uncovered)} goal(s) have no exercise, e.g. {uncovered[:3]} "
                        "(python -m pipeline build-exercises drafts them)")
    tests_data = _load_json(curriculum / "exercise_tests.json", report, required=False)
    if isinstance(tests_data, dict):
        for key, spec in tests_data.items():
            if key.startswith("_"):
                continue
            if key not in exercise_ids:
                report.error(f"exercise_tests.json {key}: no exercise with that id")
            if not isinstance(spec, dict) or not isinstance(spec.get("tests"), list):
                report.error(f"exercise_tests.json {key}: needs a 'tests' list")

    # ---- starter prompts, source map, teach-me ---------------------------------
    starters = _load_json(curriculum / "starter_prompts.json", report, required=False)
    if isinstance(starters, dict):
        for key, items in starters.items():
            if key.startswith("_"):
                continue
            if key != "meta" and key not in courses:
                report.error(f"starter_prompts.json: key {key!r} is neither 'meta' nor a course id")
                continue
            for item in items or []:
                if not item.get("label") or not item.get("message"):
                    report.error(f"starter_prompts.json {key}: every chip needs a 'label' and a 'message'")
    smap = _load_json(curriculum / "source-lecture-map.json", report, required=False)
    if isinstance(smap, dict):
        for course, spec in smap.items():
            if course.startswith("_"):
                continue
            if course not in courses:
                report.error(f"source-lecture-map.json: {course!r} is not a course in courses.yaml")
                continue
            for src, lec in (spec.get("map") or {}).items():
                if not isinstance(lec, int) or not 1 <= lec <= lectures[course]:
                    report.error(f"source-lecture-map.json {course} {src}: lecture {lec} is outside "
                                 f"1..{lectures[course]}")
    teachme = curriculum / "teachme"
    if teachme.is_dir():
        for path in sorted(teachme.rglob("*.json")):
            data = _load_json(path, report)
            if not isinstance(data, dict):
                continue
            gid = data.get("goal_id")
            where = f"teachme/{_rel(path, teachme)}"
            if gid not in goals:
                report.error(f"{where}: goal_id {gid!r} is not in learning_goals.json")
            if path.stem != gid:
                report.error(f"{where}: the file must be named after its goal_id ({gid}.json)")
            for tid, by_depth in (data.get("variants") or {}).items():
                if tid not in track_ids:
                    report.warn(f"{where}: variant for unknown track {tid!r}")
                bad = set(by_depth or {}) - DEPTHS
                if bad:
                    report.error(f"{where}: unknown depth(s) {sorted(bad)}; use {sorted(DEPTHS)}")

    # ---- pedagogy playbook -----------------------------------------------------
    playbook = curriculum / "pedagogy_moves.yaml"
    if playbook.exists():
        try:
            import yaml

            from tutor.agent.playbook import _parse

            _parse(yaml.safe_load(playbook.read_text()))
        except Exception as exc:
            report.error(f"pedagogy_moves.yaml: {exc} (the tutor falls back to built-in prompts)")
    else:
        report.warn("pedagogy_moves.yaml: missing (the tutor falls back to built-in prompts)")

    # ---- persona, profile, explorables -----------------------------------------
    for folder, names, why in (("persona", PERSONA_FILES, "who the tutor is"),
                               ("instructor-profile", PROFILE_FILES, "how the instructor teaches")):
        for name in names:
            path = content / folder / name
            if not path.exists():
                report.warn(f"{folder}/{name}: missing; the system prompt says so instead of {why}")
            elif not path.read_text(errors="ignore").strip():
                report.warn(f"{folder}/{name}: empty")
    explorables = content / "explorables"
    if explorables.is_dir():
        for path in sorted(explorables.glob("*.html")):
            if path.stem not in goals:
                report.warn(f"explorables/{path.name}: no goal with id {path.stem}, so nothing links to it")
            elif not goals[path.stem].get("threshold"):
                report.note(f"explorables/{path.name}: goal is not marked \"threshold\": true; the map "
                            "links explorables only for threshold goals")
            if re.search(r"""(src|href)\s*=\s*["']https?://""", path.read_text(errors="ignore")):
                report.error(f"explorables/{path.name}: loads something from the network; inline it")

    # ---- generated artifacts (notes only) --------------------------------------
    from tutor.paths import ROOT

    for course, n in lectures.items():
        decks = [k for k in range(1, n + 1)
                 if (ROOT / "output" / course / f"lecture-{k:02d}" / "index.html").is_file()]
        if len(decks) < n:
            report.note(f"output/{course}: {len(decks)} of {n} decks rendered "
                        f"(python -m pipeline render-html --course {course})")
    if not (ROOT / "knowledge-base" / "chromadb" / "chroma.sqlite3").is_file():
        report.note("knowledge-base/chromadb: no index yet; the tutor answers without retrieval "
                    "(python -m pipeline index --rebuild)")
    if not (curriculum / "mastery_exams.json").is_file():
        report.note("curriculum/mastery_exams.json: no exam bank; mastery checks are off "
                    "(python -m pipeline generate-mastery-exams)")
    return report


def _find_cycle(goals: dict, edges: list[tuple[str, str]]) -> list[str]:
    """One prerequisite cycle as a list of ids (first repeated at the end), or []."""
    out: dict[str, list[str]] = {}
    for a, b in edges:
        out.setdefault(a, []).append(b)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        stack.append(node)
        for nxt in out.get(node, []):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if not state.get(nxt):
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return []

    for g in goals:
        if not state.get(g):
            found = visit(g)
            if found:
                return found
    return []


def print_report(report: Report) -> None:
    for msg in report.errors:
        console.print(f"[red]ERROR[/red]   {msg}", markup=True, soft_wrap=True, highlight=False)
    for msg in report.warnings:
        console.print(f"[yellow]WARNING[/yellow] {msg}", soft_wrap=True, highlight=False)
    for msg in report.notes:
        console.print(f"[dim]NOTE[/dim]    {msg}", soft_wrap=True, highlight=False)
    summary = f"{len(report.errors)} error(s), {len(report.warnings)} warning(s), {len(report.notes)} note(s)"
    console.print(("[red]" if report.errors else "[green]") + summary + "[/]")


@click.command("check-content")
@click.option("--quiet", is_flag=True, help="Errors and warnings only, no notes")
def check_content_cmd(quiet: bool) -> None:
    """Check courses.yaml, curriculum/, persona/ and the rest of your content."""
    report = check()
    if quiet:
        report.notes.clear()
    print_report(report)
    if report.errors:
        raise SystemExit(1)
