"""Policy core: per-turn decisions from trusted state only.

This module is pure Python. It makes zero LLM calls and reads zero student
prose. It imports nothing that can carry student text (in particular, no
``anthropic``). Every decision is a function of:

  - trusted learner state (mastery, exam status) -- never the student's words,
  - the classified ``Intent`` enum (produced upstream by the pre-classifier),
  - deterministic facts (goal scope, prerequisite gate, code-adjacency),
  - instructor toggles (explain mode, exercises allowed).

That is what makes it injection-proof (P2): a student cannot talk their way
past a ceiling because the ceiling is decided from state the student cannot
write. Per P4, an aggressive intent (e.g. SOLUTION_DEMAND) never *raises*
restrictions on its own; we route kindly rather than punish.

The rung ladder H0-H7 ceiling is computed here and rendered into the actor's
per-turn envelope by :func:`tutor.agent.prompt_builder.render_envelope`.

Encode policy as DATA (the band table, the constants) rather than nested ifs,
so it is tunable and exhaustively table-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Intent(str, Enum):
    """Classified student intent (output of the Task 4 pre-classifier).

    Stored as a string enum so it serialises cleanly into telemetry, but the
    policy core only ever compares enum members, never free text.
    """

    SOLUTION_DEMAND = "SOLUTION_DEMAND"
    EXAM_LEAK = "EXAM_LEAK"
    INJECTION = "INJECTION"
    HOSTILE = "HOSTILE"
    ABUSIVE = "ABUSIVE"
    QUESTION = "QUESTION"
    ATTEMPT = "ATTEMPT"
    OTHER = "OTHER"


class Move(str, Enum):
    """Instructional move vocabulary v1 (Session 9, supervisor 2).

    The strategist proposes exactly one per turn; the policy core disposes
    (merge_move). A move shapes HOW the actor opens the turn; it can never
    change WHAT it is allowed to reveal (the rung ceiling).
    """

    ELICIT_PREDICTION = "ELICIT_PREDICTION"
    PROBE_PRIOR = "PROBE_PRIOR"
    WORKED_ANALOGY = "WORKED_ANALOGY"
    LOCATED_OBSERVATION = "LOCATED_OBSERVATION"
    SELF_EXPLAIN = "SELF_EXPLAIN"
    CHECK_UNDERSTANDING = "CHECK_UNDERSTANDING"
    ENCOURAGE_ATTEMPT = "ENCOURAGE_ATTEMPT"
    SUGGEST_REVIEW = "SUGGEST_REVIEW"
    ADVANCE = "ADVANCE"
    MODALITY_SWITCH = "MODALITY_SWITCH"
    # Faded worked examples (pedagogy item 10): the ladder for a goal is
    # WORKED_ANALOGY -> COMPLETION_PROBLEM -> FULL_PROBLEM; fading_stage()
    # below picks the rung from mastery state.
    COMPLETION_PROBLEM = "COMPLETION_PROBLEM"
    FULL_PROBLEM = "FULL_PROBLEM"


# What each move asks the actor to do this turn (rendered once into the
# cached prefix; the per-turn envelope names just the chosen move).
MOVE_GUIDANCE: dict[Move, str] = {
    Move.ELICIT_PREDICTION: "open by asking the student to predict what happens before any explanation",
    Move.PROBE_PRIOR: "open by probing what the student already knows about the prerequisite idea",
    Move.WORKED_ANALOGY: "walk through a worked example on a DIFFERENT problem than the student's",
    Move.LOCATED_OBSERVATION: "point the student at the exact slide/source line and ask what they notice",
    Move.SELF_EXPLAIN: "ask the student to explain the idea back in their own words",
    Move.CHECK_UNDERSTANDING: "ask one quick check question before moving on",
    Move.ENCOURAGE_ATTEMPT: "warmly push the student to try a first step themselves now",
    Move.SUGGEST_REVIEW: "suggest revisiting the earlier topic this difficulty traces back to",
    Move.ADVANCE: "acknowledge mastery and pull the student forward to the next idea",
    Move.MODALITY_SWITCH: "switch representation: diagram, table, code trace, or plain-words restatement",
    Move.COMPLETION_PROBLEM: (
        "give a PARTIALLY worked solution on a related problem: the setup and the "
        "first steps shown, one key step left blank for the student to complete; "
        "never the whole thing"
    ),
    Move.FULL_PROBLEM: (
        "pose the full problem for the student to solve unaided: no worked steps, "
        "no skeleton; offer a hint only if they ask"
    ),
}

# A move that implies SHOWING content needs the ceiling to permit it; all
# other moves are questions/encouragement and work at any rung.
MOVE_MIN_RUNG: dict[Move, int] = {
    Move.WORKED_ANALOGY: 5,       # a worked example is H5 territory
    Move.COMPLETION_PROBLEM: 5,   # partial solution / pseudocode is H5 too
    Move.LOCATED_OBSERVATION: 2,  # pointing at a slide is H2
}


# ---------------------------------------------------------------------------
# Faded worked examples (pedagogy item 10)
# ---------------------------------------------------------------------------

# Stage thresholds over the goal's recent graded successes (0..1). Below FADE_LOW
# the student has not shown they can do it: show a worked example. Between:
# a completion problem (partial worked example with a gap). At/above FADE_HIGH
# or after an exam pass: the full problem, unaided. A worked example already
# shown for this goal (rung 5+ used) advances a no-evidence goal to completion,
# so the ladder moves even before the first graded attempt.
FADE_LOW = 0.5
FADE_HIGH = 0.75
FADE_RECENT = 2  # graded events averaged for the stage


def fading_stage(
    graded_scores: "tuple[float, ...] | list[float]",
    rungs_used: "tuple[int, ...] | list[int]",
    exam_passes: int = 0,
) -> str:
    """'worked' | 'completion' | 'full' for a goal, from its recent evidence."""
    if exam_passes > 0:
        return "full"
    recent = list(graded_scores)[-FADE_RECENT:]
    if not recent:
        return "completion" if any(r >= 5 for r in rungs_used) else "worked"
    mean = sum(recent) / len(recent)
    if mean >= FADE_HIGH:
        return "full"
    if mean >= FADE_LOW:
        return "completion"
    return "worked"


FADING_MOVE: dict[str, Move] = {
    "worked": Move.WORKED_ANALOGY,
    "completion": Move.COMPLETION_PROBLEM,
    "full": Move.FULL_PROBLEM,
}


# ---------------------------------------------------------------------------
# Tunable policy data (not control flow)
# ---------------------------------------------------------------------------

# Hint-ladder ceiling by mastery band, highest threshold first. Lower mastery
# means a higher ceiling (more scaffolding allowed); a confident student is
# held to a low rung so we do not over-explain. Each row is
# (min_mastery_inclusive, max_rung).
RUNG_BANDS: tuple[tuple[float, int], ...] = (
    (0.85, 2),
    (0.70, 3),
    (0.40, 4),
    (0.00, 5),
)

# Floor for a constructive code turn (a genuine ATTEMPT, or a QUESTION/other that
# shows code and asks for debugging help). The student is stuck on execution, not
# concept, so the mastery-band ceiling (tuned to avoid over-explaining concepts to
# a confident student) is too low to give a targeted debugging hint: a mastered
# student gets H2 ("point at the concept"), but a real nudge ("what stops your
# loop when n hits 1?") is H3-H4, which the judge then revises as
# over-the-rung. So such a turn floors the ceiling at H4 ("describe the approach
# in words, NO code"): the actor can give a real verbal nudge, no solution leaks
# through (the detector still cuts code, and H5+ worked examples / pseudocode stay
# above the floor). Applied BEFORE the exam-lockdown and conduct caps, so those
# still tighten it back down. SOLUTION_DEMAND and the adversarial intents never
# lift the ceiling (P4: route kindly, do not reward the demand).
#
# Why QUESTION counts (2026-06-14 live finding): the classifier labels "here's my
# code, what's wrong?" as QUESTION as often as ATTEMPT, and the ATTEMPT-only floor
# left those at H2, where the actor's verbal help was revised. The floor keys on
# the constructive intent + code-adjacency, not on ATTEMPT alone.
ATTEMPT_RUNG_FLOOR = 4

# Constructive intents that earn the floor when the turn is code-adjacent (an
# ATTEMPT always is). Adversarial intents are excluded (P4).
_FLOOR_INTENTS = frozenset({Intent.ATTEMPT, Intent.QUESTION, Intent.OTHER})

# Ceiling for a goal that is under an active mastery exam: explain almost
# nothing, the student must demonstrate it themselves.
EXAM_LOCKDOWN_RUNG = 1

# Instructor "explain mode": full worked solutions allowed (H7). Never
# overrides an active exam lockdown.
EXPLAIN_MODE_RUNG = 7

# Conduct restriction (Session 10, S24): a restricted student gets boundary
# replies only until the instructor clears it. Applied LAST, so neither
# explain mode nor anything else can lift it.
RESTRICTED_RUNG = 1


# Human-readable hint ladder H0-H7. The full legend is rendered once into the
# cached system prompt; the per-turn envelope only names the ceiling number.
RUNG_LADDER: dict[int, str] = {
    0: "acknowledge and encourage; add no new content",
    1: "restate or clarify the problem in your own words",
    2: "point to the relevant concept, slide, or definition",
    3: "ask a leading question about the very next step",
    4: "describe the approach in words (no code)",
    5: "walk through a worked example on a DIFFERENT problem",
    6: "give pseudo-code with the key lines left as blanks (___)",
    7: "show the full, compilable solution",
}


def rung_for_mastery(mastery: float) -> int:
    """Return the hint-ladder ceiling for a single goal's mastery (0..1)."""
    for threshold, rung in RUNG_BANDS:
        if mastery >= threshold:
            return rung
    # Unreachable: the last band has threshold 0.0, so the loop always returns.
    raise AssertionError("RUNG_BANDS must include a 0.0 threshold")


# ---------------------------------------------------------------------------
# Inputs (trusted state only -- no student prose)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalState:
    """A student's trusted state for one learning goal."""

    goal_id: str
    mastery: float = 0.0          # 0..1 (windowed success rate; Task 5 fills this)
    status: str = "locked"        # locked | unlocked | mastered (enum-like id)
    exam_active: bool = False      # a pending mastery exam covers this goal


@dataclass(frozen=True)
class StudentState:
    """The subset of learner state the policy core is allowed to see."""

    goals: tuple[GoalState, ...] = ()

    def get(self, goal_id: str) -> "GoalState | None":
        for g in self.goals:
            if g.goal_id == goal_id:
                return g
        return None


@dataclass(frozen=True)
class DeterministicFacts:
    """Facts established without reading student prose."""

    # Goals in scope this turn: from the classifier (preferred) or, as a
    # fallback, slide metadata of the content the student is looking at.
    scope_goal_ids: tuple[str, ...] = ()
    # Prerequisite that must be gated before this content, computed from
    # trusted progress (mastery.check_prerequisite_gate), or None.
    gate_goal_id: "str | None" = None
    # The turn is about runnable code (raises judge scrutiny downstream).
    code_adjacent: bool = False
    # The student is working a live exercise/assignment (no full solutions).
    on_live_exercise: bool = False
    # Conduct restriction in force (set by the conduct ladder, cleared only
    # by the instructor): caps the ceiling at RESTRICTED_RUNG.
    restricted: bool = False


@dataclass(frozen=True)
class Toggles:
    """Instructor-controlled switches, read from the settings table."""

    explain_mode: bool = False    # allow H7 full solutions
    allow_exercise: bool = True


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Envelope:
    max_rung: int             # 0..7, ceiling for this turn
    goals: list[str]          # goal ids in scope this turn
    exam_lockdown: list[str]  # goal ids under active exam: no teaching on these
    must_ground: bool         # actor must use find_topic results
    allow_exercise: bool
    escalate_review: bool     # post-judge runs 3-sample majority
    reason: str               # human-readable, for telemetry
    # Supervisor 2 (Session 9): the strategist's instructional move for this
    # turn, attached ONLY via merge_move. None = actor's choice.
    move: "Move | None" = None
    move_reason: str = ""
    # The mastery-band ceiling before the floors and caps below moved it, so
    # the telemetry can show "H2 -> H4" rather than only the enforced number
    # (the Supervision tab's Rung cell). None on a row written before this
    # field existed. max_rung stays the one that was enforced.
    rung_base: "int | None" = None


def merge_move(envelope: Envelope, move: "Move | None", reason: str = "") -> Envelope:
    """Attach the strategist's proposed move to the envelope. Pure, supreme.

    The policy core disposes: a move can NEVER raise max_rung (structurally
    impossible here, the field is untouched), never overrides an exam
    lockdown (no move at all during one), and a content-bearing move that
    the ceiling cannot accommodate is dropped (actor's choice instead).
    """
    if move is None:
        return envelope
    if envelope.exam_lockdown:
        return envelope
    if MOVE_MIN_RUNG.get(move, 0) > envelope.max_rung:
        return envelope
    return replace(envelope, move=move, move_reason=reason[:200])


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def compute_envelope(
    student_state: StudentState,
    intent: Intent,
    deterministic_facts: DeterministicFacts,
    toggles: Toggles,
) -> Envelope:
    """Compute the per-turn envelope from trusted state. Pure.

    The ceiling is the most permissive band among the in-scope goals (we meet
    the student at their weakest goal, where they need the most scaffolding),
    floored for a genuine ATTEMPT so debugging help fits (ATTEMPT_RUNG_FLOOR),
    then tightened by exam lockdown, then optionally lifted by explain mode.
    Intent is recorded but, per P4, an aggressive intent never tightens the
    ceiling on its own (and never lifts the attempt floor).
    """
    facts = deterministic_facts
    scope: list[str] = list(facts.scope_goal_ids)
    # A gated prerequisite is in scope: the student needs it to proceed.
    if facts.gate_goal_id and facts.gate_goal_id not in scope:
        scope.append(facts.gate_goal_id)

    reasons: list[str] = []

    # 1. Base ceiling from the weakest in-scope goal (max rung = most help).
    if scope:
        ceilings = []
        for gid in scope:
            gs = student_state.get(gid)
            ceilings.append(rung_for_mastery(gs.mastery if gs else 0.0))
        base_rung = max(ceilings)
        reasons.append(f"mastery band ceiling H{base_rung}")
    else:
        base_rung = rung_for_mastery(0.0)
        reasons.append(f"no goals in scope; default ceiling H{base_rung}")

    max_rung = base_rung

    # 1b. A constructive code turn floors the ceiling so a targeted debugging
    #     hint fits under it (see ATTEMPT_RUNG_FLOOR). An ATTEMPT always counts; a
    #     QUESTION/other counts when it is code-adjacent (showed code). Applied
    #     before the lockdown and conduct caps below, which still tighten it back.
    constructive_code_turn = intent == Intent.ATTEMPT or (
        facts.code_adjacent and intent in _FLOOR_INTENTS
    )
    if constructive_code_turn and max_rung < ATTEMPT_RUNG_FLOOR:
        max_rung = ATTEMPT_RUNG_FLOOR
        reasons.append(f"constructive code turn floors ceiling at H{ATTEMPT_RUNG_FLOOR}")

    # 2. Exam lockdown: any in-scope goal under an active exam caps the turn.
    exam_lockdown = sorted(
        g.goal_id
        for g in student_state.goals
        if g.exam_active and g.goal_id in scope
    )
    if exam_lockdown:
        max_rung = min(max_rung, EXAM_LOCKDOWN_RUNG)
        reasons.append(f"exam lockdown {exam_lockdown} caps H{EXAM_LOCKDOWN_RUNG}")

    # 3. Instructor explain mode lifts the ceiling to H7, but never past an
    #    active exam lockdown.
    if toggles.explain_mode and not exam_lockdown:
        max_rung = EXPLAIN_MODE_RUNG
        reasons.append("explain_mode: H7 allowed")

    # 3b. Conduct restriction caps LAST: explain mode cannot lift it, and it
    #     stacks under an exam lockdown's even lower ceiling if both apply.
    if facts.restricted:
        max_rung = min(max_rung, RESTRICTED_RUNG)
        reasons.append(f"conduct restriction caps H{RESTRICTED_RUNG}")

    # 4. P4: aggressive intent does not change the rung. Route kindly.
    if intent == Intent.SOLUTION_DEMAND:
        reasons.append("solution demand: ceiling unchanged (route kindly)")

    # 5. Ground course claims whenever a real topic is in play.
    must_ground = bool(scope) or facts.code_adjacent

    # 6. Exercises: instructor toggle, suppressed during an exam lockdown.
    allow_exercise = toggles.allow_exercise and not exam_lockdown

    # 7. Escalate the post-judge to a 3-sample majority on risky turns.
    escalate_review = bool(exam_lockdown) or intent in (
        Intent.SOLUTION_DEMAND,
        Intent.EXAM_LEAK,
        Intent.INJECTION,
    )
    if escalate_review:
        reasons.append("escalated review (3-sample majority)")

    return Envelope(
        max_rung=max_rung,
        goals=scope,
        exam_lockdown=exam_lockdown,
        must_ground=must_ground,
        allow_exercise=allow_exercise,
        escalate_review=escalate_review,
        reason="; ".join(reasons),
        rung_base=base_rung,
    )
