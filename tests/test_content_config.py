"""The content an instructor writes: courses.yaml, the content checker, init-tracks.

Network-free. The suite runs against tests/fixtures/content (conftest sets
TUTOR_CONTENT_DIR); these tests also build small broken copies in tmp_path to
prove each check names the problem.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tutor import courses
from tutor.paths import CONTENT_DIR

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "content"


def test_the_suite_reads_the_fixture_not_the_repository_content():
    assert CONTENT_DIR == FIXTURE.resolve()
    assert courses.COURSES == ("AST101", "AST102")
    assert courses.DEFAULT_COURSE == "AST101"


def test_identity_and_course_helpers():
    assert courses.IDENTITY.name == "Astra"
    assert courses.IDENTITY.instructor_short == "Dr. Morgan"
    assert courses.course_label("AST101") == "AST 101"
    assert courses.course_label("NOPE") == "NOPE"
    assert courses.lecture_numbers("AST102") == [1, 2, 3, 4]
    assert courses.lecture_numbers("NOPE") == []
    assert courses.get_course("AST101").full_title == "AST 101: The Night Sky"
    assert "introductory astronomy" in courses.course_context()
    assert courses.course_color("AST101") != courses.course_color("AST102")


@pytest.mark.parametrize("text,expected", [
    ("AST101", "AST101"), ("ast101", "AST101"), ("AST 102", "AST102"), (" ast 102 ", "AST102"),
    ("", None), (None, None), ("AST103", None), ("../AST101", None),
])
def test_match_course(text, expected):
    assert courses.match_course(text) == expected


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "courses.yaml"
    path.write_text(body)
    return path


@pytest.mark.parametrize("body,needle", [
    ("tutor: {name: A, instructor: B, subject: c}\n", "courses: must be a list"),
    ("tutor: {name: A, instructor: B, subject: c}\ncourses: [{id: 'AST-101', lectures: 3}]\n", "1 to 8 letters or digits"),
    ("tutor: {name: A, instructor: B, subject: c}\ncourses: [{id: X1, lectures: 0}]\n", "between 1 and 99"),
    ("tutor: {name: A, instructor: B, subject: c}\ncourses: [{id: X1, lectures: 2}, {id: X1, lectures: 2}]\n", "appears twice"),
    ("tutor: {name: A, subject: c}\ncourses: [{id: X1, lectures: 2}]\n", "tutor.instructor is required"),
    ("tutor: {name: A, instructor: B, subject: c, timezone: Mars/Base}\ncourses: [{id: X1, lectures: 2}]\n", "IANA"),
    ("tutor: [\n", "not valid YAML"),
])
def test_a_bad_courses_file_names_the_problem(tmp_path, body, needle):
    with pytest.raises(courses.CourseConfigError, match=needle):
        courses._load(_write(tmp_path, body))


def test_a_minimal_courses_file_loads_with_defaults(tmp_path):
    identity, course_list = courses._load(_write(
        tmp_path, "tutor: {name: Ada, instructor: Dr. Lee, subject: statistics}\n"
                  "courses: [{id: STAT1, lectures: 3}]\n"))
    assert identity.instructor_short == "Dr. Lee"
    assert identity.timezone == "UTC"
    assert identity.code_language == ""
    assert course_list[0].label == "STAT1" and course_list[0].lectures == 3


def test_a_missing_courses_file_says_what_to_do(tmp_path):
    with pytest.raises(courses.CourseConfigError, match="TUTOR_CONTENT_DIR"):
        courses._load(tmp_path / "courses.yaml")


# ---------------------------------------------------------------------------
# python -m pipeline check-content
# ---------------------------------------------------------------------------

def _copy_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    shutil.copytree(FIXTURE, root)
    return root


def test_the_fixture_passes_the_content_check():
    from pipeline.check_content import check

    report = check(FIXTURE)
    assert report.errors == []


def test_the_content_check_names_each_problem(tmp_path):
    from pipeline.check_content import check

    root = _copy_fixture(tmp_path)
    cur = root / "curriculum"
    goals = json.loads((cur / "learning_goals.json").read_text())
    goals["goals"].append({"id": "AST101-L09-too-late", "title": "x", "description": "x",
                           "course": "AST101", "lecture": 9})
    goals["goals"].append(dict(goals["goals"][0]))                         # a duplicate id
    goals["edges"].append({"from": "AST101-L01-celestial-sphere", "to": "AST101-L99-nope"})
    goals["edges"].append({"from": "AST101-L01-daily-motion", "to": "AST101-L01-celestial-sphere"})  # a cycle
    (cur / "learning_goals.json").write_text(json.dumps(goals))
    tracks = json.loads((cur / "tracks.json").read_text())
    tracks["tracks"] = [t for t in tracks["tracks"] if t["id"] != "AST102-lecture-order"]
    (cur / "tracks.json").write_text(json.dumps(tracks))
    (cur / "AST102-lecture-plan.md").unlink(missing_ok=True)

    errors = "\n".join(check(root).errors)
    assert "AST101-L09-too-late: lecture 9 is outside 1..4" in errors
    assert "duplicate id" in errors
    assert "AST101-L99-nope: names a goal that does not exist" in errors
    assert "prerequisite cycle" in errors
    assert "'AST102-lecture-order'" in errors
    assert "AST102-lecture-plan.md: missing" in errors


def test_an_explorable_that_loads_from_the_network_is_an_error(tmp_path):
    from pipeline.check_content import check

    root = _copy_fixture(tmp_path)
    (root / "explorables").mkdir(exist_ok=True)
    (root / "explorables" / "AST101-L02-explain-moon-phases.html").write_text(
        '<script src="https://cdn.example.com/x.js"></script>')
    assert any("loads something from the network" in e for e in check(root).errors)


# ---------------------------------------------------------------------------
# python -m pipeline init-tracks
# ---------------------------------------------------------------------------

def test_init_tracks_writes_a_lecture_order_track_per_course():
    from pipeline.init_tracks import build

    goals = json.loads((FIXTURE / "curriculum" / "learning_goals.json").read_text())
    out, written = build(goals, {"tracks": []})
    assert written == ["AST101-lecture-order", "AST102-lecture-order"]
    by_id = {t["id"]: t for t in out["tracks"]}
    ast101 = by_id["AST101-lecture-order"]["goal_ids"]
    assert set(ast101) == {g["id"] for g in goals["goals"] if g["course"] == "AST101"}
    assert ast101.index("AST101-L01-celestial-sphere") < ast101.index("AST101-L01-daily-motion")
    lectures = [int(g.split("-L")[1][:2]) for g in ast101]
    assert lectures == sorted(lectures)


def test_init_tracks_keeps_existing_tracks_unless_forced():
    from pipeline.init_tracks import build

    goals = json.loads((FIXTURE / "curriculum" / "learning_goals.json").read_text())
    mine = {"id": "AST101-lecture-order", "course": "AST101", "title": "Mine", "goal_ids": []}
    out, written = build(goals, {"tracks": [mine]})
    assert written == ["AST102-lecture-order"]
    assert out["tracks"][0]["title"] == "Mine"
    out, written = build(goals, {"tracks": [mine]}, force=True)
    assert "AST101-lecture-order" in written and out["tracks"][0]["title"] == "Lecture Order"
