"""Build the tutor's system prompt from courses.yaml and the persona files.

Reads the source files below at startup and concatenates them into a single
system prompt.  Files are read once and cached; restart the server to
pick up changes.

Source files (in order), all under the content root (tutor/paths.py):
1. persona/persona-distilled.md              -- core persona rules
2. persona/communication.md                  -- voice, brevity, humor
3. persona/teaching.md                       -- teaching philosophy and method
4. instructor-profile/transcripts-voice.md   -- lecture voice from real transcripts
5. instructor-profile/lecture-flow.md        -- the structural arc of a session
6. instructor-profile/teaching-patterns.md   -- pedagogical signatures + grading stance
7. instructor-profile/examples-library.md    -- the instructor's recurring examples
8. instructor-profile/topic-explanations.md  -- how the instructor explains each topic

A missing file is noted in the prompt as missing (so the gap is visible in a
transcript), never an error: a new tutor runs with only courses.yaml.
ADDING-KNOWLEDGE.md, step 2, says what each file should hold.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from tutor.agent.playbook import actor_move_vocabulary as _actor_vocab
from tutor.agent.policy import MOVE_GUIDANCE, RUNG_LADDER, Envelope
from tutor.config import get_config
from tutor.courses import COURSE_LIST, IDENTITY

def _course_list() -> str:
    names = [c.full_title for c in COURSE_LIST]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _preamble() -> str:
    """Who the tutor is, from courses.yaml. Part of the cached prefix: it
    changes only when courses.yaml does (and the server restarts)."""
    who = IDENTITY.instructor
    at = f" at {IDENTITY.institution}" if IDENTITY.institution else ""
    code_tool = (
        "- evaluate_code: scores code the student submits and returns feedback and one "
        "leading hint, never corrected code\n"
    )
    return f"""You are {IDENTITY.name}, the digital teaching assistant for {who}'s \
{IDENTITY.subject} courses{at}: {_course_list()}.

You teach as {IDENTITY.instructor_short}, not as a generic AI tutor. Respond using their voice, \
their examples, their teaching patterns. Use the persona and style rules below exactly.

You have access to four tools:
- find_topic: queries the course knowledge base (slides, transcripts, textbook, assignments, notes)
- get_lecture_outline: returns the plan for a specific lecture
- get_exercise: fetches a practice problem from the course exercise bank
{code_tool}
When a student asks about a concept, ALWAYS call find_topic first to ground your answer \
in actual course material before responding. Show the slide reference when you use it.

Never do the student's homework. Guide them to the answer instead.
"""


_FOOTER = """
---
TOOL USE RULES:
- Call find_topic for any concept question before answering.
- Call get_lecture_outline when the student asks what a lecture covers.
- Call get_exercise when the student asks for practice or a problem to try.
- Call evaluate_code when the student submits code for feedback; relay its feedback and hint, never a fix.
- After calling a tool, synthesise the results into a short response.
- If tool results are insufficient, say so honestly and answer from knowledge.
"""


def _hint_ladder_block() -> str:
    """Static supervised-tutor discipline + the H0-H7 ladder legend.

    Lives in the cached prefix (stable across turns). The per-turn envelope
    rendered by :func:`render_envelope` only names the ceiling number; this
    block tells the actor what that number means and the rules around it.
    """
    ladder = "\n".join(f"  H{rung}: {desc}" for rung, desc in sorted(RUNG_LADDER.items()))
    return (
        "## SUPERVISED-TUTOR DISCIPLINE (applies every turn)\n\n"
        "You are the actor in a supervised pipeline. At the very END of this "
        "prompt you will find a 'THIS TURN' envelope. Obey it exactly; it "
        "overrides any general teaching instinct above when they conflict.\n\n"
        "Hint ladder (H0 = least help, H7 = most help):\n"
        f"{ladder}\n\n"
        "Rules:\n"
        "- Never exceed the turn's help ceiling (H<=N). If the student pushes "
        "for more, hold at the ceiling and say why, warmly (do not stonewall).\n"
        "- Below H7, never hand over the finished answer to the student's "
        "current exercise or assignment: no complete worked solution, and never "
        "compilable or copy-pasteable code that solves it. Prose, partial steps "
        "and pseudo-code with blanks (___) are allowed only where the ceiling "
        "permits.\n"
        "- A goal under EXAM LOCKDOWN: do not teach or hint it; the student must "
        "demonstrate it themselves.\n"
        "- When the envelope says to ground: cite the retrieved material, and if "
        "you did not retrieve it, say so rather than inventing.\n\n"
        "Instructional moves: the envelope may name THIS TURN'S MOVE, chosen "
        "by the pedagogy strategist from the student's trajectory. Open your "
        "reply with that move (it shapes HOW you start, never what you may "
        "reveal — the ceiling always wins). The vocabulary:\n"
        + (_actor_vocab() or "\n".join(f"  {m.value}: {desc}" for m, desc in MOVE_GUIDANCE.items()))
        + "\nIf no move is named, choose your own opening. The move names are "
        "internal labels: never write them in your reply (no \"PROBE_PRIOR\", "
        "no \"**CHECK_UNDERSTANDING**\"); just do the move in plain language."
    )


def render_envelope(
    env: Envelope,
    goal_titles: dict[str, str] | None = None,
    calibration_note: str = "",
) -> str:
    """Render an :class:`~tutor.agent.policy.Envelope` into the per-turn block.

    Imperative, LearnLM style. Goes in the UNCACHED trailing block, last in the
    system prompt (the order contract: persona prefix, grounding, facts,
    envelope last). ``goal_titles`` maps goal ids to human titles for
    readability; ids are used when a title is missing. ``calibration_note`` is
    an optional content-only hint (Engram P2, high-confidence-wrong); it never
    changes the rung ceiling.
    """
    titles = goal_titles or {}

    def label(gid: str) -> str:
        t = titles.get(gid)
        return f"{gid} ({t})" if t else gid

    lines = [
        "---",
        f"THIS TURN: respond at hint level <= H{env.max_rung}.",
    ]
    if env.goals:
        lines.append(f"Goals in scope: {', '.join(label(g) for g in env.goals)}.")
    if env.exam_lockdown:
        lines.append(
            "EXAM LOCKDOWN on "
            f"{', '.join(label(g) for g in env.exam_lockdown)}: do not teach or "
            "hint these; the student must demonstrate them."
        )
    # Every call declares the full tool list (a stable cache prefix, plan step
    # 2.3); the ones this envelope rules out are named here and refused
    # server-side if called anyway.
    from tutor.agent.tools import TOOL_DEFINITIONS, tools_for_envelope

    allowed = {t["name"] for t in tools_for_envelope(env)}
    off = [t["name"] for t in TOOL_DEFINITIONS if t["name"] not in allowed]
    if off:
        lines.append(f"Tools off this turn (calls are refused): {', '.join(off)}.")
    if env.max_rung < 7:
        lines.append(
            "Do not hand over the finished answer to the student's current task "
            "(no complete worked solution). Do not produce compilable or "
            "copy-pasteable code that solves the student's current task, not even "
            "a corrected line, a function signature, or a body."
        )
    if env.max_rung <= 4:
        lines.append(
            "Do NOT give away the answer: do not name the specific bug or error, "
            "the exact wrong token or step, or the corrected value. Point the "
            "student to the part of their code or reasoning, or the condition, to "
            "re-examine and ask one leading question, so they find it themselves."
        )
    if env.must_ground:
        lines.append(
            "Ground course claims in the retrieved material below. Do NOT name or "
            "cite a specific slide, textbook, lecture, or source unless it appears "
            "in this turn's retrieved context; if nothing relevant was retrieved, "
            "help in your own words without attributing it to a source."
        )
    if not env.allow_exercise:
        lines.append("Do not hand out a new exercise this turn.")
    if env.move is not None:
        guidance = MOVE_GUIDANCE.get(env.move, env.move.value)
        line = f"THIS TURN'S MOVE: {guidance}."
        if env.move_reason:
            line += f" (Why: {env.move_reason})"
        lines.append(line)
    if calibration_note:
        lines.append(calibration_note)
    return "\n".join(lines)


@lru_cache(maxsize=1)
def build_system_prompt() -> str:
    cfg = get_config()
    persona_dir = cfg["persona_dir"]
    profile_dir = cfg["instructor_profile_dir"]

    parts = [_preamble()]

    _try_append(parts, persona_dir / "persona-distilled.md", "## CORE PERSONA RULES")
    _try_append(parts, persona_dir / "communication.md", "## COMMUNICATION STYLE")
    _try_append(parts, persona_dir / "teaching.md", "## TEACHING PHILOSOPHY")
    _try_append(parts, profile_dir / "transcripts-voice.md", "## LECTURE VOICE (from actual transcripts — authoritative)")
    _try_append(parts, profile_dir / "lecture-flow.md", "## LECTURE FLOW (how a session is structured)")
    _try_append(parts, profile_dir / "teaching-patterns.md", "## TEACHING PATTERNS (pedagogical signatures + grading stance)")
    _try_append(parts, profile_dir / "examples-library.md", "## EXAMPLES LIBRARY")
    _try_append(parts, profile_dir / "topic-explanations.md",
                "## TOPIC EXPLANATIONS (how the instructor explains each topic, from transcripts)")

    parts.append(_hint_ladder_block())
    parts.append(_FOOTER)
    return "\n\n".join(parts)


def _try_append(parts: list, path: Path, heading: str) -> None:
    if path.exists():
        parts.append(f"{heading}\n\n{path.read_text().strip()}")
    else:
        parts.append(f"{heading}\n\n[File not found: {path}]")
