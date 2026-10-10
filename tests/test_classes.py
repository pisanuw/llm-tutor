"""Instructor role + classes, Phase 1 (the instructor-role plan):
the role flag, class/roster CRUD, the scoping contract, and the admin tab."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tutor import classes as classes_mod
from tutor.agent import state
from tutor.auth.access import is_instructor, list_instructors, set_instructor
from tutor.db.models import ClassEnrollment, ClassInstructor, CourseClass, Student
from tests.helpers import make_session

ADMIN = "admin@example.edu"


@pytest.fixture()
def db(monkeypatch):
    import tutor.config as config

    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    monkeypatch.setenv("ADMIN_TOKEN", "ci-dummy-admin-token")
    config.get_config.cache_clear()

    session = make_session()
    session.add_all([
        Student(id=1, email=ADMIN, name="Ada Admin"),
        Student(id=2, email="teach@example.edu", name="Terry Teacher"),
        Student(id=3, email="other@example.edu", name="Olive Other"),
        Student(id=4, email="stu1@example.edu", name="Sam One"),
        Student(id=5, email="stu2@example.edu", name="Sal Two"),
        Student(id=6, email="loner@example.edu", name="Lee Loner"),
    ])
    session.commit()
    yield session
    session.close()
    config.get_config.cache_clear()


def _class(db, name="AST 101 A", course="AST101", email="teach@example.edu", term="Autumn 2026"):
    return classes_mod.create_class(
        db, name=name, course=course, instructor_email=email, term=term, created_by=ADMIN
    )


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------

def test_role_is_off_until_granted_and_admins_always_count(db):
    assert is_instructor(db, "teach@example.edu") is False
    assert set_instructor(db, "teach@example.edu", True) is True
    assert is_instructor(db, "TEACH@example.edu") is True          # case-insensitive
    assert is_instructor(db, ADMIN) is True                   # admins pass without the flag
    assert is_instructor(db, "nobody@example.edu") is False        # unknown email
    assert is_instructor(db, None) is False
    assert [s.email for s in list_instructors(db)] == ["teach@example.edu"]

    assert set_instructor(db, "teach@example.edu", False) is True
    assert is_instructor(db, "teach@example.edu") is False
    assert set_instructor(db, "ghost@example.edu", True) is False  # no such student


def test_demoting_keeps_the_classes(db):
    set_instructor(db, "teach@example.edu", True)
    c = _class(db)
    set_instructor(db, "teach@example.edu", False)
    assert db.get(CourseClass, c.id) is not None


# ---------------------------------------------------------------------------
# Class CRUD
# ---------------------------------------------------------------------------

def test_create_class_normalizes_and_links_the_instructor(db):
    c = _class(db, name="  AST 101 A  ")
    assert c is not None
    assert c.name == "AST 101 A" and c.course == "AST101"
    assert c.instructor_id == 2 and c.created_by == ADMIN
    assert c.archived is False and c.weekly_report is True
    assert c.label == "AST 101 A (Autumn 2026)"


def test_create_class_keeps_the_quarter_dates(db):
    import datetime

    c = classes_mod.create_class(db, name="AST 102 B", course="AST102", instructor_email="teach@example.edu",
                                 quarter_start=datetime.date(2026, 9, 28),
                                 quarter_end=datetime.date(2026, 12, 20))
    assert (c.quarter_start, c.quarter_end) == (datetime.date(2026, 9, 28), datetime.date(2026, 12, 20))
    assert c.in_quarter(datetime.date(2026, 9, 28)) and c.in_quarter(datetime.date(2026, 12, 20))
    assert not c.in_quarter(datetime.date(2026, 12, 21))
    # An end before the start is unusable; later, the pair is set together.
    assert classes_mod.create_class(db, name="X", course="AST102", instructor_email="teach@example.edu",
                                    quarter_start=datetime.date(2026, 12, 20),
                                    quarter_end=datetime.date(2026, 9, 28)) is None
    classes_mod.update_class(db, c.id, quarter_start=datetime.date(2027, 1, 4), quarter_end=None)
    assert c.quarter_start == datetime.date(2026, 9, 28)
    classes_mod.update_class(db, c.id, quarter_start=datetime.date(2027, 1, 4),
                             quarter_end=datetime.date(2027, 3, 20))
    assert (c.quarter_start, c.quarter_end) == (datetime.date(2027, 1, 4), datetime.date(2027, 3, 20))
    assert classes_mod.parse_day("2027-01-04") == datetime.date(2027, 1, 4)
    assert classes_mod.parse_day("") is None and classes_mod.parse_day("soon") is None


def test_create_class_rejects_bad_input(db):
    assert _class(db, name="   ") is None
    assert _class(db, course="999") is None
    assert _class(db, email="ghost@example.edu") is None


def test_update_class_edits_in_place_and_leaves_course_alone(db):
    c = _class(db)
    assert classes_mod.update_class(db, c.id, name="AST 101 B", term="Winter 2027") is not None
    row = db.get(CourseClass, c.id)
    assert row.name == "AST 101 B" and row.term == "Winter 2027" and row.course == "AST101"

    assert classes_mod.update_class(db, c.id, instructor_email="other@example.edu") is not None
    assert db.get(CourseClass, c.id).instructor_id == 3
    assert classes_mod.update_class(db, c.id, instructor_email="ghost@example.edu") is None
    assert classes_mod.update_class(db, 999, name="x") is None

    classes_mod.update_class(db, c.id, archived=True, weekly_report=False)
    row = db.get(CourseClass, c.id)
    assert row.archived is True and row.weekly_report is False


def test_enroll_unenroll_and_bulk_paste(db):
    c = _class(db)
    assert classes_mod.enroll(db, c.id, "stu1@example.edu") is True
    assert classes_mod.enroll(db, c.id, "stu1@example.edu") is False   # already on it
    assert classes_mod.enroll(db, 999, "stu1@example.edu") is False
    assert classes_mod.enroll(db, c.id, "notanemail") is False    # unusable

    added, skipped = classes_mod.enroll_many(
        db, c.id, "stu2@example.edu, STU1@example.edu\nghost@example.edu  notanemail"
    )
    assert added == 2                                  # stu2, plus ghost pre-registered
    assert set(skipped) == {"stu1@example.edu", "notanemail"}
    assert [s.email for s in classes_mod.class_roster(db, c.id)] == [
        "ghost@example.edu", "stu1@example.edu", "stu2@example.edu"
    ]

    assert classes_mod.unenroll(db, c.id, "stu1@example.edu") is True
    assert classes_mod.unenroll(db, c.id, "stu1@example.edu") is False
    assert set(classes_mod.roster_ids(db, c.id)) == {
        5, db.query(Student).filter_by(email="ghost@example.edu").one().id
    }


def test_enroll_pre_registers_an_email_with_no_account(db):
    """The roster does not wait on the student (never-logged-in students show)."""
    c = _class(db)
    assert db.query(Student).filter_by(email="ghost@example.edu").first() is None

    assert classes_mod.enroll(db, c.id, " GHOST@example.edu ") is True

    ghost = db.query(Student).filter_by(email="ghost@example.edu").one()
    assert ghost.name == ""               # filled in by their first login
    assert ghost.login_count == 0
    assert ghost.last_login is None
    assert ghost.is_instructor is False
    assert [s.email for s in classes_mod.class_roster(db, c.id)] == ["ghost@example.edu"]

    # Idempotent: re-pasting the same roster neither duplicates the account nor
    # the enrollment.
    assert classes_mod.enroll(db, c.id, "ghost@example.edu") is False
    assert db.query(Student).filter_by(email="ghost@example.edu").count() == 1
    assert len(classes_mod.roster_ids(db, c.id)) == 1


@pytest.mark.parametrize("bad", ["notanemail", "@example.edu", "stu@example", "stu@.edu",
                                 "stu@example.", "a" * 300 + "@example.edu"])
def test_enroll_never_pre_registers_an_unusable_address(db, bad):
    """A typo must not become a permanent account nobody can log into."""
    c = _class(db)
    before = db.query(Student).count()
    assert classes_mod.enroll(db, c.id, bad) is False
    assert db.query(Student).count() == before
    assert classes_mod.roster_ids(db, c.id) == []


def test_pre_registered_student_keeps_their_row_when_they_log_in(db):
    """The first login must adopt the rostered row, not orphan it."""
    from tutor.routers.auth import _login_or_create

    c = _class(db)
    classes_mod.enroll(db, c.id, "ghost@example.edu")
    rostered_id = db.query(Student).filter_by(email="ghost@example.edu").one().id

    _login_or_create(db, "ghost@example.edu", name="Gale Host", google_sub="sub-123")

    ghost = db.query(Student).filter_by(email="ghost@example.edu").one()
    assert ghost.id == rostered_id        # same row: the enrollment still points at it
    assert ghost.name == "Gale Host"     # details arrived later, as intended
    assert ghost.login_count == 1
    assert ghost.last_login is not None
    assert classes_mod.roster_ids(db, c.id) == [rostered_id]


def test_delete_class_removes_roster_but_not_students(db):
    c = _class(db)
    classes_mod.enroll(db, c.id, "stu1@example.edu")
    assert classes_mod.delete_class(db, c.id) is True
    assert classes_mod.delete_class(db, c.id) is False
    assert db.query(ClassEnrollment).count() == 0
    assert db.query(Student).filter_by(email="stu1@example.edu").one() is not None


def test_enrollment_counts_and_class_listings(db):
    set_instructor(db, "teach@example.edu", True)
    a = _class(db, name="AST 101 A")
    b = _class(db, name="AST 102 B", course="AST102")
    old = _class(db, name="AST 101 Z")
    classes_mod.update_class(db, old.id, archived=True)
    classes_mod.enroll(db, a.id, "stu1@example.edu")
    classes_mod.enroll(db, a.id, "stu2@example.edu")
    classes_mod.enroll(db, b.id, "stu2@example.edu")

    assert classes_mod.enrollment_counts(db, [a.id, b.id, old.id]) == {a.id: 2, b.id: 1}
    assert classes_mod.enrollment_counts(db, []) == {}
    active = {c.id for c in classes_mod.classes_for_instructor(db, 2)}
    assert active == {a.id, b.id}
    assert old.id in {c.id for c in classes_mod.classes_for_instructor(db, 2, include_archived=True)}
    assert {c.id for c in classes_mod.classes_for_student(db, 5)} == {a.id, b.id}


# ---------------------------------------------------------------------------
# Scoping contract
# ---------------------------------------------------------------------------

def test_instructor_sees_only_their_own_class(db):
    set_instructor(db, "teach@example.edu", True)
    set_instructor(db, "other@example.edu", True)
    mine = _class(db, name="Mine", email="teach@example.edu")
    theirs = _class(db, name="Theirs", email="other@example.edu")
    classes_mod.enroll(db, mine.id, "stu1@example.edu")
    classes_mod.enroll(db, theirs.id, "stu2@example.edu")

    teach = db.get(Student, 2)
    other = db.get(Student, 3)
    admin = db.get(Student, 1)

    assert classes_mod.can_view_class(db, teach, mine.id) is not None
    assert classes_mod.can_view_class(db, teach, theirs.id) is None
    assert classes_mod.can_view_class(db, teach, 999) is None
    assert classes_mod.can_view_class(db, other, theirs.id) is not None
    # Admins may open any class.
    assert classes_mod.can_view_class(db, admin, mine.id) is not None
    assert classes_mod.can_view_class(db, admin, theirs.id) is not None

    assert classes_mod.visible_student_ids(db, teach) == {4}
    assert classes_mod.visible_student_ids(db, other) == {5}
    assert classes_mod.visible_student_ids(db, admin) == {4, 5}
    assert classes_mod.can_view_student(db, teach, 4) is True
    assert classes_mod.can_view_student(db, teach, 5) is False
    assert classes_mod.can_view_student(db, teach, 6) is False   # enrolled nowhere


def test_a_plain_student_sees_nothing(db):
    _class(db)
    assert classes_mod.visible_student_ids(db, db.get(Student, 6)) == set()
    assert classes_mod.visible_classes(db, db.get(Student, 6)) == []


def test_archived_classes_stay_visible_to_their_instructor(db):
    set_instructor(db, "teach@example.edu", True)
    c = _class(db)
    classes_mod.enroll(db, c.id, "stu1@example.edu")
    classes_mod.update_class(db, c.id, archived=True)
    teach = db.get(Student, 2)
    # Not in the default (active) list, but the roster is still theirs to read.
    assert classes_mod.visible_classes(db, teach) == []
    assert classes_mod.can_view_class(db, teach, c.id) is not None
    assert classes_mod.visible_student_ids(db, teach) == {4}


# ---------------------------------------------------------------------------
# Deletion / retention
# ---------------------------------------------------------------------------

def test_deleting_a_student_clears_their_enrollments(db):
    c = _class(db)
    classes_mod.enroll(db, c.id, "stu1@example.edu")
    counts = state.delete_student_data(db, 4)
    db.commit()
    assert counts["class_enrollments"] == 1
    assert db.query(ClassEnrollment).count() == 0
    assert db.get(CourseClass, c.id) is not None


def test_deleting_an_instructor_removes_their_classes(db):
    set_instructor(db, "teach@example.edu", True)
    c = _class(db)
    classes_mod.enroll(db, c.id, "stu1@example.edu")
    counts = state.delete_student_data(db, 2)
    db.commit()
    assert counts["classes"] == 1
    # erasure_scope() reaches the roster through the class, so the enrolled
    # student's row is counted with class_enrollments.
    assert counts["class_enrollments"] == 1
    assert db.query(CourseClass).count() == 0
    assert db.query(ClassEnrollment).count() == 0
    # The enrolled student is untouched.
    assert db.query(Student).filter_by(email="stu1@example.edu").one() is not None


# ---------------------------------------------------------------------------
# Admin tab
# ---------------------------------------------------------------------------

def test_admin_classes_tab_renders_and_manages(admin_client, db):
    r = admin_client.get("/admin?tab=classes")
    assert r.status_code == 200
    assert "Instructors" in r.text and "Create a class" in r.text
    assert "No classes yet." in r.text

    r = admin_client.post("/admin/instructors/set",
                          data={"email": "teach@example.edu", "enabled": "on"},
                          follow_redirects=False)
    assert r.status_code == 303
    assert is_instructor(db, "teach@example.edu") is True

    r = admin_client.post("/admin/classes/create",
                          data={"name": "AST 101 A", "course": "AST101",
                                "instructor_email": "teach@example.edu", "term": "Autumn 2026"},
                          follow_redirects=False)
    assert r.status_code == 303
    c = db.query(CourseClass).one()
    assert r.headers["location"] == f"/admin?tab=classes&class_id={c.id}"

    # A paste mixing a student who has logged in with two who never have: all
    # three land on the roster, which is the point of pre-registration.
    admin_client.post("/admin/classes/enroll",
                      data={"class_id": c.id,
                            "emails": "stu1@example.edu stu2@example.edu\nnewbie@example.edu, nobody@example.edu"},
                      follow_redirects=False)
    assert [s.email for s in classes_mod.class_roster(db, c.id)] == [
        "newbie@example.edu", "nobody@example.edu", "stu1@example.edu", "stu2@example.edu"
    ]

    page = admin_client.get(f"/admin?tab=classes&class_id={c.id}").text
    assert "AST 101 A (Autumn 2026) — roster" in page
    assert "stu1@example.edu" in page and "stu2@example.edu" in page
    # The never-logged-in pair are on the page, shown as 4 students, and the
    # roster's own columns say they have not logged in yet.
    assert "newbie@example.edu" in page and "nobody@example.edu" in page
    assert "4 student(s)" in page
    assert "never" in page

    admin_client.post("/admin/classes/unenroll",
                      data={"class_id": c.id, "email": "stu1@example.edu"}, follow_redirects=False)
    assert [s.email for s in classes_mod.class_roster(db, c.id)] == [
        "newbie@example.edu", "nobody@example.edu", "stu2@example.edu"
    ]
    # Removing them from the roster does not delete the account it created.
    admin_client.post("/admin/classes/unenroll",
                      data={"class_id": c.id, "email": "newbie@example.edu"}, follow_redirects=False)
    assert db.query(Student).filter_by(email="newbie@example.edu").one() is not None

    admin_client.post("/admin/classes/update",
                      data={"class_id": c.id, "archived": "on"}, follow_redirects=False)
    assert db.get(CourseClass, c.id).archived is True
    # A toggle form that omits the other fields must not clear them.
    assert db.get(CourseClass, c.id).name == "AST 101 A"
    assert db.get(CourseClass, c.id).weekly_report is True

    admin_client.post("/admin/classes/update",
                      data={"class_id": c.id, "weekly_report": "off"}, follow_redirects=False)
    row = db.get(CourseClass, c.id)
    assert row.weekly_report is False and row.archived is True

    admin_client.post("/admin/classes/delete", data={"class_id": c.id}, follow_redirects=False)
    assert db.query(CourseClass).count() == 0


def test_admin_classes_tab_needs_admin(db):
    from tutor.main import app
    from tutor.auth.deps import get_db

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, raise_server_exceptions=True)   # no admin cookie
    try:
        bounced = client.get("/admin?tab=classes", follow_redirects=False)   # to the login page
        assert bounced.status_code == 303 and bounced.headers["location"].startswith("/auth/login")
        r = client.post("/admin/instructors/set",
                        data={"email": "teach@example.edu", "enabled": "on"})
        assert r.status_code == 403
        assert is_instructor(db, "teach@example.edu") is False
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Several instructors per class (2026-10-06)
# ---------------------------------------------------------------------------

def test_create_class_gives_the_lead_a_membership_row(db):
    c = _class(db)
    assert classes_mod.instructor_ids(db, c.id) == [2]
    assert [s.email for s in classes_mod.class_instructors(db, c.id)] == ["teach@example.edu"]
    assert db.query(ClassInstructor).filter_by(class_id=c.id, student_id=2).one().added_by == ADMIN
    assert classes_mod.is_instructor_of(db, 2, c.id) and not classes_mod.is_instructor_of(db, 3, c.id)


def test_add_instructor_grants_the_role_and_opens_the_class(db):
    c = _class(db)
    classes_mod.enroll(db, c.id, "stu1@example.edu")
    other = db.get(Student, 3)
    assert classes_mod.can_view_class(db, other, c.id) is None
    # The whitelist is on and other@ is not on it: added, with the warning.
    assert (classes_mod.add_instructor(db, c.id, "Other@Example.EDU", added_by="teach@example.edu")
            == classes_mod.ADDED_NO_LOGIN)
    assert classes_mod.add_instructor(db, c.id, "other@example.edu") == classes_mod.ALREADY
    assert classes_mod.add_instructor(db, c.id, "teach@example.edu") == classes_mod.ALREADY   # the lead
    assert classes_mod.add_instructor(db, 999, "other@example.edu") == classes_mod.UNKNOWN
    assert classes_mod.add_instructor(db, c.id, "not an address") == classes_mod.UNKNOWN
    # The role came with it, and every scoping rule now lets them in.
    assert is_instructor(db, "other@example.edu") is True
    assert classes_mod.instructor_ids(db, c.id) == [2, 3]
    assert classes_mod.can_view_class(db, other, c.id) is not None
    assert [k.id for k in classes_mod.classes_for_instructor(db, 3)] == [c.id]
    assert classes_mod.visible_student_ids(db, other) == {4}
    assert classes_mod.can_view_student(db, other, 4) is True
    # Still not another instructor's other classes.
    d = _class(db, name="AST 102 B", course="AST102")
    assert classes_mod.can_view_class(db, other, d.id) is None
    # An address with no account is pre-registered, as a roster paste is;
    # one the whitelist admits is plainly added.
    from tutor.db.models import WhitelistEntry
    db.add(WhitelistEntry(email="new.prof@example.edu"))
    db.commit()
    assert classes_mod.add_instructor(db, c.id, "new.prof@example.edu") == classes_mod.ADDED
    newcomer = db.query(Student).filter_by(email="new.prof@example.edu").one()
    assert newcomer.is_instructor is True and newcomer.login_count == 0


def test_remove_instructor_rules_and_lead_promotion(db):
    c = _class(db)
    # Nobody removes themselves; the last instructor stays; a stranger is not removable.
    assert classes_mod.remove_instructor(db, c.id, "teach@example.edu", actor_email="Teach@example.edu") == classes_mod.SELF
    assert classes_mod.remove_instructor(db, c.id, "teach@example.edu", actor_email=ADMIN) == classes_mod.LAST
    assert classes_mod.remove_instructor(db, c.id, "other@example.edu", actor_email=ADMIN) == classes_mod.NOT_INSTRUCTOR
    assert classes_mod.remove_instructor(db, c.id, "nobody@example.edu", actor_email=ADMIN) == classes_mod.NOT_INSTRUCTOR
    assert classes_mod.remove_instructor(db, 999, "teach@example.edu", actor_email=ADMIN) == classes_mod.UNKNOWN

    classes_mod.add_instructor(db, c.id, "other@example.edu")
    # Removing the lead promotes the longest-standing co-instructor.
    assert classes_mod.remove_instructor(db, c.id, "teach@example.edu", actor_email=ADMIN) == classes_mod.REMOVED
    db.refresh(c)
    assert c.instructor_id == 3
    assert classes_mod.instructor_ids(db, c.id) == [3]
    assert classes_mod.can_view_class(db, db.get(Student, 2), c.id) is None
    # The role is not taken back by a removal (another class may need it).
    assert db.get(Student, 3).is_instructor is True
    # And a co-instructor leaves without touching the lead.
    classes_mod.add_instructor(db, c.id, "teach@example.edu")
    assert classes_mod.remove_instructor(db, c.id, "teach@example.edu", actor_email="other@example.edu") == classes_mod.REMOVED
    db.refresh(c)
    assert c.instructor_id == 3 and classes_mod.instructor_ids(db, c.id) == [3]


def test_replacing_the_lead_from_the_admin_form_swaps_the_membership(db):
    c = _class(db)
    classes_mod.update_class(db, c.id, instructor_email="other@example.edu")
    assert c.instructor_id == 3
    assert classes_mod.instructor_ids(db, c.id) == [3]
    assert classes_mod.can_view_class(db, db.get(Student, 2), c.id) is None


def test_delete_class_and_student_erasure_clear_instructor_rows(db):
    c = _class(db)
    classes_mod.add_instructor(db, c.id, "other@example.edu")
    classes_mod.enroll(db, c.id, "stu1@example.edu")
    # Erasing a co-instructor removes their membership, not the class.
    counts = state.delete_student_data(db, 3)
    db.commit()
    assert counts["class_instructors"] == 1
    assert db.get(CourseClass, c.id) is not None
    assert classes_mod.instructor_ids(db, c.id) == [2]
    # Deleting the class takes its instructor rows with the roster.
    assert classes_mod.delete_class(db, c.id) is True
    assert db.query(ClassInstructor).count() == 0


def test_admin_adds_and_removes_instructors(admin_client, db):
    c = _class(db)
    set_instructor(db, "teach@example.edu", True)

    r = admin_client.post("/admin/classes/instructors/add",
                          data={"class_id": c.id, "email": "other@example.edu"}, follow_redirects=False)
    # The whitelist is on and other@ is not on it: added, with the warning.
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin?tab=classes&class_id={c.id}&note=added_no_login"
    page = admin_client.get(r.headers["location"]).text
    assert 'data-instructor="teach@example.edu"' in page and 'data-instructor="other@example.edu"' in page
    assert "not on the login whitelist" in page
    assert ">lead<" in page
    # The classes table lists both, and the Instructors panel counts the class for both.
    assert "teach@example.edu, other@example.edu" in page

    r = admin_client.post("/admin/classes/instructors/add",
                          data={"class_id": c.id, "email": "other@example.edu"}, follow_redirects=False)
    assert r.headers["location"].endswith("&note=already")
    r = admin_client.post("/admin/classes/instructors/add",
                          data={"class_id": c.id, "email": "junk"}, follow_redirects=False)
    assert r.headers["location"].endswith("&note=unknown")

    # Removing the lead promotes the other; removing the last one is refused.
    r = admin_client.post("/admin/classes/instructors/remove",
                          data={"class_id": c.id, "email": "teach@example.edu"}, follow_redirects=False)
    assert r.headers["location"].endswith("&note=removed")
    db.expire_all()
    assert db.get(CourseClass, c.id).instructor_id == 3
    r = admin_client.post("/admin/classes/instructors/remove",
                          data={"class_id": c.id, "email": "other@example.edu"}, follow_redirects=False)
    assert r.headers["location"].endswith("&note=last")
    assert "at least one instructor" in admin_client.get(r.headers["location"]).text
    assert classes_mod.instructor_ids(db, c.id) == [3]
