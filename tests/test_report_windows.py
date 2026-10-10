"""Plan step 4.4 (finding B23): report windows name the right days.

B23: the daily and thumbs-down reports ran a rolling UTC window and labelled
it with the UTC date, so the email sent at 1:45 am local time named tomorrow,
and the weekly report's dates were UTC too; a thumbs-down whose comment was
added the next day was reported again as if new.
Done when: the Monday and daily emails name the right days.
"""
from __future__ import annotations

import argparse
import datetime

import pytest

from tutor.db.models import Message, MessageFeedback, Student
from tutor.utctime import local_day, window_label
from tests.helpers import make_session

D = datetime.datetime


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("day,since,until", [
    ("2026-09-25", D(2026, 9, 25, 7), D(2026, 9, 26, 7)),      # PDT, 24 hours
    ("2026-03-08", D(2026, 3, 8, 8), D(2026, 3, 9, 7)),        # clocks go forward: 23 hours
    ("2026-11-01", D(2026, 11, 1, 7), D(2026, 11, 2, 8)),      # clocks go back: 25 hours
    ("2026-12-15", D(2026, 12, 15, 8), D(2026, 12, 16, 8)),    # PST
])
def test_a_date_is_a_local_calendar_day(day, since, until):
    assert local_day(day) == (since, until)


def test_the_daily_window_names_the_day_it_covers():
    """The daily job runs at 08:45 UTC, 1:45 am in the class's zone (the
    fixture's America/Los_Angeles): its window is mostly Sep 25 there, and the label says so (the UTC date said Sep 26)."""
    until = D(2026, 9, 26, 8, 45)
    label = window_label(until - datetime.timedelta(hours=24), until)
    assert label == "Fri Sep 25, 1:45 am to Sat Sep 26, 1:45 am PDT"
    winter = D(2026, 12, 16, 9, 45)
    assert window_label(winter - datetime.timedelta(hours=24), winter) == \
        "Tue Dec 15, 1:45 am to Wed Dec 16, 1:45 am PST"


def test_the_monday_email_names_local_dates():
    """A weekly window ending 06:30 UTC Monday ends on Sunday evening in
    the class's zone: the label is Sep 20 to Sep 27, not the UTC Sep 21 to Sep 28."""
    from tutor import instructor_report

    until = D(2026, 9, 28, 6, 30)
    label = instructor_report.window_label(until - datetime.timedelta(days=7), until)
    assert label == "Sep 20 to Sep 27, 2026 (PDT)"
    assert instructor_report.subject([], label).endswith("(Sep 20 to Sep 27, 2026 (PDT))")


def test_the_daily_script_windows():
    from scripts import daily_report, feedback_report

    since, until, label, folder = daily_report._window(argparse.Namespace(date="2026-09-25", hours=24))
    assert (since, until, folder) == (D(2026, 9, 25, 7), D(2026, 9, 26, 7), "2026-09-25")
    assert label == "Fri Sep 25, 12:00 am to Sat Sep 26, 12:00 am PDT"
    since, until, label = feedback_report._window(argparse.Namespace(date="2026-09-25", hours=24))
    assert (since, until) == (D(2026, 9, 25, 7), D(2026, 9, 26, 7))
    # The rolling window's files go under the local date it starts on.
    _s, _u, _l, folder = daily_report._window(argparse.Namespace(date=None, hours=24))
    assert len(folder) == 10 and folder[4] == "-"


# ---------------------------------------------------------------------------
# Thumbs-down by the day they were given
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="kim@example.edu", name="Kim"))
    s.commit()
    yield s
    s.close()


def _flag(db, created, updated, comment=None):
    q = Message(student_id=1, role="user", content="what is a nebula?", course="AST102", created_at=created)
    r = Message(student_id=1, role="assistant", content="A nebula is a planet.", course="AST102",
                created_at=created)
    db.add_all([q, r])
    db.flush()
    db.add(MessageFeedback(message_id=r.id, student_id=1, rating=-1, comment=comment,
                           created_at=created, updated_at=updated))
    db.commit()


def test_a_comment_added_the_next_day_is_marked_not_reported_as_new(db):
    from tutor.feedback_report import render_report_markdown, thumbs_down_in_window

    day1, day2 = local_day("2026-09-24"), local_day("2026-09-25")
    _flag(db, created=day1[0] + datetime.timedelta(hours=5),
          updated=day2[0] + datetime.timedelta(hours=3), comment="Nebulae are gas clouds")
    first = thumbs_down_in_window(db, *day1)
    assert len(first) == 1 and not first[0].updated_later
    second = thumbs_down_in_window(db, *day2)
    assert len(second) == 1 and second[0].updated_later
    assert second[0].flagged_at == day1[0] + datetime.timedelta(hours=5)     # when it was given
    text = render_report_markdown(second, window_label(*day2), "https://tutor.test")
    assert "[comment added; flagged earlier]" in text
    assert "Fri Sep 25, 12:00 am to Sat Sep 26, 12:00 am PDT" in text
    # Nothing changed on day 3: not reported again.
    assert thumbs_down_in_window(db, *local_day("2026-09-26")) == []


def test_a_flag_given_and_explained_the_same_day_is_plain(db):
    from tutor.feedback_report import thumbs_down_in_window

    day = local_day("2026-09-25")
    _flag(db, created=day[0] + datetime.timedelta(hours=2),
          updated=day[0] + datetime.timedelta(hours=2, minutes=1), comment="Wrong")
    flags = thumbs_down_in_window(db, *day)
    assert len(flags) == 1 and not flags[0].updated_later
