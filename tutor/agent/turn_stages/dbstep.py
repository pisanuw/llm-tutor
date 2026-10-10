"""Each database step of a turn is its own transaction, off the event loop."""

from __future__ import annotations

import asyncio


async def db_step(db, fn=None, *args, **kwargs):
    """Run one database step of the turn in the default executor as its own
    transaction: commit when it returns, roll back when it raises (plan step
    2.6, finding C1).

    On Postgres every query and commit is a network round trip, and on the
    event loop it stalls every other request, SSE token streams included.
    Ending each step with a commit also hands the pooled connection back
    before the next LLM call, so a turn holds one for milliseconds instead of
    for the whole reply (the pool is small: see tutor.db.models). The turn
    awaits each step, so its session is never used by two threads at once.
    With ``fn`` left out, the step commits what the turn has added.
    """
    def step():
        try:
            result = fn(*args, **kwargs) if fn is not None else None
            db.commit()
            return result
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise

    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, step)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        # Cancelling the await does not stop the thread: it would go on to
        # commit on the session the cancelled turn is now closing (a reply
        # saved after its "cut off" note; the Stage 4 review). Wait for it.
        await asyncio.wait([fut])
        raise
