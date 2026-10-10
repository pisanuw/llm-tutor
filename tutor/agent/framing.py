"""Framing untrusted text for model calls (plan step 1.9, finding A11).

Student-derived text reaches the models in several places: the message the
classifier and the exam labeller read, answers the graders score, code the
rubric evaluates, the transcript the summarizer folds, the draft the judge
audits, and the rolling summary the actor sees. Each is wrapped in a tag and
the prompt says the tagged text is data. That only holds if the text cannot
close the tag itself, so every frame neutralizes the closing tags of all
frames here (``</student_message>`` becomes ``<\\/student_message>``).
"""

from __future__ import annotations

import re

FRAME_TAGS = (
    "student_message",
    "student_answer",
    "student_code",
    "transcript",
    "draft",
    "conversation_summary",
)

_CLOSING_TAG_RE = re.compile(r"<\s*/\s*(" + "|".join(FRAME_TAGS) + r")\b", re.IGNORECASE)


def neutralize(text: str) -> str:
    """``text`` with every closing frame tag defused (``</x`` becomes ``<\\/x``)."""
    return _CLOSING_TAG_RE.sub(lambda m: "<\\/" + m.group(1), text or "")


def frame(tag: str, text: str) -> str:
    """``text`` wrapped in ``<tag>`` and ``</tag>``; it cannot end the frame early."""
    if tag not in FRAME_TAGS:
        raise ValueError(f"unknown frame tag {tag!r}")
    return f"<{tag}>\n{neutralize(text)}\n</{tag}>"


def head_tail(text: str, limit: int, tail: int = 1000) -> str:
    """At most about ``limit`` characters: the head and the last ``tail``.

    A classifier that reads only the first N characters can be steered by
    padding: the real request goes at the end. Keeping the tail closes that.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    tail = min(tail, limit // 2)
    head = limit - tail
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[-tail:]}"


_NAME_UNSAFE_RE = re.compile(r"[^\w\s'.-]", re.UNICODE)
NAME_MAX_CHARS = 40


def safe_name(name: str, fallback: str = "") -> str:
    """A display name fit for a prompt: letters, digits, spaces, and ' . -
    only, whitespace collapsed, at most NAME_MAX_CHARS. Anything else in a
    self-chosen profile name (markup, brackets, newlines) is dropped."""
    clean = " ".join(_NAME_UNSAFE_RE.sub(" ", name or "").replace("_", " ").split())
    return clean[:NAME_MAX_CHARS].strip() or fallback
