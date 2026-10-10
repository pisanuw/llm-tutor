"""FSRS-4.5 spaced-repetition scheduling (pure math, stdlib only).

Phase 1 of the Engram integration.
Replaces the fixed 21-day linear decay in ``state.effective_mastery`` with a
per-item stability/difficulty model fitted from the student's own rating
history, so a goal demonstrated well many times decays far slower than one
barely passed once, and the "mastery scalar" fed to ``policy.rung_for_mastery``
becomes the model's predicted recall probability rather than a flat clock.

Attribution
-----------
Direct port of the FSRS-4.5 algorithm. Attribution chain (all MIT-licensed):
open-spaced-repetition (authored the FSRS-4.5 weights + formulas) -> Engram
(``nagisanzenin/engram``, ``scripts/engram.py``, which adopted them) ->
this module. The 17 default weights below are the
open-spaced-repetition FSRS-4.5 defaults.

Design notes (matching this repo's conventions)
-----------------------------------------------
- Pure: no LLM calls, no DB, no I/O, no clock reads. Every function is a
  deterministic function of its arguments, so it is exhaustively unit-testable
  and cannot leak or be injected. The model "never does calendar math" via an
  LLM (Engram's P1) is enforced structurally here: the math IS the code.
- Stability and elapsed time are in DAYS. Difficulty is on [1, 10].
- Ratings use the standard FSRS 4-band scale (again/hard/good/easy).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

# ---------------------------------------------------------------------------
# Constants (FSRS-4.5 defaults from open-spaced-repetition, via Engram)
# ---------------------------------------------------------------------------

# 17 default weights. Anki/FSRS optimises these per-user from review logs; we
# ship the population defaults (no per-user optimisation in Phase 1).
DEFAULT_W: tuple[float, ...] = (
    0.4197, 1.1869, 3.0412, 15.2441, 7.1434, 0.6477, 1.0007, 0.0674, 1.6597,
    0.1712, 1.1178, 2.0225, 0.0904, 0.3025, 2.1214, 0.2498, 2.9466,
)

# Forgetting-curve shape. R(t) = (1 + FACTOR * t / S) ** DECAY, chosen so that
# R = 0.9 exactly when t == S (the "stability = days until 90% recall" identity).
DECAY: float = -0.5
FACTOR: float = 0.9 ** (1.0 / DECAY) - 1.0  # = 19/81 ~= 0.2345679

# Target retention the scheduler aims for when it sets the next due date.
REQUEST_RETENTION: float = 0.9

# Interval clamps (days).
MIN_INTERVAL: int = 1
MAX_INTERVAL: int = 36500  # ~100 years; FSRS default ceiling

# Difficulty clamps.
_D_MIN: float = 1.0
_D_MAX: float = 10.0


class Rating(IntEnum):
    """FSRS grade for one review. Higher = better recall."""

    AGAIN = 1  # failed / forgot
    HARD = 2   # recalled with serious difficulty
    GOOD = 3   # recalled correctly
    EASY = 4   # recalled effortlessly


@dataclass(frozen=True)
class FSRSState:
    """A goal's persisted FSRS state (mirrors the StudentProgress columns).

    ``stability`` and ``difficulty`` are None before the first review; ``reps``
    is then 0, which routes :func:`rate` through the first-review initialisers.
    """

    stability: float | None = None
    difficulty: float | None = None
    reps: int = 0
    lapses: int = 0


@dataclass(frozen=True)
class Review:
    """The result of one review: updated state + the next interval in days."""

    stability: float
    difficulty: float
    reps: int
    lapses: int
    interval_days: int


# ---------------------------------------------------------------------------
# Forgetting curve + interval
# ---------------------------------------------------------------------------

def retrievability(elapsed_days: float, stability: float | None) -> float:
    """Predicted probability of recall ``elapsed_days`` after the last review.

    Returns 1.0 at elapsed 0 and decays toward 0 as time passes; the rate is set
    by ``stability`` (bigger stability = slower decay). Returns 0.0 when there is
    no stability yet (never reviewed) so an un-practised goal reads as "needs
    the most help". Pure.
    """
    if stability is None or stability <= 0.0:
        return 0.0
    t = max(0.0, elapsed_days)
    return (1.0 + FACTOR * t / stability) ** DECAY


def interval_for(stability: float, request_retention: float = REQUEST_RETENTION) -> float:
    """Days until predicted recall falls to ``request_retention``. Pure, unclamped."""
    ivl = (stability / FACTOR) * (request_retention ** (1.0 / DECAY) - 1.0)
    return max(0.0, ivl)


def _clamp_interval(ivl_days: float) -> int:
    return max(MIN_INTERVAL, min(MAX_INTERVAL, int(round(ivl_days))))


# ---------------------------------------------------------------------------
# First-review initialisers
# ---------------------------------------------------------------------------

def init_stability(rating: Rating, w: tuple[float, ...] = DEFAULT_W) -> float:
    """Stability after the very first review at ``rating`` (w0..w3)."""
    return max(0.1, w[int(rating) - 1])


def init_difficulty(rating: Rating, w: tuple[float, ...] = DEFAULT_W) -> float:
    """Difficulty after the very first review at ``rating`` (mean-reversion target)."""
    d = w[4] - math.exp(w[5] * (int(rating) - 1)) + 1.0
    return _constrain_difficulty(d)


# ---------------------------------------------------------------------------
# Subsequent-review transitions
# ---------------------------------------------------------------------------

def _constrain_difficulty(d: float) -> float:
    return min(_D_MAX, max(_D_MIN, d))


def _mean_reversion(target: float, current: float, w: tuple[float, ...]) -> float:
    return w[7] * target + (1.0 - w[7]) * current


def next_difficulty(d: float, rating: Rating, w: tuple[float, ...] = DEFAULT_W) -> float:
    """Difficulty after a subsequent review, with mean reversion toward the
    initial difficulty of an EASY first review, as FSRS-4.5 specifies.

    The port used to revert toward w4, which under the FSRS-4.5 initialiser is
    the AGAIN difficulty: every review pulled difficulty up, intervals grew
    about 40 percent slower, and goals came due too often (plan step 1.10,
    finding B12). Pinned against py-fsrs in tests/test_fsrs_reference.py.
    """
    next_d = d - w[6] * (int(rating) - 3)
    return _constrain_difficulty(_mean_reversion(init_difficulty(Rating.EASY, w), next_d, w))


def next_stability_recall(
    d: float, s: float, r: float, rating: Rating, w: tuple[float, ...] = DEFAULT_W
) -> float:
    """New stability after a *successful* review (rating >= HARD)."""
    hard_penalty = w[15] if rating == Rating.HARD else 1.0
    easy_bonus = w[16] if rating == Rating.EASY else 1.0
    return s * (
        1.0
        + math.exp(w[8])
        * (11.0 - d)
        * (s ** -w[9])
        * (math.exp((1.0 - r) * w[10]) - 1.0)
        * hard_penalty
        * easy_bonus
    )


def next_stability_forget(
    d: float, s: float, r: float, w: tuple[float, ...] = DEFAULT_W
) -> float:
    """New stability after a *lapse* (rating == AGAIN)."""
    return (
        w[11]
        * (d ** -w[12])
        * (((s + 1.0) ** w[13]) - 1.0)
        * math.exp((1.0 - r) * w[14])
    )


# ---------------------------------------------------------------------------
# The one public entry point
# ---------------------------------------------------------------------------

def rate(
    state: FSRSState,
    rating: Rating,
    elapsed_days: float,
    request_retention: float = REQUEST_RETENTION,
    w: tuple[float, ...] = DEFAULT_W,
) -> Review:
    """Apply one review outcome and return the updated state + next interval.

    ``elapsed_days`` is the time since the last review (ignored on the first
    review). Pure: the caller stamps the due date from ``interval_days`` and
    persists the result through the single-writer state module.
    """
    rating = Rating(int(rating))
    if state.stability is None or state.reps <= 0:
        stability = init_stability(rating, w)
        difficulty = init_difficulty(rating, w)
    else:
        prev_d = state.difficulty if state.difficulty is not None else init_difficulty(Rating.GOOD, w)
        r = retrievability(elapsed_days, state.stability)
        # Stability moves with the difficulty BEFORE this review, as in the
        # FSRS optimizer and py-fsrs; the new difficulty applies next time.
        if rating == Rating.AGAIN:
            stability = next_stability_forget(prev_d, state.stability, r, w)
        else:
            stability = next_stability_recall(prev_d, state.stability, r, rating, w)
        difficulty = next_difficulty(prev_d, rating, w)

    stability = max(0.1, stability)
    interval = _clamp_interval(interval_for(stability, request_retention))
    return Review(
        stability=stability,
        difficulty=difficulty,
        reps=state.reps + 1,
        lapses=state.lapses + (1 if rating == Rating.AGAIN else 0),
        interval_days=interval,
    )


# ---------------------------------------------------------------------------
# Score -> Rating mappings (the graders speak 0-3 / 0-1, FSRS speaks 1-4)
# ---------------------------------------------------------------------------

def rating_from_exam_score(score: int) -> Rating:
    """Map a mastery-check score (0-3 questions correct) to an FSRS rating."""
    return {0: Rating.AGAIN, 1: Rating.HARD, 2: Rating.GOOD}.get(int(score), Rating.EASY)


def rating_from_fraction(success: float) -> Rating:
    """Map a 0..1 graded-success fraction to an FSRS rating.

    Used to derive a rating for graded events (exams, exercises) that only carry
    a success fraction, so FSRS state stays populated from every evidence source,
    not just the dedicated review loop.
    """
    if success < 0.25:
        return Rating.AGAIN
    if success < 0.6:
        return Rating.HARD
    if success < 0.85:
        return Rating.GOOD
    return Rating.EASY
