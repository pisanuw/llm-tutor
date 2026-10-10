"""schema drift: boot-only columns, NOT NULL, and student indexes

Plan step 2.8 (findings C6, C7, C25).

* Seven columns existed only through startup.ensure_schema's boot ALTER
  (learning_goals.threshold, the five student_progress.fsrs_* columns, and
  turn_events.confidence), so a database built by the migrations lacked them.
* The boot ALTER adds columns nullable where the models declare NOT NULL, and
  the baseline created five timestamps nullable. Each is backfilled and set
  NOT NULL, so a migrated database and one the app built at boot both match
  the models.
* Indexes on the student columns that history, the voice and feedback
  lookups, the class reports, and admin counts filter on.

Every step is guarded: it runs on a migrated database, on one the app built
at boot (tutor.migrate stamps that at the baseline first), and a second time.
The downgrade drops the indexes only; the columns and constraints stay.

Revision ID: e8a3c5d1f2b4
Revises: d4f7b2c9e813
Create Date: 2026-09-26
"""
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8a3c5d1f2b4"
down_revision: Union[str, Sequence[str], None] = "d4f7b2c9e813"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# No new tables, so nothing new to enable RLS on.
_RLS_TABLES: tuple[str, ...] = ()

_NOW = sa.func.now()

# (table, column, type, nullable, server default) for columns no revision added.
_MISSING_COLUMNS: tuple[tuple[str, str, Any, bool, Any], ...] = (
    ("learning_goals", "threshold", sa.Boolean(), False, sa.false()),
    ("student_progress", "fsrs_stability", sa.Float(), True, None),
    ("student_progress", "fsrs_difficulty", sa.Float(), True, None),
    ("student_progress", "fsrs_due", sa.DateTime(), True, None),
    ("student_progress", "fsrs_reps", sa.Integer(), False, sa.text("0")),
    ("student_progress", "fsrs_lapses", sa.Integer(), False, sa.text("0")),
    ("turn_events", "confidence", sa.Integer(), True, None),
)

# (table, column, value for existing NULLs) for columns the models declare NOT NULL.
_NOT_NULL: tuple[tuple[str, str, Any], ...] = (
    ("students", "restricted", False),
    ("students", "login_count", 0),
    ("students", "is_instructor", False),
    ("learning_goals", "threshold", False),
    ("student_progress", "seen", False),
    ("student_progress", "fsrs_reps", 0),
    ("student_progress", "fsrs_lapses", 0),
    ("goal_misconceptions", "created_at", _NOW),
    ("goal_misconceptions", "updated_at", _NOW),
    ("message_feedback", "created_at", _NOW),
    ("message_feedback", "updated_at", _NOW),
    ("misconceptions", "created_at", _NOW),
)

_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("messages", "ix_messages_student_course_created", ["student_id", "course", "created_at"]),
    ("exercise_attempts", "ix_exercise_attempts_student_id", ["student_id"]),
    ("topics_accessed", "ix_topics_accessed_student_id", ["student_id"]),
)


def _columns(table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    for table, column, type_, nullable, default in _MISSING_COLUMNS:
        if column not in _columns(table):
            op.add_column(table, sa.Column(column, type_, nullable=nullable,
                                           server_default=default))

    for table, column, value in _NOT_NULL:
        existing = _columns(table).get(column)
        if existing is None or not existing["nullable"]:
            continue
        rows = sa.table(table, sa.column(column))
        op.execute(rows.update().where(rows.c[column].is_(None)).values({column: value}))
        # batch mode: a plain ALTER on Postgres, a table rebuild on SQLite
        # (which cannot change a column's nullability in place).
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=existing["type"], nullable=False)

    for table, name, columns in _INDEXES:
        if name not in _indexes(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    for table, name, _columns_ in _INDEXES:
        if name in _indexes(table):
            op.drop_index(name, table_name=table)
