"""A hand-edited, restored or reloaded address lands on the page, not on
FastAPI's JSON or an error page (bulletproofing Stage 2).

R13: a parameter FastAPI could not read was its raw 422 JSON on a page; the
numbers a student's pages take parse leniently, and anything left takes the
designed path. R14: the chat script read ?course= raw. R15: /review refused a
bad course. R17: the pages that answer a POST were a 405 from the address bar.
R18: a reload served the Teach-me opener again below the follow-ups. R19: a
form posted with a lapsed session was raw JSON. R20: the login page opened
inside the slides pane.
"""
from __future__ import annotations

import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tutor import alerts
from tutor.db.models import (
    CourseClass, LearningGoal, Message, RequestError, Student, StudentPath, Track, WhitelistEntry,
)
from tutor.utctime import utcnow
from tests.browser_support import ORIGIN, app_client, expect, open_page, student_db
from tests.helpers import login_cookie, make_session

HTML = {"accept": "text/html,application/xhtml+xml"}
NAV = {**HTML, "sec-fetch-dest": "document", "sec-fetch-mode": "navigate"}
FETCH = {"accept": "*/*", "sec-fetch-dest": "empty", "sec-fetch-mode": "cors"}
OPENER = {"opener": "A star's mass decides how it ends.", "title": "Stellar endpoints", "depth": "intro",
          "track": "AST101-lecture-order"}


@pytest.fixture
def site(monkeypatch):
    from tutor.auth.deps import get_db
    from tutor.main import app

    monkeypatch.setenv("ADMIN_EMAILS", "root@example.edu")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import tutor.config as config

    config.get_config.cache_clear()
    db = make_session(
        Student(id=1, email="ada@example.edu", name="Ada"),
        Student(id=2, email="prof@example.edu", name="Prof", is_instructor=True),
        WhitelistEntry(email="@example.edu", added_by="test"),
        Track(id="AST102-lecture-order", course="AST102", title="Lecture order", goal_ids_json="[]"),
        Track(id="AST101-lecture-order", course="AST101", title="Lecture order", goal_ids_json="[]"),
        LearningGoal(id="AST102-L02-stellar-endpoints", title="Stellar endpoints", description="", course="AST102",
                     lecture_num=2),
    )
    db.add(StudentPath(student_id=1, course="AST102", track_id="AST102-lecture-order"))
    db.commit()
    from tutor import classes as classes_mod

    course_class = classes_mod.create_class(db, name="AST 101 A", course="AST101",
                                            instructor_email="prof@example.edu", created_by="root@example.edu")
    classes_mod.enroll(db, course_class.id, "ada@example.edu")
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: sent.append(key) or "queued")
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, db, sent
    finally:
        app.dependency_overrides.clear()
        db.close()


def _as(client, db, student_id):
    client.cookies.set("session_token", login_cookie(db, student_id))
    return client


def _lapsed(client):
    client.cookies.set("session_token", "not-a-live-session")
    return client


# ---------------------------------------------------------------------------
# R13: numbers in the address parse leniently; what is left gets the page
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/lectures/AST101/abc", "/lectures/AST101/1.5", "/lectures/AST101/" + "9" * 300,
                                  "/lectures/AST101/0", "/feedback/abc", "/feedback/1e9"])
def test_a_path_segment_that_is_not_a_number_is_the_not_here_page(site, path):
    client, db, sent = site
    _as(client, db, 1)
    resp = client.get(path, headers=NAV, follow_redirects=False)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "detail" not in resp.text[:200]


def test_a_lecture_number_with_a_leading_zero_still_opens(site, tmp_path, monkeypatch):
    from tutor.config import get_config

    deck = tmp_path / "AST101" / "lecture-03"
    deck.mkdir(parents=True)
    (deck / "index.html").write_text("<html><body>deck</body></html>")
    monkeypatch.setitem(get_config(), "output_dir", tmp_path)
    client, db, _ = site
    _as(client, db, 1)
    for num in ("03", "3"):
        resp = client.get(f"/lectures/AST101/{num}", headers=NAV)
        assert resp.status_code == 200 and "/slides/AST101/lecture-03/index.html" in resp.text


@pytest.mark.parametrize("days", ["abc", "", "-3", "1.5", "9" * 300, "0"])
def test_the_instructor_pages_fall_back_on_a_bad_window(site, days):
    client, db, _ = site
    cid = db.query(CourseClass).one().id
    _as(client, db, 2)
    assert client.get(f"/instructor?days={days}", headers=NAV).status_code == 200
    assert client.get(f"/instructor/report?days={days}", headers=NAV).status_code == 200
    assert client.get(f"/instructor/export?class_id={cid}&days={days}").status_code == 200


def test_fetch_numbers_fall_back_too(site):
    client, db, _ = site
    _as(client, db, 1)
    assert client.get("/path/next?course=AST102&n=abc", headers=FETCH).status_code == 200
    assert client.get("/path/next?course=AST102&n=1000", headers=FETCH).status_code == 200
    # An unknown turn is the 404 the page already handles, whatever the offset.
    assert client.get("/chat/message/nope?offset=abc", headers=FETCH).status_code == 404


def test_what_still_fails_validation_is_the_page_for_a_navigation_and_json_for_a_fetch(site):
    """The net under the lenient parsing: a navigation gets the designed page
    and a row, a fetch keeps FastAPI's 422 body."""
    client, db, sent = site
    _as(client, db, 2)
    page = client.get("/instructor/transcript/view?class_id=abc&student=ada@example.edu", headers=NAV)
    assert page.status_code == 422 and "data-error-page" in page.text
    fetched = client.get("/instructor/transcript/view?class_id=abc&student=ada@example.edu", headers=FETCH)
    assert fetched.status_code == 422 and fetched.json()["detail"][0]["loc"] == ["query", "class_id"]
    assert [r.status for r in db.query(RequestError).order_by(RequestError.id)] == [422, 422]
    assert not [k for k in sent if k.startswith("exc:")]


# ---------------------------------------------------------------------------
# R15, R17
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("course", ["999", "", "abc", "x" * 2000])
def test_review_opens_on_the_students_course_for_a_bad_one(site, course):
    client, db, _ = site
    _as(client, db, 1)
    resp = client.get(f"/review?course={course}", headers=NAV)
    assert resp.status_code == 200 and "AST 102" in resp.text


def test_the_pages_that_answer_a_post_redirect_from_the_address_bar(site):
    client, db, _ = site
    for path in ("/auth/magic-link", "/auth/magic-code"):
        resp = client.get(path, headers=NAV, follow_redirects=False)
        assert (resp.status_code, resp.headers["location"]) == (303, "/auth/login?code=1")
    _as(client, db, 1)
    resp = client.get("/errors/report", headers=NAV, follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (303, "/")
    assert client.get("/auth/login?code=1", headers=NAV).status_code == 200


# ---------------------------------------------------------------------------
# R18: the Teach-me opener is served once
# ---------------------------------------------------------------------------

def test_a_reload_after_follow_ups_does_not_serve_the_opener_again(site, monkeypatch):
    from tutor.agent import teachme

    monkeypatch.setattr(teachme, "get_opener", lambda *a, **k: dict(OPENER))
    client, db, _ = site
    _as(client, db, 1)
    form = {"goal": "AST102-L02-stellar-endpoints", "course": "AST102", "lecture_num": "2"}

    def openers():
        return db.query(Message).filter_by(student_id=1, role="assistant", content=OPENER["opener"]).count()

    assert client.post("/chat/teachme", data=form).json()["cached"] is True
    db.add_all([Message(student_id=1, role="user", content="And a white dwarf's size?", course="AST102"),
                Message(student_id=1, role="assistant", content="About the size of Earth.", course="AST102")])
    db.commit()
    assert client.post("/chat/teachme", data=form).json()["cached"] is True      # the reload
    assert openers() == 1
    # Pressed again on the map or a starter chip (the page says so): the
    # opener comes back at the bottom, as before (the Stage 2 review).
    client.post("/chat/teachme", data={**form, "again": "1"})
    assert openers() == 2
    client.post("/chat/teachme", data={**form, "again": "1"})                # a double tap
    assert openers() == 2
    # Hours later, after more follow-ups, a page without the flag: the
    # window has passed, so a fresh opener.
    db.add_all([Message(student_id=1, role="user", content="And a supernova?", course="AST102"),
                Message(student_id=1, role="assistant", content="Only for the most massive stars.", course="AST102")])
    db.query(Message).update({Message.created_at: utcnow() - datetime.timedelta(hours=3)})
    db.commit()
    client.post("/chat/teachme", data=form)
    assert openers() == 3


# ---------------------------------------------------------------------------
# R19, R20: a lapsed session on a form or in the slides pane
# ---------------------------------------------------------------------------

def test_a_form_posted_with_a_lapsed_session_goes_to_the_login_page(site):
    client, db, _ = site
    _lapsed(client)
    for path, data, came_from in (("/welcome", {"course": "AST101"}, "/welcome"),
                                  ("/errors/report", {"ref": "ABC123"}, "/lectures/AST101/99"),
                                  ("/instructor/weekly-email", {"enabled": "on"}, "/instructor?class_id=5&view=roster"),
                                  ("/errors/report", {"ref": "ABC123"}, "/errors/report")):
        resp = client.post(path, data=data, follow_redirects=False,
                           headers={**NAV, "referer": f"http://testserver{came_from}"})
        assert resp.status_code == 303
        where = urlsplit(resp.headers["location"])
        # The page with its query; a form whose page is its own POST goes on to the chat.
        expected = "/chat" if came_from == path else came_from
        assert where.path == "/auth/login" and parse_qs(where.query)["next"] == [expected]
    # A fetch still gets the 401 its script reads.
    resp = client.post("/welcome", data={"course": "AST101"}, headers=FETCH, follow_redirects=False)
    assert resp.status_code == 401 and resp.json()["detail"]


def test_a_form_refused_for_another_reason_is_the_page_not_json(site):
    client, db, _ = site
    cid = db.query(CourseClass).one().id
    _as(client, db, 1)                                     # a student, not an instructor
    resp = client.post("/instructor/instructors/add", data={"class_id": str(cid), "email": "x@example.edu"},
                       headers=NAV, follow_redirects=False)
    assert resp.status_code in (403, 404) and "data-error-page" in resp.text


def test_the_slides_pane_says_the_session_ended_and_signs_in_the_whole_page(site, tmp_path, monkeypatch):
    from tutor.config import get_config

    deck = tmp_path / "AST101" / "lecture-01"
    deck.mkdir(parents=True)
    (deck / "index.html").write_text("<html><body>deck</body></html>")
    monkeypatch.setitem(get_config(), "output_dir", tmp_path)
    client, db, _ = site
    _lapsed(client)
    resp = client.get("/slides/AST101/lecture-01/index.html", follow_redirects=False,
                      headers={**HTML, "sec-fetch-dest": "iframe", "referer": "http://testserver/chat?course=AST101"})
    assert resp.status_code == 401 and resp.headers["content-type"].startswith("text/html")
    assert "data-signed-out" in resp.text and 'target="_top"' in resp.text
    assert 'href="/auth/login?next=%2Fchat%3Fcourse%3DAST101"' in resp.text
    # The cookie goes, or every lecture picked after it wrote another expiry row.
    assert 'session_token=""' in resp.headers.get("set-cookie", "")
    # The tab itself still goes to the login page with its address kept.
    resp = client.get("/slides/AST101/lecture-01/index.html", headers=NAV, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/login?next=%2Fslides")


# ---------------------------------------------------------------------------
# In the browser: R14, R18
# ---------------------------------------------------------------------------

@pytest.fixture
def chat_client():
    db = student_db(courses=("AST101",))
    with app_client(db) as c:
        yield c


def test_a_hand_edited_course_does_not_break_the_chat(browser, chat_client):
    with open_page(browser, chat_client, width=1280, height=800, clock=False) as (page, net):
        page.goto(f"{ORIGIN}/chat?course=999")
        expect(page.locator("select[aria-label=Course]")).to_have_value("AST101")
        page.wait_for_function("document.querySelector('iframe[title=\"Lecture slides\"]')")
        asked = [s for s in net.seen if "course=" in s and not s.startswith("GET /chat?")]
        assert asked and all("course=999" not in s for s in asked), asked
        assert "/slides/AST101/" in page.get_attribute("iframe[title='Lecture slides']", "src")
        assert page.evaluate("location.search") == "?course=AST101"


def test_the_maps_one_shot_parameters_leave_the_address(browser, chat_client):
    with open_page(browser, chat_client, clock=False) as (page, net):
        served = []
        net.stubs[("POST", "/chat/teachme")] = lambda route, url: (
            served.append(1), route.fulfill(status=200, content_type="application/json",
                                            body='{"cached": true, "depth": "intro", "track": "t"}'))
        page.goto(f"{ORIGIN}/chat?goal=AST101-L02-x&course=AST101&lecture_num=2&depth=intro&topic=Eclipses")
        page.wait_for_function("location.search === '?course=AST101&lecture_num=2'")
        page.reload()
        page.wait_for_function("location.search === '?course=AST101&lecture_num=2'")
        assert served == [1]
        expect(page.locator("select[aria-label=Lecture]")).to_have_value("2")


def test_the_slides_pane_keeps_the_typed_question_and_the_lecture(browser, chat_client):
    """The pane's link signs in the whole page; the question typed and not
    sent comes back as a draft, and next names the course and lecture (the
    Stage 2 review: the link went to /chat and the text was lost)."""
    from tutor.templating import templates

    frame = templates.get_template("signed_out_frame.html").render(login="/auth/login?next=%2Fchat")
    with open_page(browser, chat_client, width=1280, height=800, clock=False) as (page, net):
        net.stubs[("GET", "/slides/AST101/lecture-03/index.html")] = lambda route, url: route.fulfill(
            status=401, content_type="text/html", body=frame)
        page.goto(f"{ORIGIN}/chat?course=AST101")
        page.fill("#msg-input", "Why does the Moon show phases?")
        page.select_option("select[aria-label=Lecture]", "3")
        link = page.frame_locator("iframe[title='Lecture slides']").locator("[data-signed-out] a")
        expect(link).to_have_attribute("href", "/auth/login?next=%2Fchat%3Fcourse%3DAST101%26lecture_num%3D3")
        draft = page.evaluate("JSON.parse(localStorage.getItem('tutor-draft'))")
        assert (draft["text"], draft["course"]) == ("Why does the Moon show phases?", "AST101")
