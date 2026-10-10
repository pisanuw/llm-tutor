"""The turn's request, as the stages read and rewrite it."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from tutor.db.models import Student
from tutor.courses import DEFAULT_COURSE


@dataclass
class TurnRequest:
    """One turn's request.

    The exam intercept may rewrite the message the turn answers: after a
    pass, or after a bare "Not now", the question the gate held back is
    replayed through the normal flow.
    """

    student: Student
    user_message: str
    lecture_num: Optional[int] = None
    slide_num: Optional[int] = None
    course: str = DEFAULT_COURSE
    # The student's id, read once: a rolled-back step expires the turn's
    # objects, and student.id on the event loop would then be a blocking
    # reload (plan step 2.6).
    sid: int = 0
    # Correlates turn_start with the terminal chat_turn, gate, and slow_turn
    # events in the activity stream.
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started: float = field(default_factory=time.monotonic)
    # The message row this turn answers. A prerequisite exam records it as
    # its trigger, and the history leaves it out because it is sent as the
    # final user turn (plan step 2.4, A4).
    gate_trigger_id: Optional[int] = None
    # Set when this turn must not become a prerequisite exam: after a DEFER
    # (the student just asked to change the subject) and on the replay of a
    # question a passed exam swallowed (one pass buys one answer, not the
    # next exam).
    skip_gate: bool = False
    # Where the turn's time went (Phase A), in
    # ms: ``*_at_ms`` marks are elapsed time since the turn started, and the
    # rest are durations summed over the turn. Stored on the TurnEvent's
    # verdicts as "timings".
    timings: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sid:
            self.sid = self.student.id

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def mark(self, name: str) -> None:
        """Record when the turn reached ``name`` (the first mark wins)."""
        self.timings.setdefault(f"{name}_at_ms", self.elapsed_ms())

    def add_ms(self, name: str, since: float) -> None:
        """Add the time since ``since`` (a ``time.monotonic()``) to ``name``."""
        ms = int((time.monotonic() - since) * 1000)
        self.timings[f"{name}_ms"] = self.timings.get(f"{name}_ms", 0) + ms
