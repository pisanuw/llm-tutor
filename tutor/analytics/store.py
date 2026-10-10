"""The local store: a SQLite copy of what the analysis needs
(phase 2).

``export`` reads the production database (``DATABASE_URL``, read-only) or
the app over HTTP (``/admin/export/analytics/<table>``, the same rows as
JSON lines) and writes a local SQLite file every analysis command opens.
The copy is pseudonymous by default: ``students`` carries an id and a
pseudonym, no email or name, unless ``with_emails`` is set, and
``messages`` carries each message's length, never its text, unless
``with_content`` is set. The file is still student data: it lives on the
instructor's machine under the same rules as the database
(docs/retention-policy.md, docs/analytics-cookbook.md).

Each table has a spec: the columns copied, how rows are selected, and
whether the copy is incremental (by id, with a short tail re-read so a row
finished after the last export is refreshed) or replaced whole (small
tables whose rows change in place). ``read_rows`` is the one producer for
both sources, so the HTTP route and the direct path agree row for row.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from tutor.db.models import (
    ActivityEvent, ClassEnrollment, ClassInstructor, CourseClass, GradedEvent, LearningGoal, LoginAttempt,
    MasteryAttempt, Message, MessageFeedback, PathEvent, RequestError, Session as LoginSession, Student,
    StudentDay, StudentProgress, TopicAccess, Track, TurnEvent, safe_json_loads,
)

PAGE = 5000             # rows per read, both sources
TAIL = 500              # ids re-read below the last exported one (rows finished later)
_LEC_RE = re.compile(r"lecture-(\d+)$")
_SLIDE_RE = re.compile(r"(?m)^##\s+Slide\s+\d+\s*:")


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    incremental: bool = True            # by id, else replaced whole
    student_col: Optional[str] = "student_id"
    time_col: Optional[str] = None      # the column --since filters on
    primary_key: tuple[str, ...] = ("id",)
    optional: tuple[str, ...] = field(default_factory=tuple)   # columns present only with a flag


STUDENT_DAY_METRICS = (
    "questions", "turns", "cost_usd", "active_min", "deck_min", "narration_min", "visits", "slides_seen",
    "lectures_opened", "error_outcomes", "busy_outcomes", "detector_cuts", "blocks", "slow_turns",
    "thumbs_up", "thumbs_down", "logins", "failed_logins", "user_agents", "request_errors", "hours_mask",
)

TABLES: tuple[TableSpec, ...] = (
    TableSpec("students", ("id", "pseudonym", "created_at", "last_seen", "last_login", "login_count",
                           "anonymized_at", "is_instructor"),
              incremental=False, student_col="id", optional=("email", "name")),
    TableSpec("classes", ("id", "name", "course", "term", "instructor_id", "archived", "created_at"),
              incremental=False, student_col=None),
    TableSpec("class_enrollments", ("id", "class_id", "student_id", "created_at"), incremental=False),
    TableSpec("class_instructors", ("id", "class_id", "student_id", "created_at"), incremental=False),
    TableSpec("sessions", ("id", "student_id", "created_at", "expires_at", "method"), time_col="created_at"),
    TableSpec("messages", ("id", "student_id", "role", "course", "lecture_num", "slide_num", "created_at",
                           "chars"), time_col="created_at", optional=("content",)),
    TableSpec("message_feedback", ("id", "message_id", "student_id", "rating", "created_at", "updated_at",
                                   "seen_at"),
              time_col="created_at", optional=("comment",)),
    TableSpec("turn_events", ("id", "student_id", "ts", "kind", "turn_id", "intent", "rung_used", "move",
                              "goal_ids_json", "grounding_hit", "verdict", "model", "in_tokens", "out_tokens",
                              "cache_read_tokens", "cache_write_tokens", "cost_usd", "latency_ms",
                              "confidence", "timings_json"), time_col="ts"),
    TableSpec("topics_accessed", ("id", "student_id", "lecture_num", "slide_num", "accessed_at"),
              time_col="accessed_at"),
    TableSpec("path_events", ("id", "student_id", "course", "goal_id", "modality", "source", "ts"),
              time_col="ts"),
    TableSpec("graded_events", ("id", "student_id", "goal_id", "success", "source", "ts"), time_col="ts"),
    TableSpec("mastery_attempts", ("id", "student_id", "goal_id", "status", "attempts", "started_at",
                                   "completed_at", "deferred_at"), incremental=False),
    TableSpec("student_progress", ("id", "student_id", "goal_id", "status", "mastery_score",
                                   "last_evidence_at", "mastered_at", "confirmed_at", "seen", "updated_at"),
              incremental=False),
    TableSpec("activity_events", ("id", "student_id", "login_session_id", "visit_id", "page_view_id", "seq",
                                  "name", "ts", "client_ts", "course", "lecture_num", "slide_num", "turn_id",
                                  "message_id", "path", "target", "label", "value_ms", "detail_json"),
              time_col="ts"),
    TableSpec("learning_goals", ("id", "title", "course", "lecture_num", "threshold"),
              incremental=False, student_col=None),
    TableSpec("tracks", ("id", "course", "title", "goal_ids_json"), incremental=False, student_col=None),
    TableSpec("lectures", ("course", "lecture_num", "title", "slides_total", "narration_s_json",
                           "narration_total_s"),
              incremental=False, student_col=None, primary_key=("course", "lecture_num")),
    # Review step 0.9: the daily rollup (replaced whole, since its rows are
    # recomputed), the sign-in trail without its address, host or browser
    # string (the browser only as a class), and the error pages.
    TableSpec("student_days", ("id", "student_id", "day", "course") + STUDENT_DAY_METRICS + ("computed_at",),
              incremental=False),
    TableSpec("login_attempts", ("id", "created_at", "student_id", "method", "outcome", "agent", "detail"),
              time_col="created_at"),
    TableSpec("request_errors", ("id", "ts", "status", "method", "path", "referrer_path", "student_id",
                                 "kind", "agent", "ref", "detail"), time_col="ts"),
)
TABLE_NAMES = tuple(t.name for t in TABLES)
_SPECS = {t.name: t for t in TABLES}
_MODELS: dict[str, Any] = {
    "students": Student, "classes": CourseClass, "class_enrollments": ClassEnrollment,
    "class_instructors": ClassInstructor,
    "sessions": LoginSession, "messages": Message, "message_feedback": MessageFeedback,
    "turn_events": TurnEvent, "topics_accessed": TopicAccess, "path_events": PathEvent,
    "graded_events": GradedEvent, "mastery_attempts": MasteryAttempt,
    "student_progress": StudentProgress, "activity_events": ActivityEvent,
    "learning_goals": LearningGoal, "tracks": Track,
    "student_days": StudentDay, "login_attempts": LoginAttempt, "request_errors": RequestError,
}


def spec(name: str) -> TableSpec:
    return _SPECS[name]


def pseudonym(student_id: int) -> str:
    """The name a student goes by in reports: stable, not an email."""
    return f"S{student_id}"


# ---------------------------------------------------------------------------
# Reading rows from the database (the one producer for both sources)
# ---------------------------------------------------------------------------

def roster_ids(db: DBSession, class_id: Optional[int]) -> Optional[set[int]]:
    """The student ids of one class, or None for everyone."""
    if class_id is None:
        return None
    rows = db.execute(select(ClassEnrollment.student_id).where(ClassEnrollment.class_id == class_id)).all()
    return {int(r[0]) for r in rows}


def _row_from(model_row: Any, name: str, *, with_emails: bool, with_content: bool) -> dict[str, Any]:
    """One model instance as the local table's row."""
    if name == "students":
        row = {c: getattr(model_row, c) for c in spec(name).columns if c != "pseudonym"}
        row["pseudonym"] = pseudonym(model_row.id)
        if with_emails:
            row["email"], row["name"] = model_row.email, model_row.name
        return row
    if name == "messages":
        row = {c: getattr(model_row, c) for c in spec(name).columns if c != "chars"}
        row["chars"] = len(model_row.content or "")
        if with_content:
            row["content"] = model_row.content
        return row
    if name == "message_feedback":
        # The student's reason is free text: only with --with-content, like a message's.
        row = {c: getattr(model_row, c) for c in spec(name).columns}
        if with_content:
            row["comment"] = model_row.comment
        return row
    if name == "turn_events":
        verdicts = safe_json_loads(model_row.verdicts_json, {}) or {}
        envelope = safe_json_loads(model_row.envelope_json, {}) or {}
        row = {c: getattr(model_row, c) for c in spec(name).columns
               if c not in ("move", "verdict", "timings_json")}
        row["verdict"] = verdicts.get("verdict") if isinstance(verdicts, dict) else None
        row["move"] = envelope.get("move") if isinstance(envelope, dict) else None
        timings = verdicts.get("timings") if isinstance(verdicts, dict) else None
        row["timings_json"] = json.dumps(timings, sort_keys=True) if timings else None
        return row
    if name == "login_attempts":
        from tutor.request_errors import classify_agent

        row = {c: getattr(model_row, c) for c in spec(name).columns if c != "agent"}
        row["agent"] = classify_agent(model_row.user_agent)
        return row
    if name == "student_days":
        row = {c: getattr(model_row, c) for c in spec(name).columns}
        row["day"] = model_row.day.isoformat() if model_row.day else None
        return row
    return {c: getattr(model_row, c) for c in spec(name).columns}


def read_rows(
    db: DBSession,
    name: str,
    *,
    since_id: int = 0,
    limit: int = PAGE,
    since: Optional[datetime.datetime] = None,
    class_id: Optional[int] = None,
    with_emails: bool = False,
    with_content: bool = False,
) -> list[dict[str, Any]]:
    """Up to ``limit`` rows of ``name`` as the local store holds them.

    Incremental tables come in id order above ``since_id``; whole tables
    ignore ``since_id`` and ``limit`` and come entire. ``since`` keeps only
    rows at or after that time (on the spec's time column); ``class_id``
    keeps only one roster's students (and that class).
    """
    if name == "lectures":
        return lectures_rows()
    s = spec(name)
    model = _MODELS[name]
    ids = roster_ids(db, class_id)
    query = select(model)
    if ids is not None:
        if s.student_col is not None:
            col = getattr(model, s.student_col)
            query = query.where(col.in_(sorted(ids)) if ids else col.in_([-1]))
        elif name == "classes":
            query = query.where(model.id == class_id)
    if s.incremental:
        query = query.where(model.id > since_id).order_by(model.id).limit(limit)
        if since is not None and s.time_col:
            query = query.where(getattr(model, s.time_col) >= since)
    else:
        query = query.order_by(*(getattr(model, c) for c in s.primary_key))
    rows = db.execute(query).scalars().all()
    return [_row_from(r, name, with_emails=with_emails, with_content=with_content) for r in rows]


def max_id(db: DBSession, name: str) -> int:
    model = _MODELS[name]
    value = db.execute(select(func.max(model.id))).scalar()
    return int(value or 0)


def lectures_rows(output_dir: Optional[Path] = None, audio_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """One row per rendered deck: slides, and the narration seconds per slide
    from ``timing.json`` (beside the deck, else beside the clips), so a
    listened fraction has a denominator."""
    from tutor.config import get_config
    from tutor.courses import COURSES

    cfg = get_config()
    out = output_dir or cfg["output_dir"]
    audio = audio_dir or cfg["lecture_audio_dir"]
    rows: list[dict[str, Any]] = []
    for course in COURSES:
        course_dir = Path(out) / course
        if not course_dir.exists():
            continue
        for lec_dir in sorted(course_dir.glob("lecture-*")):
            m = _LEC_RE.search(lec_dir.name)
            if not m or not (lec_dir / "index.html").exists():
                continue
            num = int(m.group(1))
            content = lec_dir / "content.md"
            text = content.read_text(errors="ignore") if content.exists() else ""
            title = ""
            tm = re.search(r"(?m)^##\s+Slide\s+1\s*:\s*(.+?)\s*$", text)
            if tm:
                title = tm.group(1)
            timing: dict[str, float] = {}
            for candidate in (lec_dir / "timing.json", Path(audio) / course / lec_dir.name / "timing.json"):
                if candidate.exists():
                    try:
                        raw = json.loads(candidate.read_text())
                        timing = {str(k): float(v) for k, v in raw.items()}
                    except (ValueError, TypeError, AttributeError):
                        timing = {}
                    break
            rows.append({
                "course": course, "lecture_num": num, "title": title,
                "slides_total": len(_SLIDE_RE.findall(text)),
                "narration_s_json": json.dumps(timing, sort_keys=True) if timing else None,
                "narration_total_s": round(sum(timing.values()), 1) if timing else None,
            })
    return rows


# ---------------------------------------------------------------------------
# JSON on the wire (the HTTP source)
# ---------------------------------------------------------------------------

def jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat(sep=" ") if isinstance(v, datetime.datetime) else v) for k, v in row.items()}


_DATETIME_COLUMNS = frozenset({
    "created_at", "last_seen", "last_login", "anonymized_at", "expires_at", "ts", "client_ts",
    "accessed_at", "started_at", "completed_at", "deferred_at", "last_evidence_at", "mastered_at",
    "confirmed_at", "updated_at",
})


def from_json(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in _DATETIME_COLUMNS:
        value = out.get(key)
        if isinstance(value, str):
            try:
                out[key] = datetime.datetime.fromisoformat(value)
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------------------
# The local SQLite file
# ---------------------------------------------------------------------------

_TEXT_PK = {"learning_goals", "tracks"}


def _ddl(s: TableSpec, extra: Iterable[str] = ()) -> str:
    cols = list(s.columns) + [c for c in extra if c not in s.columns]
    defs = []
    for c in cols:
        if c == "id" and s.name not in _TEXT_PK and s.primary_key == ("id",):
            defs.append("id INTEGER PRIMARY KEY")
        else:
            defs.append(f"{c}")
    pk = "" if s.primary_key == ("id",) and s.name not in _TEXT_PK else f", PRIMARY KEY ({', '.join(s.primary_key)})"
    return f"CREATE TABLE IF NOT EXISTS {s.name} ({', '.join(defs)}{pk})"


def open_store(path: Path) -> sqlite3.Connection:
    """The local file, with every table and the meta table present."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    for s in TABLES:
        con.execute(_ddl(s, s.optional))
    con.execute("CREATE TABLE IF NOT EXISTS export_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_local_ae_student_ts ON activity_events (student_id, ts)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_local_ae_visit ON activity_events (visit_id, seq)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_local_msg_student ON messages (student_id, course, created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_local_te_student ON turn_events (student_id, ts)")
    con.commit()
    return con


def _sqlite_value(v: Any) -> Any:
    if isinstance(v, datetime.datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, bool):
        return int(v)
    return v


def write_rows(con: sqlite3.Connection, name: str, rows: list[dict[str, Any]], replace_all: bool = False) -> int:
    """Upsert ``rows`` into the local table; ``replace_all`` empties it first."""
    s = spec(name)
    if replace_all:
        con.execute(f"DELETE FROM {name}")
    if not rows:
        con.commit()
        return 0
    cols = list(s.columns) + [c for c in s.optional if any(c in r for r in rows)]
    placeholders = ", ".join("?" for _ in cols)
    con.executemany(
        f"INSERT OR REPLACE INTO {name} ({', '.join(cols)}) VALUES ({placeholders})",
        [tuple(_sqlite_value(r.get(c)) for c in cols) for r in rows],
    )
    con.commit()
    return len(rows)


def local_max_id(con: sqlite3.Connection, name: str) -> int:
    value = con.execute(f"SELECT MAX(id) FROM {name}").fetchone()[0]
    return int(value or 0)


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute("INSERT OR REPLACE INTO export_meta (key, value) VALUES (?, ?)", (key, json.dumps(value, default=str)))
    con.commit()


def get_meta(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT value FROM export_meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


# ---------------------------------------------------------------------------
# The export itself
# ---------------------------------------------------------------------------

Reader = Callable[..., list[dict[str, Any]]]


@dataclass
class ExportOptions:
    since: Optional[datetime.datetime] = None
    class_id: Optional[int] = None
    with_emails: bool = False
    with_content: bool = False
    full: bool = False


def export(con: sqlite3.Connection, reader: Reader, options: ExportOptions,
           log: Callable[[str], None] = lambda _m: None) -> dict[str, int]:
    """Copy every table through ``reader(name, since_id=, limit=)`` into the
    store. Whole tables are replaced; incremental ones continue from the
    local maximum id minus a tail, or from zero with ``full``. Returns rows
    written per table."""
    counts: dict[str, int] = {}
    for s in TABLES:
        if not s.incremental:
            rows = reader(s.name, since_id=0, limit=PAGE)
            counts[s.name] = write_rows(con, s.name, rows, replace_all=True)
            log(f"{s.name}: {counts[s.name]} rows (replaced)")
            continue
        start = 0 if options.full else max(0, local_max_id(con, s.name) - TAIL)
        total = 0
        since_id = start
        while True:
            rows = reader(s.name, since_id=since_id, limit=PAGE)
            if not rows:
                break
            total += write_rows(con, s.name, rows)
            since_id = int(rows[-1]["id"])
            if len(rows) < PAGE:
                break
        counts[s.name] = total
        log(f"{s.name}: {total} rows from id {start}")
    set_meta(con, "last_export", datetime.datetime.now(datetime.timezone.utc).isoformat())
    set_meta(con, "options", {"since": options.since.isoformat() if options.since else None,
                              "class_id": options.class_id, "with_emails": options.with_emails,
                              "with_content": options.with_content})
    return counts


def db_reader(db: DBSession, options: ExportOptions) -> Reader:
    """A reader over a database session (the direct path)."""
    def read(name: str, *, since_id: int = 0, limit: int = PAGE) -> list[dict[str, Any]]:
        return read_rows(db, name, since_id=since_id, limit=limit, since=options.since,
                         class_id=options.class_id, with_emails=options.with_emails,
                         with_content=options.with_content)
    return read


def http_reader(base_url: str, admin_token: str, options: ExportOptions, client: Any = None) -> Reader:
    """A reader over the app's admin export route (no database credentials
    on this machine; the admin token instead). ``client`` is an httpx-like
    object with ``get(url, params=, headers=)``; the default is httpx."""
    if client is None:
        import httpx
        client = httpx.Client(timeout=120)
    base = base_url.rstrip("/")

    def read(name: str, *, since_id: int = 0, limit: int = PAGE) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"since_id": since_id, "limit": limit}
        if options.since:
            params["since"] = options.since.isoformat()
        if options.class_id is not None:
            params["class_id"] = options.class_id
        if options.with_emails:
            params["with_emails"] = "true"
        if options.with_content:
            params["with_content"] = "true"
        resp = client.get(f"{base}/admin/export/analytics/{name}", params=params,
                          headers={"X-Admin-Token": admin_token})
        if resp.status_code != 200:
            raise RuntimeError(f"{name}: the export route answered {resp.status_code}")
        return [from_json(json.loads(line)) for line in resp.text.splitlines() if line.strip()]
    return read
