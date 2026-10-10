"""Query-parameter helpers the routers share."""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import Request
from starlette.datastructures import FormData
from starlette.requests import ClientDisconnect

# Every id and number column is a Postgres INTEGER: a bound value past this
# is "integer out of range", a 500, even in a WHERE (the psycopg dialect
# casts the parameter). SQLite never complained, so the suite never saw it
# (R4).
INT4_MAX = 2**31 - 1


def bounded_int(value: Any, lo: int = 1, hi: int = INT4_MAX) -> Optional[int]:
    """``value`` as a whole number in ``lo..hi``, or None: blank, a word, a
    fraction or a number out of range all read as "not given", so a
    hand-edited parameter falls back to the page's default."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def optional_int(name: str) -> Callable[[Request], Optional[int]]:
    """A dependency for an integer query parameter a form may send empty.

    A picker's "all" option submits ``name=`` (the empty string), which
    ``Query(Optional[int])`` answers with a 422 instead of the page: on
    2026-10-06 Show on the admin Conversations tab, with "all classes"
    selected, returned ``int_parsing`` JSON. Absent, blank, not a whole
    number or out of range all read as None, the page's default: a
    hand-edited ``class_id=abc`` lands on the page, not on an error page
    (R4 and R16).

        class_id: Optional[int] = Depends(optional_int("class_id"))
    """
    def read(request: Request) -> Optional[int]:
        return bounded_int(request.query_params.get(name))
    return read


def int_query(name: str, default: int, lo: int = 1) -> Callable[[Request], int]:
    """A dependency for an integer query parameter with a default: absent,
    blank, a word or out of range reads as ``default``, so a hand-edited
    ``?days=abc`` opens the page as it would without it, where
    ``Query(int)`` answered FastAPI's 422 (R13). The route clamps the upper
    end where it has one.

        days: int = Depends(int_query("days", DEFAULT_DAYS))
    """
    def read(request: Request) -> int:
        n = bounded_int(request.query_params.get(name), lo=lo)
        return default if n is None else n
    return read


async def read_form(request: Request) -> FormData:
    """The request's form, read before the login check. Listed ahead of
    ``get_current_student`` in a route's parameters, it makes FastAPI read the
    body first: the login check opens a pooled connection, and a slow or
    stalled upload used to hold it for as long as the body took to arrive
    (R1)."""
    return await request.form()


async def read_body(request: Request) -> Optional[bytes]:
    """The raw body, read before the login check for the same reason as
    ``read_form``; None when the browser left before it finished sending."""
    try:
        return await request.body()
    except ClientDisconnect:
        return None
