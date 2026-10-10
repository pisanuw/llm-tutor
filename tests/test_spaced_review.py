"""FSRS-4.5 spaced-repetition math (Engram integration, Phase 1).

Pure, network-free property tests of tutor/agent/spaced_review.py.
"""

from __future__ import annotations

import pytest

from tutor.agent import spaced_review as sr
from tutor.agent.spaced_review import FSRSState, Rating


def test_retrievability_is_one_at_zero_elapsed():
    assert sr.retrievability(0.0, 10.0) == pytest.approx(1.0)


def test_retrievability_equals_target_when_elapsed_equals_stability():
    # The forgetting curve is defined so R == 0.9 exactly when t == S.
    for s in (1.0, 5.0, 42.0):
        assert sr.retrievability(s, s) == pytest.approx(sr.REQUEST_RETENTION, abs=1e-9)


def test_retrievability_decreases_with_time_and_none_stability_is_zero():
    assert sr.retrievability(1.0, 10.0) > sr.retrievability(5.0, 10.0)
    assert sr.retrievability(5.0, None) == 0.0
    assert sr.retrievability(5.0, 0.0) == 0.0


def test_interval_for_inverts_retrievability():
    # Interval to reach the target retention should equal the stability.
    assert sr.interval_for(10.0) == pytest.approx(10.0, abs=1e-6)
    # A higher requested retention shortens the interval.
    assert sr.interval_for(10.0, 0.95) < sr.interval_for(10.0, 0.85)


def test_first_review_stability_is_the_weight_for_that_rating():
    for rating in Rating:
        r = sr.rate(FSRSState(), rating, 0.0)
        assert r.stability == pytest.approx(max(0.1, sr.DEFAULT_W[int(rating) - 1]))
        assert r.reps == 1
        assert 1.0 <= r.difficulty <= 10.0


def test_first_review_easy_beats_again_on_stability_and_interval():
    again = sr.rate(FSRSState(), Rating.AGAIN, 0.0)
    easy = sr.rate(FSRSState(), Rating.EASY, 0.0)
    assert easy.stability > again.stability
    assert easy.interval_days >= again.interval_days


def test_successful_review_grows_stability():
    first = sr.rate(FSRSState(), Rating.GOOD, 0.0)
    second = sr.rate(
        FSRSState(first.stability, first.difficulty, first.reps, first.lapses),
        Rating.GOOD,
        first.interval_days,
    )
    assert second.stability > first.stability
    assert second.reps == 2
    assert second.lapses == 0


def test_lapse_shrinks_stability_and_counts_a_lapse():
    strong = sr.rate(FSRSState(), Rating.EASY, 0.0)
    strong = sr.rate(FSRSState(strong.stability, strong.difficulty, 1, 0), Rating.GOOD, strong.interval_days)
    lapsed = sr.rate(
        FSRSState(strong.stability, strong.difficulty, strong.reps, strong.lapses),
        Rating.AGAIN,
        strong.interval_days,
    )
    assert lapsed.stability < strong.stability
    assert lapsed.lapses == strong.lapses + 1


def test_again_raises_difficulty_easy_lowers_it():
    base = sr.rate(FSRSState(), Rating.GOOD, 0.0)
    st = FSRSState(base.stability, base.difficulty, base.reps, base.lapses)
    harder = sr.rate(st, Rating.AGAIN, base.interval_days)
    easier = sr.rate(st, Rating.EASY, base.interval_days)
    assert harder.difficulty > base.difficulty
    assert easier.difficulty < base.difficulty


def test_intervals_are_clamped_to_valid_range():
    r = sr.rate(FSRSState(), Rating.AGAIN, 0.0)
    assert sr.MIN_INTERVAL <= r.interval_days <= sr.MAX_INTERVAL


def test_exam_score_rating_mapping():
    assert sr.rating_from_exam_score(0) is Rating.AGAIN
    assert sr.rating_from_exam_score(1) is Rating.HARD
    assert sr.rating_from_exam_score(2) is Rating.GOOD
    assert sr.rating_from_exam_score(3) is Rating.EASY


def test_fraction_rating_mapping_is_monotone():
    ratings = [sr.rating_from_fraction(f) for f in (0.0, 0.3, 0.7, 1.0)]
    assert ratings == [Rating.AGAIN, Rating.HARD, Rating.GOOD, Rating.EASY]


def test_default_weights_are_the_fsrs_45_seventeen():
    assert len(sr.DEFAULT_W) == 17
    assert sr.DECAY == -0.5
    assert sr.FACTOR == pytest.approx(19 / 81)
