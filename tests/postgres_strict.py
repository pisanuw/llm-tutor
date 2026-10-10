"""Make the suite's SQLite refuse what production's Postgres refuses
(R12).

SQLite accepted a 300-character address in a VARCHAR(255) column, an id past
2^31 in an INTEGER comparison, and a NUL byte in text; Postgres answers each
with an error, so a 500 that only production could raise passed the suite.
This listener, registered for every engine by tests/conftest.py, raises the
same class of error before SQLite runs the statement:

* a bound value for an ``Integer`` column or comparison outside ``int4``
  (SQLAlchemy's psycopg dialect casts the parameter, so even a WHERE fails);
* a string longer than its ``String(N)`` column in an INSERT or UPDATE (a
  comparison is cast without the length, so only writes fail);
* a NUL character in any string the statement finally binds (the app strips
  NUL on every engine, tutor/db/models.py, so this checks what is left after
  that listener, which runs first because tutor.db.models is imported first).

The error is ``sqlalchemy.exc.DataError`` around a ``sqlite3.DataError``,
the class production raises around psycopg's (SQLAlchemy does not wrap an
exception raised by an event listener, so the guard wraps its own).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from sqlalchemy import Integer, String, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError

INT4_MIN, INT4_MAX = -(2**31), 2**31 - 1


def _values(value: Any, expanding: bool) -> Iterable[Any]:
    if expanding and isinstance(value, (list, tuple, set)):
        return value
    return (value,)


def _refuse(message: str) -> None:
    raise DataError("(postgres_strict)", None, sqlite3.DataError(message))


def check(context: Any) -> None:
    compiled = getattr(context, "compiled", None)
    rows = getattr(context, "compiled_parameters", None)
    if compiled is None or not rows:
        return
    write = bool(getattr(context, "isinsert", False) or getattr(context, "isupdate", False))
    # The values a write assigns are bound under their column's name; a
    # WHERE value gets a generated one (token_1) and is cast without the
    # length, so Postgres compares it rather than refusing it.
    table = getattr(getattr(compiled, "statement", None), "table", None)
    assigned = {c.key for c in table.c} if write and table is not None and hasattr(table, "c") else set()
    for row in rows:
        for name, value in row.items():
            bind = compiled.binds.get(name)
            if bind is None or type(bind).__name__ == "_OffsetLimitParam":
                continue                      # LIMIT and OFFSET are bigint on Postgres
            typ = bind.type
            for v in _values(value, bool(getattr(bind, "expanding", False))):
                if isinstance(v, str):
                    if (name in assigned and isinstance(typ, String) and typ.length is not None
                            and len(v) > typ.length):
                        _refuse(f"value too long for type character varying({typ.length}) "
                                f"(parameter {name}, {len(v)} characters)")
                elif (isinstance(v, int) and not isinstance(v, bool) and isinstance(typ, Integer)
                      and not INT4_MIN <= v <= INT4_MAX):
                    _refuse(f"integer out of range (parameter {name} = {v})")


def _is_many(parameters: Any) -> bool:
    # A list of rows, by shape: a batched insert can arrive flagged
    # executemany with one flat row of values.
    return (isinstance(parameters, (list, tuple)) and bool(parameters)
            and all(isinstance(row, (dict, list, tuple)) for row in parameters))


def check_nul(parameters: Any, executemany: bool) -> None:
    rows = parameters if _is_many(parameters) else [parameters]
    for row in rows or ():
        values = row.values() if isinstance(row, dict) else (row if isinstance(row, (list, tuple)) else ())
        for v in values:
            if isinstance(v, str) and "\x00" in v:
                _refuse("PostgreSQL text fields cannot contain NUL (0x00) bytes")


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if conn.dialect.name == "sqlite":
        check(context)
        check_nul(parameters, executemany)


def install() -> None:
    if not event.contains(Engine, "before_cursor_execute", _before_cursor_execute):
        event.listen(Engine, "before_cursor_execute", _before_cursor_execute)
