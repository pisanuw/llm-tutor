"""instructor role and classes

Adds the third user kind:
  * students.is_instructor — the runtime-granted role flag
  * classes               — one instructor's named roster in one course
  * class_enrollments     — the many-to-many roster rows

Both new tables are born RLS-enabled (Supabase exposes the public schema over
PostgREST with the anon key; a table created RLS-disabled is world-readable).
The app connects as the owner role, which bypasses RLS, so enabling it with no
policies locks out anon/authenticated and leaves the app untouched.

Every step is guarded (plan step 2.8, finding C6): a database the app already
booted has the column (startup.ensure_schema) and the tables (create_all), and
an unguarded add raised DuplicateColumn there.

Revision ID: c7a41e9db830
Revises: bb5bc75ceadf
Create Date: 2026-09-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7a41e9db830"
down_revision: Union[str, Sequence[str], None] = "bb5bc75ceadf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables this revision creates, which must be RLS-enabled from birth.
# tests/test_migrations.py checks the union of these lists across every
# revision against the ORM metadata, so a new table cannot ship unsecured.
_RLS_TABLES = ("classes", "class_enrollments")


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    # server_default so the ALTER can run on a table that already has rows;
    # the ORM default keeps new rows false without relying on it.
    if not _has_column("students", "is_instructor"):
        op.add_column(
            "students",
            sa.Column("is_instructor", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
        )
    if not _has_table("classes"):
        op.create_table(
            "classes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("course", sa.String(length=8), nullable=False),
            sa.Column("term", sa.String(length=64), nullable=False),
            sa.Column("instructor_id", sa.Integer(), nullable=False),
            sa.Column("archived", sa.Boolean(), nullable=False),
            sa.Column("weekly_report", sa.Boolean(), nullable=False),
            sa.Column("created_by", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["instructor_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("classes", "ix_classes_instructor_id"):
        op.create_index(op.f("ix_classes_instructor_id"), "classes", ["instructor_id"])
    if not _has_table("class_enrollments"):
        op.create_table(
            "class_enrollments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("class_id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["class_id"], ["classes.id"]),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("class_id", "student_id", name="uq_class_enrollment"),
        )
    for name, column in (("ix_class_enrollments_class_id", "class_id"),
                         ("ix_class_enrollments_student_id", "student_id")):
        if not _has_index("class_enrollments", name):
            op.create_index(op.f(name), "class_enrollments", [column])
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    op.drop_index(op.f("ix_class_enrollments_student_id"), table_name="class_enrollments")
    op.drop_index(op.f("ix_class_enrollments_class_id"), table_name="class_enrollments")
    op.drop_table("class_enrollments")
    op.drop_index(op.f("ix_classes_instructor_id"), table_name="classes")
    op.drop_table("classes")
    op.drop_column("students", "is_instructor")


def _set_rls(enable: bool) -> None:
    """Enable/disable RLS on this revision's tables (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    verb = "ENABLE" if enable else "DISABLE"
    for table in _RLS_TABLES:
        # Identifiers are constant literals from this file, not user input.
        op.execute(f'ALTER TABLE public."{table}" {verb} ROW LEVEL SECURITY')
