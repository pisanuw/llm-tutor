"""Plan step 4.2 (finding C16): the admin Students and Supervision tabs.

C16: the Students tab ran three queries per student and loaded every
ExerciseAttempt row, code submissions included, to count them; Supervision
loaded every Student to map ids to emails.
Done when: one query per aggregate.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tutor.config as config
from tutor.db.models import Base, ExerciseAttempt, Message, Student, TopicAccess, TurnEvent


def _world(n: int):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for i in range(1, n + 1):
        db.add(Student(id=i, email=f"s{i}@example.edu", name=f"S{i}"))
    db.flush()
    for i in range(1, n + 1):
        db.add_all([Message(student_id=i, role="user", content="q", course="AST101") for _ in range(i % 4)])
        db.add_all([ExerciseAttempt(student_id=i, exercise_id=f"ex{k}", submission="int main(){}" * 50,
                                    score=float(k), passed=k >= 3) for k in range(i % 3)])
        db.add(ExerciseAttempt(student_id=i, exercise_id="unscored", submission="x", score=None))
        db.add_all([TopicAccess(student_id=i, lecture_num=1, topic_tag="t") for _ in range(i % 2)])
        db.add(TurnEvent(student_id=i, kind="chat"))
    db.commit()
    return engine, db


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-test")
    config.get_config.cache_clear()
    from tutor.auth.deps import get_db
    from tutor.main import app

    made = []

    def open_world(n: int):
        engine, db = _world(n)
        statements: list[str] = []
        event.listen(engine, "before_cursor_execute",
                     lambda conn, cur, stmt, params, ctx, many: statements.append(stmt))
        app.dependency_overrides[get_db] = lambda: db
        client = TestClient(app)
        from tutor.routers.admin import issue_admin_session
        client.cookies.set("admin_session", issue_admin_session())
        made.append(db)
        return client, db, statements

    try:
        yield open_world
    finally:
        app.dependency_overrides.clear()
        for db in made:
            db.close()
        config.get_config.cache_clear()


def _queries(open_world, n: int, tab: str) -> tuple[list[str], str]:
    client, _db, statements = open_world(n)
    statements.clear()
    body = client.get(f"/admin?tab={tab}").text
    return list(statements), body


@pytest.mark.parametrize("tab", ["students", "supervision"])
def test_the_query_count_does_not_grow_with_the_class(admin, tab):
    few, _ = _queries(admin, 3, tab)
    many, _ = _queries(admin, 30, tab)
    assert len(few) == len(many), (len(few), len(many))


def test_the_students_tab_never_reads_code_submissions(admin):
    statements, _ = _queries(admin, 5, "students")
    assert not any("submission" in s for s in statements)


def test_the_students_tab_numbers_are_unchanged(admin):
    _, body = _queries(admin, 5, "students")
    # s5: 1 message, 2 exercises scored 0 and 1 plus one unscored (3 rows,
    # average 0.5 over the scored ones), 1 topic.
    start = body.rindex("<tr", 0, body.index("s5@example.edu"))
    row = body[start:body.index("</tr>", start)]
    cells = [c.strip() for c in re.findall(r"<td[^>]*>\s*(.*?)\s*</td>", row, re.S)]
    assert cells[0] == "s5@example.edu"
    assert cells[2] == "1"          # messages
    assert cells[3] == "3"          # exercise attempts
    assert "0.5" in cells[4]        # average score, unscored attempts skipped
    assert cells[5] == "1"          # topics


def test_supervision_does_not_load_every_student_for_emails(admin):
    statements, body = _queries(admin, 30, "supervision")
    unfiltered_full_rows = [s for s in statements
                            if "FROM students" in s and "WHERE" not in s and "students.name" in s]
    assert unfiltered_full_rows == []
    assert "s30@example.edu" in body     # the events still show their students
