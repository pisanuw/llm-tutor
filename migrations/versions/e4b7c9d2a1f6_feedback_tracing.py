"""messages.turn_id; message_feedback.seen_at; feedback_events

Tracing a thumbs up or down to its conversation and its turn (step 0.4):

  * ``messages.turn_id`` — the turn a message belongs to, so a flagged
    reply reaches its ledger row (verdict, cost, cut) without a join by time.
  * ``message_feedback.seen_at`` — when the instructor marked the flag seen.
  * ``feedback_events`` — every rating a student gave, append-only, so a
    thumbs-up after a thumbs-down no longer erases the complaint. Born
    RLS-enabled.

Guarded like every revision after the baseline: a database the app already
booted has the table (create_all) and the columns (startup.ensure_schema).

Revision ID: e4b7c9d2a1f6
Revises: d6e1f8a2c4b7
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4b7c9d2a1f6"
down_revision: Union[str, Sequence[str], None] = "d6e1f8a2c4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables this revision creates, RLS-enabled from birth (see c7a41e9db830).
_RLS_TABLES = ("feedback_events",)

_INDEXES = (
    ("ix_feedback_events_message_id", ["message_id"]),
    ("ix_feedback_events_student_id", ["student_id"]),
    ("ix_feedback_events_ts", ["ts"]),
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
    if not _has_column("messages", "turn_id"):
        op.add_column("messages", sa.Column("turn_id", sa.String(length=32), nullable=True))
    if not _has_index("messages", "ix_messages_turn_id"):
        op.create_index(op.f("ix_messages_turn_id"), "messages", ["turn_id"])
    if not _has_column("message_feedback", "seen_at"):
        op.add_column("message_feedback", sa.Column("seen_at", sa.DateTime(), nullable=True))
    if not _has_table("feedback_events"):
        op.create_table(
            "feedback_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in _INDEXES:
        if not _has_index("feedback_events", name):
            op.create_index(op.f(name), "feedback_events", columns)
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    for name, _columns in reversed(_INDEXES):
        op.drop_index(op.f(name), table_name="feedback_events")
    op.drop_table("feedback_events")
    with op.batch_alter_table("message_feedback") as batch:
        batch.drop_column("seen_at")
    with op.batch_alter_table("messages") as batch:
        batch.drop_index(op.f("ix_messages_turn_id"))
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
