"""magic_links.email; student_id nullable

Plan step 2.9 (finding C21): requesting a magic link created a students row
for any allowed address (a whole domain can be allowed). A link now carries
the email it was sent to, and the students row is created by a successful
login, so ``magic_links.student_id`` stays empty until then.

Guarded like every revision after the baseline: startup.ensure_schema adds
the email column at boot on a database that has not run this yet.

Revision ID: f3b9d2e7a1c6
Revises: e8a3c5d1f2b4
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3b9d2e7a1c6"
down_revision: Union[str, Sequence[str], None] = "e8a3c5d1f2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# No new tables, so nothing new to enable RLS on.
_RLS_TABLES: tuple[str, ...] = ()

_INDEX = "ix_magic_links_email"


def _columns() -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("magic_links")}


def upgrade() -> None:
    if "email" not in _columns():
        op.add_column("magic_links", sa.Column("email", sa.String(length=255), nullable=True))
    if _INDEX not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("magic_links")}:
        op.create_index(_INDEX, "magic_links", ["email"])
    student_id = _columns()["student_id"]
    if not student_id["nullable"]:
        with op.batch_alter_table("magic_links") as batch:
            batch.alter_column("student_id", existing_type=student_id["type"], nullable=True)


def downgrade() -> None:
    if _INDEX in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("magic_links")}:
        op.drop_index(_INDEX, table_name="magic_links")
