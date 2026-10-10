"""activity_events; turn_events.turn_id

Per-click and per-slide activity logging:

  * ``activity_events`` — one row per thing a student did in the browser
    (a page opened or left, a click, a question sent, a reply finished, a
    slide shown with its dwell, a narration clip played), written in batches
    through ``state.record_activity_events``. Born RLS-enabled.
  * ``turn_events.turn_id`` — the id the chat stream sends as its resume
    handle, so a browser event joins the ledger row of its turn.

Guarded like every revision after the baseline: a database the app already
booted has the table (create_all) and the column (startup.ensure_schema).

Revision ID: a9c4e7f1b2d3
Revises: b7c2d9e4f1a3
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9c4e7f1b2d3"
down_revision: Union[str, Sequence[str], None] = "b7c2d9e4f1a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables this revision creates, RLS-enabled from birth (see c7a41e9db830).
_RLS_TABLES = ("activity_events",)

# (index name, columns) — the same shape the ORM declares, so
# tests/test_migrations.py finds no drift between a migrated database and
# the models.
_INDEXES = (
    ("ix_activity_events_student_id", ["student_id"]),
    ("ix_activity_events_ts", ["ts"]),
    ("ix_activity_events_turn_id", ["turn_id"]),
    ("ix_activity_events_student_ts", ["student_id", "ts"]),
    ("ix_activity_events_name_ts", ["name", "ts"]),
    ("ix_activity_events_visit_seq", ["visit_id", "seq"]),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_column("turn_events", "turn_id"):
        op.add_column("turn_events", sa.Column("turn_id", sa.String(length=32), nullable=True))
    if not _has_index("turn_events", "ix_turn_events_turn_id"):
        op.create_index(op.f("ix_turn_events_turn_id"), "turn_events", ["turn_id"])
    if not _has_table("activity_events"):
        op.create_table(
            "activity_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("login_session_id", sa.Integer(), nullable=True),
            sa.Column("visit_id", sa.String(length=32), nullable=False),
            sa.Column("page_view_id", sa.String(length=32), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=40), nullable=False),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.Column("client_ts", sa.DateTime(), nullable=True),
            sa.Column("course", sa.String(length=8), nullable=True),
            sa.Column("lecture_num", sa.Integer(), nullable=True),
            sa.Column("slide_num", sa.Integer(), nullable=True),
            sa.Column("turn_id", sa.String(length=32), nullable=True),
            sa.Column("message_id", sa.Integer(), nullable=True),
            sa.Column("path", sa.String(length=200), nullable=True),
            sa.Column("target", sa.String(length=80), nullable=True),
            sa.Column("label", sa.String(length=80), nullable=True),
            sa.Column("value_ms", sa.Integer(), nullable=True),
            sa.Column("detail_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in _INDEXES:
        if not _has_index("activity_events", name):
            op.create_index(op.f(name), "activity_events", columns)
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    for name, _columns in reversed(_INDEXES):
        op.drop_index(op.f(name), table_name="activity_events")
    op.drop_table("activity_events")
    with op.batch_alter_table("turn_events") as batch:
        batch.drop_index(op.f("ix_turn_events_turn_id"))
        batch.drop_column("turn_id")


def _set_rls(enable: bool) -> None:
    """Enable/disable RLS on this revision's tables (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    verb = "ENABLE" if enable else "DISABLE"
    for table in _RLS_TABLES:
        # Identifiers are constant literals from this file, not user input.
        op.execute(f'ALTER TABLE public."{table}" {verb} ROW LEVEL SECURITY')
