"""Interactive explorables for threshold concepts (Engram integration, Phase 4).

Network-free. Covers the goal_id -> URL helper (existence-gated, traversal-safe),
the authored explorable files against the Explorable Contract, the threshold flag
in the curriculum data + its seeding onto LearningGoal, and the /map payload
surfacing the explorable URL only for threshold goals that have a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tutor.agent.explorables import EXPLORABLES_DIR, explorable_path, explorable_url, has_explorable
from tutor.db.models import Base, LearningGoal, Student
from tests.helpers import make_session

# The goals flagged threshold in the fixture curriculum, and the subset with an
# explorable in tests/fixtures/content/explorables.
THRESHOLD_GOAL_IDS = {
    "AST101-L02-explain-moon-phases",
    "AST101-L03-axial-tilt-seasons",
    "AST102-L01-parallax-distance",
    "AST102-L02-hr-diagram",
    "AST102-L04-hubble-law",
}
AUTHORED = ("AST101-L02-explain-moon-phases", "AST102-L01-parallax-distance")

_GOALS_JSON = Path(__file__).resolve().parent / "fixtures" / "content" / "curriculum" / "learning_goals.json"


# ---------------------------------------------------------------------------
# URL helper: existence-gated and traversal-safe
# ---------------------------------------------------------------------------

def test_url_present_when_file_exists(tmp_path):
    (tmp_path / "AST101-L02-explain-moon-phases.html").write_text("<html></html>")
    assert explorable_url("AST101-L02-explain-moon-phases", base_dir=tmp_path) == \
        "/explorables/AST101-L02-explain-moon-phases.html"
    assert has_explorable("AST101-L02-explain-moon-phases", base_dir=tmp_path)


def test_url_none_when_file_absent(tmp_path):
    assert explorable_url("AST102-L03-galaxy-types", base_dir=tmp_path) is None
    assert not has_explorable("AST102-L03-galaxy-types", base_dir=tmp_path)
    assert explorable_path("AST102-L03-galaxy-types", base_dir=tmp_path) is None


@pytest.mark.parametrize("bad", ["../secret", "a/b", "..", "x.html", "foo bar", ""])
def test_unsafe_goal_id_rejected(tmp_path, bad):
    # Even if a matching file somehow existed, an unsafe id never resolves.
    assert explorable_url(bad, base_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Authored files vs the Explorable Contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("goal_id", AUTHORED)
def test_authored_file_exists_and_resolves(goal_id):
    assert explorable_url(goal_id) == f"/explorables/{goal_id}.html"
    assert (EXPLORABLES_DIR / f"{goal_id}.html").is_file()


@pytest.mark.parametrize("goal_id", AUTHORED)
def test_authored_file_is_self_contained_and_complete(goal_id):
    html = (EXPLORABLES_DIR / f"{goal_id}.html").read_text()
    assert len(html) > 2000
    # Offline: no external CSS/JS/font/CDN references.
    assert "http://" not in html and "https://" not in html
    assert "<script" in html  # it is actually interactive
    # Contract: predict-before-reveal + a blank-page reconstruction prompt.
    assert "reconstruct" in html.lower()
    assert "predict" in html.lower()


@pytest.mark.parametrize("goal_id", AUTHORED)
def test_authored_goal_is_flagged_threshold(goal_id):
    assert goal_id in THRESHOLD_GOAL_IDS


# ---------------------------------------------------------------------------
# Curriculum data: the threshold flag
# ---------------------------------------------------------------------------

def test_curriculum_flags_exactly_the_expected_threshold_goals():
    data = json.loads(_GOALS_JSON.read_text())
    flagged = {g["id"] for g in data["goals"] if g.get("threshold")}
    assert flagged == THRESHOLD_GOAL_IDS


# ---------------------------------------------------------------------------
# Seeding: the flag lands on LearningGoal.threshold
# ---------------------------------------------------------------------------

def test_seeder_sets_threshold_column():
    from tutor.startup import seed_learning_goals_if_empty

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    seed_learning_goals_if_empty(engine)

    Session = sessionmaker(bind=engine)
    with Session() as db:
        flagged = db.get(LearningGoal, "AST102-L01-parallax-distance")
        plain = db.get(LearningGoal, "AST101-L01-daily-motion")
        assert flagged is not None and flagged.threshold is True
        assert plain is not None and plain.threshold is False


# ---------------------------------------------------------------------------
# /map surfaces the explorable URL only for threshold goals with a file
# ---------------------------------------------------------------------------

@pytest.fixture()
def map_client():
    from tutor.auth.deps import get_current_student, get_db
    from tutor.main import app

    session = make_session()
    s = Student(email="stu@example.edu", name="Stu")
    session.add(s)
    session.add(LearningGoal(
        id="AST101-L02-explain-moon-phases", title="Explain the cause of lunar phases",
        description="", course="AST101", lecture_num=2, threshold=True,
    ))
    session.add(LearningGoal(
        id="AST101-L01-daily-motion", title="Explain why stars rise and set",
        description="", course="AST101", lecture_num=1, threshold=False,
    ))
    session.commit()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()
    session.close()


def test_map_payload_includes_explorable_for_threshold_goal(map_client):
    body = map_client.get("/map").text
    # The authored threshold goal exposes its explorable URL...
    assert "/explorables/AST101-L02-explain-moon-phases.html" in body
    # ...and the non-threshold goal carries an explicit null (never a stray URL).
    assert '"AST101-L01-daily-motion"' in body
    assert '"explorable_url": null' in body
