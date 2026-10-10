"""The recent-conversations list (tutor/conversations.py):
the 30-minute gap, the row's fields and flags, the joins by time and by
message id, sorting, the filters and the cap, the words, and the cookie
baseline for "newer than your previous visit".
"""
from __future__ import annotations

import datetime

import pytest

from tests.helpers import make_session
from tutor import conversations as conv
from tutor.db.models import (ClassEnrollment, CourseClass, Message, MessageFeedback, Student,
                             TurnEvent, WhitelistEntry)

NOW = datetime.datetime(2026, 10, 6, 20, 0, 0)          # Tuesday 1 pm in the class's zone (fixture: Los Angeles)
T0 = NOW - datetime.timedelta(hours=2)


def _m(sid, minute, role="user", text="what is parallax?", course="AST101", lecture=None, mid=None):
    return Message(id=mid, student_id=sid, role=role, content=text, course=course, lecture_num=lecture,
                   created_at=T0 + datetime.timedelta(minutes=minute))


@pytest.fixture
def db():
    s = make_session(
        Student(id=1, email="jane@example.edu", name="Jane"),
        Student(id=2, email="john@example.edu", name="John"),
        Student(id=3, email="prof@example.edu", name="Prof", is_instructor=True),
        CourseClass(id=7, name="A", course="AST101", term="Au26", instructor_id=3),
        ClassEnrollment(class_id=7, student_id=1),
        WhitelistEntry(email="@example.edu", added_by="t"),         # everyone here may sign in
    )
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Splitting and building (pure)
# ---------------------------------------------------------------------------

def _msg(sid, minute, role="user", text="q", course="AST101", lecture=None, mid=1):
    return conv.Msg(mid, sid, role, T0 + datetime.timedelta(minutes=minute), course, lecture, text)


def test_a_gap_over_thirty_minutes_starts_a_new_conversation():
    runs = conv.split([_msg(1, 0, mid=1), _msg(1, 29, "assistant", mid=2), _msg(1, 60, mid=3),
                       _msg(1, 61, "assistant", mid=4), _msg(2, 61, mid=5)])
    assert [[m.id for m in r] for r in runs] == [[1, 2], [3, 4], [5]]      # 31 min apart; another student
    one = conv.split([_msg(1, 0, mid=1), _msg(1, 30, mid=2)])
    assert len(one) == 1                                                   # exactly 30 min stays together


def test_a_row_counts_and_describes_its_run():
    run = [_msg(1, 0, text="  how   does\nparallax work? " + "x" * 100, lecture=3, mid=10),
           _msg(1, 1, "assistant", "...", lecture=3, mid=11),
           _msg(1, 5, text="and delete?", lecture=4, mid=12),
           _msg(1, 6, "assistant", "...", course="AST102", lecture=4, mid=13)]
    c = conv.build(run)
    assert (c.student_id, c.first_message_id, c.message_ids) == (1, 10, [10, 11, 12, 13])
    assert (c.questions, c.replies) == (2, 2)
    assert c.courses == ["AST101", "AST102"] and c.course == "AST101, AST102"
    assert c.lectures == [3, 4] and c.lecture_labels == ["L03", "L04"]
    assert c.preview.startswith("how does parallax work? xxx") and len(c.preview) == 80 and c.preview.endswith("…")
    assert c.duration == datetime.timedelta(minutes=6)


def test_a_conversation_with_no_question_has_an_empty_preview():
    assert conv.build([_msg(1, 0, "assistant", "Hi! :-)")]).preview == ""


def test_turns_join_by_time_and_feedback_by_message_id():
    a = conv.build([_msg(1, 0, mid=1), _msg(1, 1, "assistant", mid=2)])
    b = conv.build([_msg(1, 60, mid=3), _msg(1, 62, "assistant", mid=4)])
    other = conv.build([_msg(2, 0, mid=5)])

    def turn(sid, minute, kind="chat", cost=0.01, verdicts=None, det=None):
        return TurnEvent(student_id=sid, kind=kind, cost_usd=cost, ts=T0 + datetime.timedelta(minutes=minute),
                         verdicts_json=verdicts, det_check_json=det)

    conv.attach_turns([a, b, other], [
        turn(1, -2),                                    # 2 min before the first message: belongs
        turn(1, 1, verdicts='{"detector_cut": true}'),  # a real cut
        turn(1, 1, det='{"cut": false}'),               # not a cut
        turn(1, 20),                                    # 19 min after the last message: nobody's
        turn(1, 59, kind="exam", cost=0.05),            # just before b
        turn(1, 63, kind="quota", cost=0.0),            # a quota hit, not spend
        turn(1, 62, kind="cache_warm", cost=0.3),       # the warm-up never counts
        turn(2, 0, cost=0.02),
    ])
    assert (a.turns, round(a.cost_usd, 2), a.cuts, a.exams, a.quota_hits) == (3, 0.03, 1, 0, 0)
    assert (b.turns, round(b.cost_usd, 2), b.exams, b.quota_hits) == (1, 0.05, 1, 1)
    assert (other.turns, round(other.cost_usd, 2)) == (1, 0.02)

    conv.attach_feedback([a, b], [MessageFeedback(message_id=2, student_id=1, rating=-1),
                                  MessageFeedback(message_id=4, student_id=1, rating=1),
                                  MessageFeedback(message_id=99, student_id=1, rating=-1)])
    assert (a.thumbs_down, b.thumbs_down) == (1, 0)
    conv.attach_first_ever([a, b], {1: 1})
    assert a.first_ever and not b.first_ever
    assert a.flags == 3 and a.flagged                   # thumbs, cut, first
    assert b.flags == 2 and b.flagged                   # exam, quota
    assert not other.flagged and other.flags == 0


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def test_the_words_for_ago_span_and_when():
    assert conv.ago(NOW - datetime.timedelta(seconds=30), NOW) == "just now"
    assert conv.ago(NOW - datetime.timedelta(minutes=12), NOW) == "12 min ago"
    assert conv.ago(NOW - datetime.timedelta(hours=3), NOW) == "3 h ago"
    assert conv.ago(NOW - datetime.timedelta(days=2, hours=5), NOW) == "2 d ago"
    assert conv.span_words(NOW, NOW + datetime.timedelta(seconds=20)) == "<1 min"
    assert conv.span_words(NOW, NOW + datetime.timedelta(minutes=14)) == "14 min"
    assert conv.span_words(NOW, NOW + datetime.timedelta(minutes=125)) == "2 h 5 min"
    assert conv.when_label(NOW - datetime.timedelta(hours=1), NOW) == "12:00"            # today, in the class's zone
    assert conv.when_label(NOW - datetime.timedelta(days=1), NOW) == "Mon 13:00"
    assert conv.when_label(NOW - datetime.timedelta(days=10), NOW) == "Sep 26 13:00"
    assert conv.clean_preview(None) == "" and conv.clean_preview(" a  b ") == "a b"


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

def _rows():
    a = conv.build([_msg(1, 0, mid=1), _msg(1, 10, "assistant", mid=2), _msg(1, 11, mid=3)])
    b = conv.build([_msg(2, 30, mid=4), _msg(2, 31, "assistant", mid=5)])
    c = conv.build([_msg(1, 90, mid=6)])
    a.email, b.email, c.email = "jane@example.edu", "john@example.edu", "jane@example.edu"
    a.cost_usd, b.cost_usd, c.cost_usd = 0.02, 0.05, 0.0
    b.thumbs_down = 1
    return a, b, c


def test_sorting_every_key_both_ways_with_newest_first_on_ties():
    a, b, c = _rows()
    assert conv.sort_rows([a, b, c], "start", True) == [c, b, a]
    assert conv.sort_rows([a, b, c], "start", False) == [a, b, c]
    assert conv.sort_rows([a, b, c], "student", False) == [c, a, b]        # ties: newest first
    assert conv.sort_rows([a, b, c], "questions", True) == [a, c, b]        # 2, 1, 1 (c newer than b)
    assert conv.sort_rows([a, b, c], "cost", True) == [b, a, c]
    assert conv.sort_rows([a, b, c], "duration", True) == [a, b, c]
    assert conv.sort_rows([a, b, c], "flags", True) == [b, c, a]
    assert conv.sort_rows([a, b, c], "nonsense", False) == [c, b, a]
    assert conv.sort_spec(None, None) == ("start", True)
    assert conv.sort_spec("student", None) == ("student", False)
    assert conv.sort_spec("cost", None) == ("cost", True)
    assert conv.sort_spec("cost", "asc") == ("cost", False)
    assert conv.sort_spec("bogus", "asc") == ("start", True)
    assert [conv.clamp_days(v) for v in ("1", 30, "9", None, "x")] == [1, 30, 7, 7, 7]


# ---------------------------------------------------------------------------
# The list, against the database
# ---------------------------------------------------------------------------

def _seed(db):
    db.add_all([
        _m(1, 0, mid=1, lecture=3), _m(1, 1, "assistant", "a", mid=2, lecture=3),
        _m(1, 50, text="is a nebula a galaxy?", mid=3), _m(1, 51, "assistant", "no", mid=4),
        _m(2, 55, text="help with the seasons", course="AST102", mid=5), _m(2, 56, "assistant", "sure", course="AST102", mid=6),
        _m(1, 118, text="last one", mid=7),                              # 2 min before NOW: active
    ])
    db.add_all([
        TurnEvent(student_id=1, kind="chat", cost_usd=0.02, ts=T0),
        TurnEvent(student_id=1, kind="chat", cost_usd=0.03, ts=T0 + datetime.timedelta(minutes=50),
                  verdicts_json='{"detector_cut": true}'),
        TurnEvent(student_id=2, kind="chat", cost_usd=0.04, ts=T0 + datetime.timedelta(minutes=55)),
        MessageFeedback(message_id=4, student_id=1, rating=-1, comment="wrong"),
    ])
    db.commit()


def test_recent_builds_rows_with_students_classes_flags_and_words(db):
    _seed(db)
    listing = conv.recent(db, NOW - datetime.timedelta(days=7), now=NOW)
    assert listing.total == 4 and not listing.capped
    rows = listing.rows
    assert [(c.email, c.first_message_id) for c in rows] == [
        ("jane@example.edu", 7), ("john@example.edu", 5), ("jane@example.edu", 3), ("jane@example.edu", 1)]
    newest, john, second, first = rows
    assert newest.active and newest.ago == "2 min ago" and newest.span == "<1 min" and newest.when == "12:58"
    assert first.name == "Jane" and first.classes == ["A (Au26)"] == [db.get(CourseClass, 7).label]
    assert first.first_ever and not second.first_ever
    assert (first.cost_usd, second.cost_usd, second.cuts, second.thumbs_down) == (0.02, 0.03, 1, 1)
    assert john.course == "AST102" and john.classes == [] and john.cost_usd == 0.04 and john.first_ever
    assert first.lecture_labels == ["L03"] and first.preview == "what is parallax?"


def test_recent_filters_by_students_course_and_flags_and_caps(db):
    _seed(db)
    since = NOW - datetime.timedelta(days=7)
    assert [c.email for c in conv.recent(db, since, student_ids=[2], now=NOW).rows] == ["john@example.edu"]
    assert conv.recent(db, since, student_ids=[], now=NOW).rows == []
    assert [c.first_message_id for c in conv.recent(db, since, course="AST102", now=NOW).rows] == [5]
    flagged = conv.recent(db, since, flagged_only=True, now=NOW)
    assert [c.first_message_id for c in flagged.rows] == [3] and flagged.total == 1
    capped = conv.recent(db, since, now=NOW, limit=2)
    assert capped.capped and capped.total == 4 and [c.first_message_id for c in capped.rows] == [7, 5]
    # The window: a message before `since` is not a conversation.
    assert conv.recent(db, NOW - datetime.timedelta(minutes=30), now=NOW).total == 1


def test_mark_new_and_the_seen_cookie_keep_a_sitting_together():
    a, b, c = _rows()
    assert conv.mark_new([a, b, c], T0 + datetime.timedelta(minutes=20)) == 2
    assert (a.is_new, b.is_new, c.is_new) == (False, True, True)
    assert conv.mark_new([a, b, c], None) == 0

    first = conv.seen_state(None, NOW)                                      # first visit: no baseline
    assert first.baseline is None and first.cookie == f"|{NOW.isoformat(timespec='seconds')}"
    again = conv.seen_state(first.cookie, NOW + datetime.timedelta(minutes=3))   # sorting, same sitting
    assert again.baseline is None and again.cookie is None
    later = conv.seen_state(first.cookie, NOW + datetime.timedelta(hours=4))     # a new sitting
    assert later.baseline == NOW and later.cookie.startswith(NOW.isoformat(timespec="seconds") + "|")
    within = conv.seen_state(later.cookie, NOW + datetime.timedelta(hours=4, minutes=9))
    assert within.baseline == NOW and within.cookie is None
    assert conv.seen_state("garbage", NOW).baseline is None
    assert conv.parse_ts("2026-10-06T20:00:00+00:00") == NOW and conv.parse_ts("nope") is None


def test_thread_returns_one_conversations_messages_with_feedback(db):
    _seed(db)
    start, end = T0 + datetime.timedelta(minutes=50), T0 + datetime.timedelta(minutes=51)
    msgs = conv.thread(db, 1, start, end)
    assert [m.id for m in msgs] == [3, 4] and msgs[1].feedback.rating == -1
    assert [m.id for m in conv.thread(db, 1, course="AST101")] == [1, 2, 3, 4, 7]
    assert [m.id for m in conv.thread(db, 1, limit=2)] == [4, 7]
    assert conv.thread(db, 2, course="AST101") == []


# ---------------------------------------------------------------------------
# The admin tab
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(db, monkeypatch):
    from fastapi.testclient import TestClient

    import tutor.routers.admin as admin_mod
    from tutor.auth.deps import get_db
    from tutor.main import app
    from tutor.routers.admin import issue_admin_session

    monkeypatch.setattr(conv, "utcnow", lambda: NOW)
    monkeypatch.setattr(admin_mod, "utcnow", lambda: NOW)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, base_url="http://localhost")
    client.cookies.set("admin_session", issue_admin_session())
    yield client
    app.dependency_overrides.clear()


def _row_starts(page: str) -> list[str]:
    """The conversations' start times in page order (the table's rows)."""
    import html as _html
    import re
    import urllib.parse
    table = page[page.index("data-conversations-table"):page.index("data-conversations-cards")]
    urls = [_html.unescape(m) for m in re.findall(r'data-thread-url="([^"]+)"', table)]
    return [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["start"][0] for u in urls]


def test_the_admin_tab_lists_recent_conversations_by_default(admin, db):
    _seed(db)
    r = admin.get("/admin?tab=conversations")
    assert r.status_code == 200
    page = r.text
    assert "Recent conversations" in page and "4 in the last 7 days" in page
    assert _row_starts(page) == [(T0 + datetime.timedelta(minutes=m)).isoformat() for m in (118, 55, 50, 0)]
    assert "what is parallax?" in page and "is a nebula a galaxy?" in page and "help with the seasons" in page
    assert 'data-badge="thumbs">👎 1' in page and 'data-badge="cut">cut<' in page
    assert page.count('data-badge="first"') == 4                            # jane's first + john's first, table and cards
    assert "A (Au26)" in page and "Jane" in page and 'data-active' in page
    assert "$0.030" in page and "1 / 1" in page and "1 / 0" in page
    assert 'href="/admin?tab=timeline&amp;student=jane%40example.edu"' in page
    assert 'href="/admin?tab=supervision&amp;student=jane%40example.edu"' in page
    assert 'href="/admin?tab=logins&amp;student=jane%40example.edu"' in page
    assert 'target="_blank" rel="noopener"' in page and "#msg-3" in page
    assert "/admin/conversations/thread?student=jane%40example.edu&amp;start=" in page
    assert "/admin/conversations/export" not in page                        # exports live on the transcript view


def test_the_admin_list_sorts_and_filters_from_the_query_string(admin, db):
    _seed(db)
    asc = admin.get("/admin?tab=conversations&sort=start&dir=asc").text
    assert _row_starts(asc) == [(T0 + datetime.timedelta(minutes=m)).isoformat() for m in (0, 50, 55, 118)]
    assert 'data-sort="start"' in asc and "When ▲" in asc
    by_student = admin.get("/admin?tab=conversations&sort=student").text
    assert _row_starts(by_student)[-1] == (T0 + datetime.timedelta(minutes=55)).isoformat()   # john last
    assert 'href="/admin?tab=conversations&amp;days=7&amp;sort=student&amp;dir=desc"' in by_student  # the flip
    flagged = admin.get("/admin?tab=conversations&flagged=1").text
    assert _row_starts(flagged) == [(T0 + datetime.timedelta(minutes=50)).isoformat()] and "1 in the last 7 days" in flagged
    assert _row_starts(admin.get("/admin?tab=conversations&course=AST102").text) == [(T0 + datetime.timedelta(minutes=55)).isoformat()]
    assert len(_row_starts(admin.get("/admin?tab=conversations&class_id=7").text)) == 3
    assert "in the last 1 day" in admin.get("/admin?tab=conversations&days=1").text
    assert "in the last 7 days" in admin.get("/admin?tab=conversations&days=9").text
    assert "Recent conversations" in admin.get("/admin?tab=conversations&student=").text   # an empty box is the list
    assert "No conversations in this window." in admin.get("/admin?tab=conversations&class_id=999").text


def test_the_admin_filter_form_submits_blank_pickers(admin, db):
    """Show with "all classes" and "all courses" selected sends class_id= and
    course= empty, which used to answer 422 int_parsing JSON (2026-10-06)."""
    _seed(db)
    r = admin.get("/admin?tab=conversations&days=7&class_id=&course=&flagged=&student=")
    assert r.status_code == 200
    assert "Recent conversations" in r.text and len(_row_starts(r.text)) == 4      # unfiltered
    # A hand-edited class id is ignored, not an error page
    # (finding R16).
    r = admin.get("/admin?tab=conversations&class_id=abc")
    assert r.status_code == 200 and len(_row_starts(r.text)) == 4


def test_the_admin_list_highlights_rows_newer_than_the_previous_visit(admin, db):
    _seed(db)
    first = admin.get("/admin?tab=conversations")
    cookie = first.headers.get("set-cookie", "")
    assert "admin_convos_seen=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie and "Path=/admin" in cookie             # http://localhost in the test
    assert "data-new" not in first.text                                   # nothing to compare with yet
    # Sorting a minute later is the same sitting: no new cookie, still nothing highlighted.
    again = admin.get("/admin?tab=conversations&sort=cost")
    assert "set-cookie" not in again.headers and "data-new" not in again.text
    # The previous sitting started between the second and third conversation and this is
    # a new sitting: the two newer conversations light up, and stay lit while sorting.
    baseline = T0.isoformat(timespec="seconds")
    sitting = (T0 + datetime.timedelta(minutes=52)).isoformat(timespec="seconds")
    from tutor.routers.admin import issue_admin_session
    admin.cookies.clear()                                                 # drop the server's cookie first
    admin.cookies.set("admin_session", issue_admin_session())
    admin.cookies.set("admin_convos_seen", f"{baseline}|{sitting}")
    page = admin.get("/admin?tab=conversations").text
    assert "2 new since your last visit" in page and page.count('data-new>') == 4   # table and cards
    assert "2 new since your last visit" in admin.get("/admin?tab=conversations&sort=cost").text


def test_the_transcript_view_keeps_its_shape_and_links_back(admin, db):
    _seed(db)
    page = admin.get("/admin?tab=conversations&student=jane@example.edu").text
    assert 'id="msg-3"' in page and "is a nebula a galaxy?" in page
    assert "👎 Student flagged this" in page and ">wrong</span>" in page
    assert "/admin/conversations/export?student=jane%40example.edu&amp;format=md" in page
    assert 'data-back-to-list' in page and "Recent conversations" in page
    assert "data-conversations-table" not in page


# ---------------------------------------------------------------------------
# The thread fragment and expanding in place
# ---------------------------------------------------------------------------

def test_the_thread_fragment_renders_one_span_and_escapes_it(admin, db):
    _seed(db)
    db.add(Message(id=8, student_id=1, role="user", content="<script>alert(1)</script> & co", course="AST101",
                   created_at=T0 + datetime.timedelta(minutes=51, seconds=30)))
    db.commit()
    start = (T0 + datetime.timedelta(minutes=50)).isoformat()
    end = (T0 + datetime.timedelta(minutes=52)).isoformat()
    r = admin.get(f"/admin/conversations/thread?student=jane@example.edu&start={start}&end={end}")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert 'id="msg-3"' in r.text and 'id="msg-4"' in r.text and 'id="msg-8"' in r.text and 'id="msg-1"' not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in r.text and "<script>alert" not in r.text
    assert "👎 Student flagged this" in r.text
    assert "No messages in this span." in admin.get(f"/admin/conversations/thread?student=ghost@example.edu&start={start}&end={end}").text
    assert "No messages in this span." in admin.get("/admin/conversations/thread?student=jane@example.edu&start=x&end=y").text
    assert "<!DOCTYPE" not in r.text                                        # a fragment, not a page


def test_the_thread_fragment_is_for_admins_only(db):
    from fastapi.testclient import TestClient

    from tests.helpers import login_cookie
    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.cookies.set("session_token", login_cookie(db, student_id=1))
        r = client.get("/admin/conversations/thread?student=jane@example.edu&start=2026-10-06T18:00:00&end=2026-10-06T19:00:00",
                       follow_redirects=False)
        assert r.status_code in (303, 401, 403) and "msg-" not in r.text
    finally:
        app.dependency_overrides.clear()


def test_a_row_expands_in_place_in_the_browser(browser, db, monkeypatch):
    from tests.browser_support import ORIGIN, app_client, open_page

    import tutor.routers.admin as admin_mod
    from tutor.routers.admin import issue_admin_session

    monkeypatch.setattr(conv, "utcnow", lambda: NOW)
    monkeypatch.setattr(admin_mod, "utcnow", lambda: NOW)
    _seed(db)
    with app_client(db) as client:
        client.cookies.set("admin_session", issue_admin_session())
        with open_page(browser, client, width=1280, height=800, clock=False) as (page, net):
            page.goto(f"{ORIGIN}/admin?tab=conversations")
            page.wait_for_selector("[data-conversations-table]")
            rows = page.locator("[data-conversations-table] tr[data-conversation]")
            assert rows.count() == 4
            third = rows.nth(2)                                           # jane, "is a nebula a galaxy?"
            third.locator("[data-expand]").click()
            thread = page.locator("[data-conversations-table] tr[data-thread-row]").nth(2)
            thread.locator("#msg-4").wait_for()
            assert "is a nebula a galaxy?" in thread.inner_text() and "👎 Student flagged this" in thread.inner_text()
            assert third.locator("[data-expand]").get_attribute("aria-expanded") == "true"
            assert any(s.startswith("GET /admin/conversations/thread") for s in net.seen)
            fetched = len([s for s in net.seen if s.startswith("GET /admin/conversations/thread")])
            third.locator("td").nth(3).click()                            # the row itself closes it ...
            assert not thread.is_visible()
            third.locator("td").nth(3).click()                            # ... and opens it again, no refetch
            assert thread.is_visible()
            assert len([s for s in net.seen if s.startswith("GET /admin/conversations/thread")]) == fetched
            # The new-tab link is a link: clicking it does not toggle the row.
            assert third.locator('a[target="_blank"]').get_attribute("href").endswith("#msg-3")


# ---------------------------------------------------------------------------
# Instructors: the same list inside their class
# ---------------------------------------------------------------------------

@pytest.fixture
def classes(db):
    """prof (id 3) teaches class 7 with jane; other (id 4) teaches class 8 with john."""
    db.add_all([Student(id=4, email="other@example.edu", name="Other", is_instructor=True),
                CourseClass(id=8, name="B", course="AST102", term="Au26", instructor_id=4),
                ClassEnrollment(class_id=8, student_id=2)])
    db.commit()
    _seed(db)
    # jane's AST102 question is not class 7's business.
    db.add(Message(id=9, student_id=1, role="user", content="a AST102 question", course="AST102",
                   created_at=T0 + datetime.timedelta(minutes=100)))
    db.commit()


def _signed_in(db, student_id, monkeypatch):
    from fastapi.testclient import TestClient

    import tutor.routers.instructor as instr_mod
    from tests.helpers import login_cookie
    from tutor.auth.deps import get_db
    from tutor.main import app

    monkeypatch.setattr(conv, "utcnow", lambda: NOW)
    monkeypatch.setattr(instr_mod, "utcnow", lambda: NOW)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, base_url="http://localhost")
    client.cookies.set("session_token", login_cookie(db, student_id=student_id))
    return client


@pytest.fixture
def prof(db, classes, monkeypatch):
    client = _signed_in(db, 3, monkeypatch)
    yield client
    from tutor.main import app
    app.dependency_overrides.clear()


def test_an_instructor_sees_the_classes_conversations_without_cost_or_admin_links(prof):
    dashboard = prof.get("/instructor?class_id=7").text
    assert 'data-conversations-link' in dashboard and "view=conversations&class_id=7" in dashboard
    r = prof.get("/instructor?view=conversations&class_id=7")
    assert r.status_code == 200
    page = r.text
    assert "Recent conversations" in page and "3 in the last 7 days" in page       # jane's three AST101 conversations
    assert "a AST102 question" not in page and "help with the seasons" not in page       # other course, other class
    assert 'data-sort="cost"' not in page and "$0." not in page
    assert "tab=timeline" not in page and "tab=supervision" not in page and "tab=logins" not in page
    assert "/instructor/conversations/thread?class_id=7&amp;student=jane%40example.edu&amp;start=" in page
    assert 'href="/instructor/transcript/view?class_id=7&amp;student=jane%40example.edu#msg-3"' in page
    assert 'data-roster-link' in page and '<input type="hidden" name="view" value="conversations">' in page
    assert 'data-badge="thumbs"' in page and 'data-badge="first"' in page
    cookie = r.headers.get("set-cookie", "")
    assert "instructor_convos_seen=" in cookie and "Path=/instructor" in cookie and "HttpOnly" in cookie
    # Sorting and the flag filter work through the same URL shape.
    assert 'href="/instructor?view=conversations&amp;class_id=7&amp;days=7&amp;sort=student&amp;dir=asc"' in page
    assert "1 in the last 7 days" in prof.get("/instructor?view=conversations&class_id=7&flagged=1").text
    only = prof.get("/instructor?view=conversations&class_id=7&student=jane@example.edu").text
    assert "3 in the last 7 days" in only and 'value="jane@example.edu"' in only


def test_the_instructor_list_thread_and_transcript_page_are_scoped(prof):
    assert prof.get("/instructor?view=conversations&class_id=8").status_code == 403          # not my class
    assert prof.get("/instructor?view=conversations&class_id=7&student=john@example.edu").status_code == 403
    start = (T0 + datetime.timedelta(minutes=50)).isoformat()
    end = (T0 + datetime.timedelta(minutes=51)).isoformat()
    frag = prof.get(f"/instructor/conversations/thread?class_id=7&student=jane@example.edu&start={start}&end={end}")
    assert frag.status_code == 200 and 'id="msg-3"' in frag.text and 'id="msg-4"' in frag.text
    assert "<!DOCTYPE" not in frag.text
    assert prof.get(f"/instructor/conversations/thread?class_id=8&student=john@example.edu&start={start}&end={end}").status_code == 403
    assert prof.get(f"/instructor/conversations/thread?class_id=7&student=john@example.edu&start={start}&end={end}").status_code == 403
    # The AST102 message sits inside a span but outside the class's course.
    s2 = (T0 + datetime.timedelta(minutes=99)).isoformat()
    e2 = (T0 + datetime.timedelta(minutes=101)).isoformat()
    assert "No messages in this span." in prof.get(f"/instructor/conversations/thread?class_id=7&student=jane@example.edu&start={s2}&end={e2}").text
    view = prof.get("/instructor/transcript/view?class_id=7&student=jane@example.edu")
    assert view.status_code == 200
    assert 'id="msg-1"' in view.text and 'id="msg-7"' in view.text and "a AST102 question" not in view.text
    assert "/instructor/transcript?class_id=7&student=jane%40example.edu&format=md" in view.text
    assert 'data-back-to-list' in view.text and "5 messages" in view.text
    assert prof.get("/instructor/transcript/view?class_id=8&student=john@example.edu").status_code == 403
    assert prof.get("/instructor/transcript/view?class_id=7&student=john@example.edu").status_code == 403


def test_a_student_gets_nothing_from_the_instructor_conversation_routes(db, classes, monkeypatch):
    jane = _signed_in(db, 1, monkeypatch)
    try:
        assert jane.get("/instructor?view=conversations&class_id=7").status_code == 403
        assert jane.get("/instructor/conversations/thread?class_id=7&student=jane@example.edu&start=2026-10-06T18:00:00&end=2026-10-06T19:00:00").status_code == 403
        assert jane.get("/instructor/transcript/view?class_id=7&student=jane@example.edu").status_code == 403
    finally:
        from tutor.main import app
        app.dependency_overrides.clear()


def test_the_instructor_list_fits_a_phone_and_expands_a_card(browser, db, classes, monkeypatch):
    from tests.browser_support import ORIGIN, app_client, open_page

    import tutor.routers.instructor as instr_mod

    monkeypatch.setattr(conv, "utcnow", lambda: NOW)
    monkeypatch.setattr(instr_mod, "utcnow", lambda: NOW)
    with app_client(db, student_id=3) as client:
        with open_page(browser, client, width=390, height=844, clock=False, touch=True) as (page, net):
            page.goto(f"{ORIGIN}/instructor?view=conversations&class_id=7")
            page.wait_for_selector("[data-conversations-cards]")
            assert page.evaluate("() => document.documentElement.scrollWidth") <= 390
            assert not page.locator("[data-conversations-table]").is_visible()
            cards = page.locator("[data-conversations-cards] li[data-conversation]")
            assert cards.count() == 3
            card = cards.nth(1)                                            # "is a nebula a galaxy?"
            card.locator("[data-expand]").click()
            card.locator("[data-thread] #msg-4").wait_for()
            assert "is a nebula a galaxy?" in card.inner_text()
            assert page.evaluate("() => document.documentElement.scrollWidth") <= 390
