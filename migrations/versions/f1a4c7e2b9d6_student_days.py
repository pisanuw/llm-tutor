"""student_days: the per-student daily rollup

One row per student per local calendar day with any activity, folded
from the raw tables by tutor/student_days.py (step 0.9), so the
instructor pages, the Sunday email and the admin Today panel compare
students on numbers computed once. Ids and counts only. Born RLS-enabled.

Guarded like every revision after the baseline: a database the app already
booted has the table (create_all).

Revision ID: f1a4c7e2b9d6
Revises: e4b7c9d2a1f6
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a4c7e2b9d6"
down_revision: Union[str, Sequence[str], None] = "e4b7c9d2a1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables this revision creates, RLS-enabled from birth (see c7a41e9db830).
_RLS_TABLES = ("student_days",)

_INDEXES = (
    ("ix_student_days_student_id", ["student_id"]),
    ("ix_student_days_day", ["day"]),
)

# The signal columns in the model's order: a count is an Integer, an amount a Float.
_SIGNALS = (
    ("questions", "int"), ("turns", "int"), ("cost_usd", "float"), ("active_min", "float"),
    ("deck_min", "float"), ("narration_min", "float"), ("visits", "int"), ("slides_seen", "int"),
    ("lectures_opened", "int"), ("error_outcomes", "int"), ("busy_outcomes", "int"),
    ("detector_cuts", "int"), ("blocks", "int"), ("slow_turns", "int"), ("thumbs_up", "int"),
    ("thumbs_down", "int"), ("logins", "int"), ("failed_logins", "int"), ("user_agents", "int"),
    ("request_errors", "int"), ("hours_mask", "int"),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_table("student_days"):
        op.create_table(
            "student_days",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("course", sa.String(length=8), nullable=True),
            *[sa.Column(name, sa.Float() if kind == "float" else sa.Integer(), nullable=False)
              for name, kind in _SIGNALS],
            sa.Column("computed_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("student_id", "day", name="uq_student_days_student_day"),
        )
    for name, columns in _INDEXES:
        if not _has_index("student_days", name):
            op.create_index(op.f(name), "student_days", columns)
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    for name, _columns in reversed(_INDEXES):
        op.drop_index(op.f(name), table_name="student_days")
    op.drop_table("student_days")


def _set_rls(enable: bool) -> None:
    """Enable/disable RLS on this revision's tables (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    verb = "ENABLE" if enable else "DISABLE"
    for table in _RLS_TABLES:
        # Identifiers are constant literals from this file, not user input.
        op.execute(f'ALTER TABLE public."{table}" {verb} ROW LEVEL SECURITY')
