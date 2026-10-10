"""turn_events.cache_write_tokens

Prompt-cache writes in the cost ledger (plan step 2.1).
startup.ensure_schema adds the same column at boot on a database that
predates it, so the add is guarded: a database that already has the column
is left alone.

Revision ID: d4f7b2c9e813
Revises: c7a41e9db830
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f7b2c9e813"
down_revision: Union[str, Sequence[str], None] = "c7a41e9db830"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# No new tables, so nothing new to enable RLS on.
_RLS_TABLES: tuple[str, ...] = ()


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("turn_events", "cache_write_tokens"):
        op.add_column("turn_events", sa.Column("cache_write_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _has_column("turn_events", "cache_write_tokens"):
        op.drop_column("turn_events", "cache_write_tokens")
