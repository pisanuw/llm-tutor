"""Plan step 5.3 (findings B28, B20): the learner-state transition as a table.

B28: record_graded_event did the window, the FSRS schedule, the status, and
confirmation in one database-bound function. state.next_progress is now the
pure transition; record_graded_event reads the row and the graded history
and applies the result. B20: three "review due" clocks disagreed (flat decay
in the navigator, a fading heuristic in the progress summary, FSRS in the
envelope and /review), and a mastered row with no evidence date was due
forever. There is now one clock, the FSRS due date (state.review_is_due),
and one decayed score (state.current_mastery).
Done when: threshold, confirmation, and FSRS transitions have table tests.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from tutor.agent import spaced_review as sr
from tutor.agent import state
from tutor.agent.state import GradedOutcome, ProgressSnapshot, next_progress
from tutor.db.models import LearningGoal, Student, StudentProgress
from tests.helpers import make_session

NOW = dt.datetime(2026, 10, 5, 12, 0)
H = dt.timedelta(hours=1)
D = dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Thresholds: upgrade-only status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,window,n_events,upgrade,expected", [
    ("locked",   (0.5,),                 1, True,  "locked"),     # below unlock
    ("locked",   (0.6,),                 1, True,  "unlocked"),   # exactly at unlock
    ("locked",   (1.0, 1.0, 1.0),        3, True,  "unlocked"),   # strong, but too few events
    ("locked",   (0.85,) * 4,            4, True,  "mastered"),   # exactly at master, 4 events
    ("unlocked", (1.0, 0.7, 0.85, 0.9),  4, True,  "mastered"),   # mean 0.8625
    ("unlocked", (1.0, 0.5, 0.85, 0.9),  4, True,  "unlocked"),   # mean 0.8125
    ("unlocked", (0.0,) * 6,             9, True,  "unlocked"),   # never relocks
    ("mastered", (0.0,) * 6,             9, True,  "mastered"),   # mastery is sticky
    ("locked",   (1.0,) * 6,             6, False, "locked"),     # a review answer never moves status
    ("unlocked", (1.0,) * 6,             6, False, "unlocked"),
])
def test_status_thresholds(status, window, n_events, upgrade, expected):
    up = next_progress(ProgressSnapshot(status=status, window=window, n_events=n_events),
                       GradedOutcome(success=window[0], upgrade_status=upgrade), NOW)
    assert up.status == expected
    assert up.status_changed is (expected != status)
    assert up.mastery_score == pytest.approx(sum(window) / len(window))
    newly_mastered = expected == "mastered" and status != "mastered"
    assert up.mastered_at == (NOW if newly_mastered else None)


def test_an_empty_window_scores_zero_and_mastered_at_is_kept():
    earlier = NOW - 30 * D
    up = next_progress(ProgressSnapshot(status="mastered", mastered_at=earlier),
                       GradedOutcome(success=1.0), NOW)
    assert up.mastery_score == 0.0 and up.mastered_at == earlier and not up.status_changed


# ---------------------------------------------------------------------------
# Confirmation: two successful retrievals CONFIRM_MIN_GAP_HOURS apart
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,window,n_events,retrievals,confirmed", [
    ("mastered", (1.0,) * 4, 5, (NOW - 15 * H, NOW), False),     # too close together
    ("mastered", (1.0,) * 4, 5, (NOW - 16 * H, NOW), True),      # exactly the gap
    ("mastered", (1.0,) * 4, 5, (NOW,),               False),     # one retrieval
    ("unlocked", (0.7, 0.7), 2, (NOW - 3 * D, NOW),   False),     # not mastered
    ("locked",   (1.0,) * 4, 4, (NOW - 2 * D, NOW),   True),      # mastered by this event: confirms at once
    ("mastered", (0.6,) * 6, 9, (NOW - 5 * D, NOW - 2 * D), True),   # earlier successes suffice
])
def test_confirmation(status, window, n_events, retrievals, confirmed):
    up = next_progress(ProgressSnapshot(status=status, window=window, n_events=n_events,
                                        retrievals=retrievals),
                       GradedOutcome(success=window[0]), NOW)
    assert (up.confirmed_at == NOW) is confirmed


def test_a_confirmed_goal_stays_confirmed():
    earlier = NOW - 3 * D
    up = next_progress(ProgressSnapshot(status="mastered", window=(0.0,) * 6, n_events=9,
                                        confirmed_at=earlier),
                       GradedOutcome(success=0.0), NOW)
    assert up.confirmed_at == earlier


# ---------------------------------------------------------------------------
# FSRS: every graded event is a review for scheduling
# ---------------------------------------------------------------------------

def _seen(reps=2, lapses=0, stability=3.0, **kw) -> ProgressSnapshot:
    return ProgressSnapshot(status="unlocked", window=(0.8,), n_events=reps, fsrs_stability=stability,
                            fsrs_difficulty=5.0, fsrs_reps=reps, fsrs_lapses=lapses, **kw)


@pytest.mark.parametrize("snapshot,event,rating,elapsed", [
    # First evidence: nothing to measure from, the rating from the fraction.
    (ProgressSnapshot(), GradedOutcome(1.0), sr.Rating.EASY, 0.0),
    # Elapsed days run from the last evidence, not the last write.
    (_seen(last_evidence_at=NOW - 4 * D, updated_at=NOW - H), GradedOutcome(0.7), sr.Rating.GOOD, 4.0),
    # A legacy row: updated_at stands in for a missing evidence date.
    (_seen(reps=1, updated_at=NOW - 2 * D), GradedOutcome(0.5), sr.Rating.HARD, 2.0),
    # The review loop's own rating wins over the fraction.
    (_seen(last_evidence_at=NOW - D), GradedOutcome(0.7, fsrs_rating=sr.Rating.EASY), sr.Rating.EASY, 1.0),
    # A lapse on a reviewed goal.
    (_seen(reps=3, lapses=1, stability=10.0, last_evidence_at=NOW - 12 * D), GradedOutcome(0.0),
     sr.Rating.AGAIN, 12.0),
    # A clock that ran backwards counts as no time passing.
    (_seen(last_evidence_at=NOW + D), GradedOutcome(1.0), sr.Rating.EASY, 0.0),
])
def test_the_fsrs_schedule(snapshot, event, rating, elapsed):
    up = next_progress(snapshot, event, NOW)
    want = sr.rate(sr.FSRSState(stability=snapshot.fsrs_stability, difficulty=snapshot.fsrs_difficulty,
                                reps=snapshot.fsrs_reps, lapses=snapshot.fsrs_lapses), rating, elapsed)
    assert (up.fsrs_stability, up.fsrs_difficulty, up.fsrs_reps, up.fsrs_lapses) == \
        (want.stability, want.difficulty, want.reps, want.lapses)
    assert up.fsrs_due == NOW + dt.timedelta(days=want.interval_days)
    assert up.last_evidence_at == NOW                   # the decay clock moves only on evidence


def test_a_lapse_counts():
    up = next_progress(_seen(reps=3, lapses=1, stability=10.0, last_evidence_at=NOW - 12 * D),
                       GradedOutcome(0.0), NOW)
    assert up.fsrs_lapses == 2


# ---------------------------------------------------------------------------
# record_graded_event applies next_progress to the stored row
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="s@example.edu", name="S"))
    s.add(LearningGoal(id="AST101-L03-seasons", title="Seasons", description="", course="AST101", lecture_num=3))
    s.add(LearningGoal(id="AST101-L04-tides", title="Tides", description="", course="AST101", lecture_num=4))
    s.commit()
    yield s
    s.close()


def _row(db, goal="AST101-L03-seasons") -> StudentProgress:
    return db.query(StudentProgress).filter_by(student_id=1, goal_id=goal).one()


def test_record_graded_event_stores_the_transition(db):
    goal = "AST101-L03-seasons"
    with patch("tutor.agent.state._utcnow", return_value=NOW - 2 * D):
        for _ in range(state.MASTER_MIN_EVENTS):
            state.record_graded_event(db, 1, goal, 1.0, source="exam")
    first = _row(db)
    assert first.status == "mastered" and first.evidence_type == "graded"
    assert first.mastered_at == NOW - 2 * D and first.confirmed_at is None
    before = ProgressSnapshot(
        status=first.status, mastered_at=first.mastered_at, last_evidence_at=first.last_evidence_at,
        updated_at=first.updated_at, fsrs_stability=first.fsrs_stability,
        fsrs_difficulty=first.fsrs_difficulty, fsrs_reps=first.fsrs_reps, fsrs_lapses=first.fsrs_lapses,
        window=(0.85, 1.0, 1.0, 1.0, 1.0), n_events=5, retrievals=(NOW - 2 * D,) * 4 + (NOW,),
    )
    want = next_progress(before, GradedOutcome(0.85, fsrs_rating=sr.Rating.GOOD, upgrade_status=False), NOW)
    with patch("tutor.agent.state._utcnow", return_value=NOW):
        score = state.record_review_rating(db, 1, goal, sr.Rating.GOOD)
    row = _row(db)
    assert score == pytest.approx(want.mastery_score)
    assert (row.fsrs_stability, row.fsrs_reps, row.fsrs_due, row.confirmed_at) == \
        (want.fsrs_stability, want.fsrs_reps, want.fsrs_due, NOW)


# ---------------------------------------------------------------------------
# One review clock (B20)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("due,expected", [
    (None, False), (NOW - H, True), (NOW, True), (NOW + H, False),
])
def test_review_is_due(due, expected):
    assert state.review_is_due(due, NOW) is expected


def test_current_mastery_fades_on_the_fsrs_clock_when_it_can():
    fsrs_row = StudentProgress(mastery_score=0.9, fsrs_stability=2.0, last_evidence_at=NOW - 10 * D)
    assert state.current_mastery(fsrs_row, NOW) == pytest.approx(state.fsrs_effective_mastery(0.9, 2.0, 10.0))
    legacy = StudentProgress(mastery_score=0.9, updated_at=NOW - 10 * D)
    assert state.current_mastery(legacy, NOW) == pytest.approx(state.effective_mastery(0.9, NOW - 10 * D, NOW))


def test_every_reader_uses_the_one_clock(db):
    """The navigator, the progress summary, and /review agree on what is due:
    a row long past its last evidence but not yet at its FSRS due date is not
    due anywhere, and a row past its due date is due everywhere."""
    from tutor.agent.navigator import _review_due_ids
    from tutor.agent.progress import progress_summary
    from tutor.routers.review import due_rows

    db.add_all([
        StudentProgress(student_id=1, goal_id="AST101-L03-seasons", status="mastered", mastery_score=1.0,
                        last_evidence_at=NOW - 60 * D, fsrs_due=NOW + 5 * D),
        StudentProgress(student_id=1, goal_id="AST101-L04-tides", status="mastered", mastery_score=1.0,
                        last_evidence_at=NOW - 3 * D, fsrs_due=NOW - H),
    ])
    db.commit()
    with patch("tutor.agent.state._utcnow", return_value=NOW), \
         patch("tutor.agent.mastery._has_exam_questions", return_value=True):
        assert _review_due_ids(1, "AST101", db) == ["AST101-L04-tides"]
        assert [r.goal_id for r in due_rows(1, "AST101", db, now=NOW)] == ["AST101-L04-tides"]
        summary = progress_summary(1, db, "AST101")
    assert "Spaced review: 1 item due for retrieval practice: tides." in summary
    assert "seasons" not in summary.split("Spaced review:")[1]

    from tutor.class_metrics import compute_engagement
    from tutor.db.models import CourseClass

    course_class = CourseClass(name="AST 101 A", course="AST101", term="Autumn 2026", instructor_id=1)
    db.add(course_class)
    db.commit()
    engagement = compute_engagement(db, course_class, [db.get(Student, 1)],
                                    since=NOW - 7 * D, until=NOW, now=NOW)
    assert engagement[0].reviews_due == 1               # the class report agrees
