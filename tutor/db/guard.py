"""Best-effort database work that cannot poison the session (plan step 5.6,
finding A13).

On Postgres a failed statement aborts the whole transaction: every later
query in the session fails until a rollback. A fail-soft read that just
swallowed its error ("the progress summary is non-critical") therefore
failed the rest of the request anyway: the turn step's commit, or the
admin page's next query. ``best_effort`` runs the work in a SAVEPOINT. A
failure rolls back to it, is logged at warning (with the turn id when
there is one), and returns ``default``; the outer transaction, pending
writes included, stays usable.

The work passed in must not swallow a database error itself: an error
caught inside the savepoint still leaves it aborted on Postgres.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, TypeVar

from sqlalchemy.orm import Session as DBSession

T = TypeVar("T")

log = logging.getLogger("tutor")


def best_effort(db: DBSession, fn: Callable[[], T], default: T, what: str,
                turn_id: Optional[str] = None) -> T:
    """``fn()`` in a savepoint, or ``default`` (logged) when it fails."""
    try:
        with db.begin_nested():
            return fn()
    except Exception:
        where = f" (turn {turn_id})" if turn_id else ""
        log.warning("%s failed%s; carrying on without it", what, where, exc_info=True)
        return default
