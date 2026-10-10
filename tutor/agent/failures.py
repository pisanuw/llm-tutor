"""A failed turn is counted here; a burst of them emails the operator.

Every failure inside a turn is caught (the stream error, the crash in the
route's producer, a judge that produced no verdict on a high-risk turn, an
exam grader that was down), ledgered, and shown to the student as a friendly
sentence. None of those paths is an unhandled 500, so the alert the 500
handler sends never fires and, until 2026-10-07, a dead API key or a renamed
model was first seen in the next morning's digest, or in a student's email
(F49 and F50, step 0.8).

:func:`note` counts a failure of one ``kind``; at :data:`BURST` failures
inside :data:`WINDOW_S` seconds it sends one alert through ``tutor.alerts``
(key ``tutor-errors``, so the next email waits for that module's cooldown).
State is process memory on one instance, like the alert dedup itself.
"""

from __future__ import annotations

import collections
import threading
import time

BURST = 3          # failures ...
WINDOW_S = 600     # ... within this many seconds

_lock = threading.Lock()
_recent: collections.deque[tuple[float, str]] = collections.deque()


def _reset_for_tests() -> None:
    with _lock:
        _recent.clear()


def note(kind: str) -> str:
    """Count one failed turn of ``kind`` (``stream``, ``crash``, ``judge``,
    ``grader``). Returns ``counted`` below the burst, else what
    ``alerts.send_alert`` returned."""
    now = time.monotonic()
    with _lock:
        _recent.append((now, kind))
        while _recent and _recent[0][0] < now - WINDOW_S:
            _recent.popleft()
        recent = list(_recent)
    if len(recent) < BURST:
        return "counted"
    from tutor import alerts

    kinds = collections.Counter(k for _, k in recent)
    summary = ", ".join(f"{n} {k}" for k, n in kinds.most_common())
    minutes = WINDOW_S // 60
    body = (
        f"{len(recent)} turns failed in the last {minutes} minutes ({summary}). "
        "Students saw a friendly error instead of an answer. Admin -> Supervision "
        "lists the turns (verdict stream_error, crash, block with judge_unavailable, "
        "or a grading_error); Admin -> Health pings the model. One email per hour "
        "while it goes on."
    )
    return alerts.send_alert("tutor-errors", f"Tutor failures: {len(recent)} in {minutes} minutes", body)
