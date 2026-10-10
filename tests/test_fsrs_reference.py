"""Plan step 1.10 (findings B10, B11, B12): the mastery clock and FSRS.

B12: the FSRS-4.5 port reverted difficulty toward the AGAIN difficulty and
moved stability with the new difficulty, so intervals grew about 40 percent
slower than the reference. The sequences below come from py-fsrs 3.1.0
(FSRS-5, whose long-term review formulas are FSRS-4.5's) run with this
module's 17 weights plus two unused short-term weights, reviewing each time
on the day the previous interval fell due:

    from fsrs.fsrs import FSRS                     # pip install fsrs==3.1.0
    f = FSRS(w=DEFAULT_W + (0.5034, 0.6567))
    S, D = f.init_stability(r0), f.init_difficulty(r0)
    then per review: R = f.forgetting_curve(ivl, S);
    S' = f.next_recall_stability(D, S, R, r) or f.next_forget_stability(D, S, R);
    D' = f.next_difficulty(D, r); ivl = f.next_interval(S')

B10: confirmation and the exam attempt cap used UTC calendar dates, which
roll over at 4 or 5 pm in Seattle. B11: a shaky review (HARD, 0.6) counted as
a successful retrieval and as retained.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from tutor.agent import mastery, spaced_review as sr, state
from tutor.db.models import LearningGoal, MasteryAttempt, Student, StudentProgress
from tests.helpers import make_session

G, H, A, E = sr.Rating.GOOD, sr.Rating.HARD, sr.Rating.AGAIN, sr.Rating.EASY

# (stability, difficulty, interval_days) after each review, from py-fsrs 3.1.0.
REFERENCE = {
    "good x6": ([G] * 6, [
        (3.0412, 4.4909, 3), (13.0913, 4.2666, 13), (48.1585, 4.0575, 48),
        (154.9373, 3.8624, 155), (445.0556, 3.6804, 445), (1158.0778, 3.5108, 1158),
    ]),
    "again then good x4": ([A] + [G] * 4, [
        (0.4197, 7.1434, 1), (2.8806, 6.7403, 3), (9.484, 6.3644, 9),
        (27.2214, 6.0138, 27), (74.8069, 5.6869, 75),
    ]),
    "mixed": ([H, G, G, E, A, G], [
        (1.1869, 6.2323, 1), (4.1103, 5.8906, 4), (14.1131, 5.572, 14),
        (102.6805, 4.3415, 103), (6.7292, 5.9938, 7), (22.3909, 5.6682, 22),
    ]),
}


def _run(ratings):
    st = sr.FSRSState()
    elapsed = 0.0
    out = []
    for rating in ratings:
        rv = sr.rate(st, rating, elapsed)
        out.append((round(rv.stability, 4), round(rv.difficulty, 4), rv.interval_days))
        st = sr.FSRSState(stability=rv.stability, difficulty=rv.difficulty,
                          reps=rv.reps, lapses=rv.lapses)
        elapsed = rv.interval_days
    return out


@pytest.mark.parametrize("name", list(REFERENCE))
def test_interval_sequence_matches_py_fsrs(name):
    ratings, expected = REFERENCE[name]
    got = _run(ratings)
    for (s, d, ivl), (es, ed, eivl) in zip(got, expected):
        assert s == pytest.approx(es, abs=1e-3)
        assert d == pytest.approx(ed, abs=1e-3)
        assert ivl == eivl
    assert len(got) == len(expected)


def test_difficulty_reverts_toward_easy_not_again():
    easy_d = sr.init_difficulty(E)
    again_d = sr.init_difficulty(A)
    d = 5.0
    for _ in range(50):
        d = sr.next_difficulty(d, G)          # GOOD leaves only the reversion
    assert abs(d - easy_d) < abs(d - again_d)


# ---------------------------------------------------------------------------
# B10 and B11: the confirmation clock and the success bar
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="s@example.edu", name="S"))
    s.add(LearningGoal(id="g1", title="G1", description="", course="AST101", lecture_num=1))
    s.commit()
    yield s
    s.close()


def _at(when):
    return patch("tutor.agent.state._utcnow", return_value=when)


def _master_at(db, when):
    with _at(when):
        for _ in range(state.MASTER_MIN_EVENTS):
            state.record_graded_event(db, 1, "g1", 1.0, source="exam")


def _row(db):
    return db.query(StudentProgress).filter_by(student_id=1, goal_id="g1").one()


def test_an_hour_across_midnight_does_not_confirm(db):
    _master_at(db, datetime.datetime(2026, 10, 5, 23, 30))
    with _at(datetime.datetime(2026, 10, 6, 0, 30)):         # next UTC date, 1 h later
        state.record_graded_event(db, 1, "g1", 1.0, source="review")
    assert _row(db).confirmed_at is None


def test_sixteen_hours_on_one_utc_date_confirms(db):
    _master_at(db, datetime.datetime(2026, 10, 5, 0, 30))
    with _at(datetime.datetime(2026, 10, 5, 16, 45)):        # same UTC date, 16 h later
        state.record_graded_event(db, 1, "g1", 1.0, source="review")
    assert _row(db).confirmed_at is not None


def test_a_shaky_review_is_not_a_successful_retrieval(db):
    _master_at(db, datetime.datetime(2026, 10, 1, 9, 0))
    with _at(datetime.datetime(2026, 10, 4, 9, 0)):
        state.record_review_rating(db, 1, "g1", sr.Rating.HARD)   # 0.6
    assert _row(db).confirmed_at is None
    with _at(datetime.datetime(2026, 10, 5, 9, 0)):
        state.record_review_rating(db, 1, "g1", sr.Rating.GOOD)   # 0.85
    assert _row(db).confirmed_at is not None


def test_a_shaky_review_is_not_retained():
    from tutor.learning_metrics import RETAINED_THRESHOLD

    assert 0.6 < RETAINED_THRESHOLD <= 0.85
    assert state.RETRIEVAL_SUCCESS == RETAINED_THRESHOLD


def test_attempt_window_is_rolling_across_midnight(db):
    now = datetime.datetime(2026, 10, 6, 0, 30)
    a = MasteryAttempt(student_id=1, goal_id="g1", questions_json="{}", status="failed",
                       completed_at=datetime.datetime(2026, 10, 5, 23, 45))   # 45 min ago
    b = MasteryAttempt(student_id=1, goal_id="g1", questions_json="{}", status="failed",
                       completed_at=datetime.datetime(2026, 10, 4, 23, 0))    # 25.5 h ago
    db.add_all([a, b])
    db.commit()
    with patch("tutor.agent.mastery.utcnow", return_value=now):
        assert mastery.attempts_today(1, "g1", db) == 1       # the UTC date would say 0
        assert mastery.hours_until_reopen(1, "g1", db) == 24  # 23 h 15 min, rounded up
