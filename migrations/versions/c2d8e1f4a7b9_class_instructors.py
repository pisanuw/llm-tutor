"""class_instructors: a class can have more than one instructor

Until now a class had exactly one, ``classes.instructor_id``, and a co-taught
section was two classes or an admin. The new table holds every instructor
of a class, the lead included, so the scoping rules and the weekly email
read one join; ``instructor_id`` stays as the lead
(tutor/classes.py keeps the two in step).

Born RLS-enabled like the tables before it. Guarded: runs on a migrated
database, on one the app built at boot (create_all made the table, empty), and
a second time. The backfill gives every existing class a row for its lead,
whichever way the table arrived, and is a no-op once the rows exist.

Revision ID: c2d8e1f4a7b9
Revises: a7c2e9f4b1d8
Create Date: 2026-10-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2d8e1f4a7b9"
down_revision: Union[str, Sequence[str], None] = "a7c2e9f4b1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# tests/test_migrations.py checks the union of these lists across every
# revision against the ORM metadata, so a new table cannot ship unsecured.
_RLS_TABLES = ("class_instructors",)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_table("class_instructors"):
        op.create_table(
            "class_instructors",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("class_id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("added_by", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["class_id"], ["classes.id"]),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("class_id", "student_id", name="uq_class_instructor"),
        )
    for name, column in (("ix_class_instructors_class_id", "class_id"),
                         ("ix_class_instructors_student_id", "student_id")):
        if not _has_index("class_instructors", name):
            op.create_index(op.f(name), "class_instructors", [column])
    _set_rls(True)
    # Every class's lead gets a row (idempotent: only where none exists yet).
    op.execute(sa.text(
        "INSERT INTO class_instructors (class_id, student_id, added_by, created_at) "
        "SELECT c.id, c.instructor_id, c.created_by, c.created_at FROM classes c "
        "WHERE NOT EXISTS (SELECT 1 FROM class_instructors ci "
        "WHERE ci.class_id = c.id AND ci.student_id = c.instructor_id)"
    ))


def downgrade() -> None:
    _set_rls(False)
    op.drop_index(op.f("ix_class_instructors_student_id"), table_name="class_instructors")
    op.drop_index(op.f("ix_class_instructors_class_id"), table_name="class_instructors")
    op.drop_table("class_instructors")


def _set_rls(enable: bool) -> None:
    """Enable/disable RLS on this revision's tables (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    verb = "ENABLE" if enable else "DISABLE"
    for table in _RLS_TABLES:
        # Identifiers are constant literals from this file, not user input.
        op.execute(f'ALTER TABLE public."{table}" {verb} ROW LEVEL SECURITY')
