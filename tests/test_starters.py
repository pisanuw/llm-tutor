"""First-login UX, Phase 2: starter prompts, lecture titles, and the tutor's
first-conversation orientation note."""

from __future__ import annotations

import pytest

from tutor.agent.lecture_plan import all_lecture_titles, lecture_titles
from tutor.agent.starters import load_starter_prompts, starter_prompts
from tutor.db.models import LearningGoal, Message, Student, Track
from tutor.agent.turn_stages import context
from tests.helpers import make_session


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False: the TestClient serves requests on
    # another thread, which would otherwise get its own empty :memory: DB.
    session = make_session()
    session.add(Student(id=1, email="stu@example.edu", name="Stu Dent"))
    session.add_all([
        LearningGoal(id="AST101-L01-hello", title="Describe the celestial sphere",
                     description="", course="AST101", lecture_num=1),
        LearningGoal(id="AST102-L01-parallax", title="Measure a star's distance by parallax",
                     description="", course="AST102", lecture_num=1),
        Track(id="AST101-lecture-order", course="AST101", title="Lecture Order",
              description="", goal_ids_json='["AST101-L01-hello"]', graph_version=1),
        Track(id="AST102-lecture-order", course="AST102", title="Lecture Order",
              description="", goal_ids_json='["AST102-L01-parallax"]', graph_version=1),
    ])
    session.commit()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Curated file
# ---------------------------------------------------------------------------

def test_curated_prompts_cover_both_courses():
    data = load_starter_prompts()
    for course in ("AST101", "AST102"):
        assert len(data[course]) >= 5, course
        for e in data[course]:
            assert e["label"] and e["message"]
    assert len(data["meta"]) >= 1


def test_curated_prompts_are_course_specific():
    data = load_starter_prompts()
    assert not {e["message"] for e in data["AST101"]} & {e["message"] for e in data["AST102"]}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_starters_lead_with_the_students_next_goal(db):
    chips = starter_prompts(1, "AST102", db)
    assert chips[0]["kind"] == "next"
    assert chips[0]["goal_id"] == "AST102-L01-parallax"
    assert chips[0]["opener"] is True and chips[0]["modality"] == "teach"
    assert chips[0]["message"] == "Explain: Measure a star's distance by parallax"
    assert chips[1]["kind"] == "next" and chips[1]["modality"] == "quiz"
    assert "practice exercise" in chips[1]["message"]


def test_starters_mix_course_and_meta_prompts_without_duplicates(db):
    chips = starter_prompts(1, "AST101", db)
    kinds = [c["kind"] for c in chips]
    assert kinds.count("course") == 3
    assert kinds.count("meta") == 1
    messages = [c["message"] for c in chips]
    assert len(messages) == len(set(messages))
    curated_ast101 = {e["message"] for e in load_starter_prompts()["AST101"]}
    assert all(c["message"] in curated_ast101 for c in chips if c["kind"] == "course")


def test_starters_are_stable_per_student_and_vary_between_students(db):
    a1 = [c["message"] for c in starter_prompts(1, "AST101", db)]
    a2 = [c["message"] for c in starter_prompts(1, "AST101", db)]
    assert a1 == a2
    others = {tuple(c["message"] for c in starter_prompts(sid, "AST101", None)) for sid in range(2, 40)}
    assert len(others) > 1


def test_starters_without_db_still_give_curated_chips():
    chips = starter_prompts(7, "AST102", None)
    assert chips and all(c["kind"] in ("course", "meta") for c in chips)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def test_starters_endpoint_follows_the_course(student_client):
    r = student_client.get("/chat/starters?course=AST102")
    assert r.status_code == 200
    body = r.json()
    assert body["course"] == "AST102"
    assert body["starters"][0]["goal_id"] == "AST102-L01-parallax"
    curated_ast101 = {e["message"] for e in load_starter_prompts()["AST101"]}
    assert not any(c["message"] in curated_ast101 for c in body["starters"])


def test_chat_page_carries_name_and_lecture_titles(student_client, db):
    from tutor.db.models import StudentPath
    db.add(StudentPath(student_id=1, course="AST101", track_id="AST101-lecture-order"))
    db.commit()
    r = student_client.get("/chat")
    assert r.status_code == 200
    assert "Hi Stu." in r.text
    assert "window.LECTURE_TITLES" in r.text
    assert "Phases of the Moon and Eclipses" in r.text
    assert "Try asking" in r.text


def test_progress_panel_orients_a_new_student(student_client, db):
    from tutor.db.models import StudentPath
    db.add(StudentPath(student_id=1, course="AST101", track_id="AST101-lecture-order"))
    db.commit()
    r = student_client.get("/chat/progress?course=AST101")
    assert "Getting started" in r.text
    assert "Nothing here yet" in r.text
    assert "Messages:" not in r.text
    assert "Describe the celestial sphere" in r.text   # What's next still shows


# ---------------------------------------------------------------------------
# Lecture titles
# ---------------------------------------------------------------------------

def test_lecture_titles_come_from_the_curriculum_plans():
    t_ast101 = lecture_titles("AST101")
    t_ast102 = lecture_titles("AST102")
    assert len(t_ast101) == 4 and len(t_ast102) == 4         # the fixture plans' lectures
    assert t_ast101[2] == "Phases of the Moon and Eclipses"
    assert t_ast102[3].startswith("Galaxies")
    assert lecture_titles("999") == {}
    assert set(all_lecture_titles()) == {"AST101", "AST102"}


# ---------------------------------------------------------------------------
# First-conversation note in the actor's per-request context
# ---------------------------------------------------------------------------

def test_first_conversation_note_only_before_any_exchange(db):
    student = db.get(Student, 1)
    ctx = context.build_per_request_context(student, None, "AST101", db)
    assert "FIRST CONVERSATION" in ctx and "AST 101" in ctx

    # This turn's own (flushed) user message still counts as first.
    db.add(Message(student_id=1, role="user", content="hi", course="AST101"))
    db.flush()
    assert "FIRST CONVERSATION" in context.build_per_request_context(student, None, "AST101", db)

    # Once an exchange exists in the course, the note is gone...
    db.add(Message(student_id=1, role="assistant", content="hello", course="AST101"))
    db.flush()
    assert "FIRST CONVERSATION" not in context.build_per_request_context(student, None, "AST101", db)
    # ...but the other course is still a first conversation.
    assert "FIRST CONVERSATION" in context.build_per_request_context(student, None, "AST102", db)
