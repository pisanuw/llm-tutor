"""request_errors

Every 4xx or 5xx the app answered a student's browser with (step 0.1):
status, method, the path with no query and a magic-link token redacted,
the same-site referrer, the student when the cookie was live, the kind of
thing asked for, the user agent's class, and the reference the error page
showed. Born RLS-enabled.

Guarded like every revision after the baseline: a database the app already
booted has the table (create_all).

Revision ID: d6e1f8a2c4b7
Revises: c2d8e1f4a7b9
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d6e1f8a2c4b7"
down_revision: Union[str, Sequence[str], None] = "c2d8e1f4a7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables this revision creates, RLS-enabled from birth (see c7a41e9db830).
_RLS_TABLES = ("request_errors",)

# (index name, columns), the shape the ORM declares, so
# tests/test_migrations.py finds no drift.
_INDEXES = (
    ("ix_request_errors_ts", ["ts"]),
    ("ix_request_errors_student_id", ["student_id"]),
    ("ix_request_errors_ref", ["ref"]),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_table("request_errors"):
        op.create_table(
            "request_errors",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.Column("status", sa.Integer(), nullable=False),
            sa.Column("method", sa.String(length=8), nullable=False),
            sa.Column("path", sa.String(length=200), nullable=False),
            sa.Column("referrer_path", sa.String(length=200), nullable=True),
            sa.Column("student_id", sa.Integer(), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("agent", sa.String(length=12), nullable=False),
            sa.Column("ref", sa.String(length=8), nullable=False),
            sa.Column("detail", sa.String(length=120), nullable=True),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in _INDEXES:
        if not _has_index("request_errors", name):
            op.create_index(op.f(name), "request_errors", columns)
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    for name, _columns in reversed(_INDEXES):
        op.drop_index(op.f(name), table_name="request_errors")
    op.drop_table("request_errors")


def _set_rls(enable: bool) -> None:
    """Enable/disable RLS on this revision's tables (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    verb = "ENABLE" if enable else "DISABLE"
    for table in _RLS_TABLES:
        # Identifiers are constant literals from this file, not user input.
        op.execute(f'ALTER TABLE public."{table}" {verb} ROW LEVEL SECURITY')
