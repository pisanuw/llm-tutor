"""login_attempts; magic_links.code

2026-10-02: a student could not sign in by either method and the site
had no record of it. Two changes:

  * ``login_attempts`` — one row per step of a sign-in, whatever its outcome,
    with the host and the browser that made it (tutor.auth.attempts). The
    admin Logins tab reads it.
  * ``magic_links.code`` / ``code_attempts`` — the 6-digit code the login
    email carries beside the link. Some university mail systems rewrite
    every link through a URL scanner that fetches it on delivery, which
    used to spend the one-time link before the student could; a code typed
    on the login page cannot be spent by a scanner.

Guarded like every revision after the baseline: a database the app already
booted has the table (create_all) and the columns (startup.ensure_schema).

Revision ID: b7c2d9e4f1a3
Revises: f3b9d2e7a1c6
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c2d9e4f1a3"
down_revision: Union[str, Sequence[str], None] = "f3b9d2e7a1c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables this revision creates, RLS-enabled from birth (see c7a41e9db830).
_RLS_TABLES = ("login_attempts",)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_column("magic_links", "code"):
        op.add_column("magic_links", sa.Column("code", sa.String(length=8), nullable=True))
    if not _has_column("magic_links", "code_attempts"):
        # server_default so the ALTER runs on a table with rows; the ORM
        # default keeps new rows at 0 without relying on it.
        op.add_column("magic_links", sa.Column("code_attempts", sa.Integer(), nullable=False,
                                               server_default="0"))
    if not _has_table("login_attempts"):
        op.create_table(
            "login_attempts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("student_id", sa.Integer(), nullable=True),
            sa.Column("method", sa.String(length=16), nullable=False),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("host", sa.String(length=255), nullable=True),
            sa.Column("user_agent", sa.String(length=255), nullable=True),
            sa.Column("detail", sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, column in (("ix_login_attempts_created_at", "created_at"),
                         ("ix_login_attempts_email", "email")):
        if not _has_index("login_attempts", name):
            op.create_index(op.f(name), "login_attempts", [column])
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    op.drop_index(op.f("ix_login_attempts_email"), table_name="login_attempts")
    op.drop_index(op.f("ix_login_attempts_created_at"), table_name="login_attempts")
    op.drop_table("login_attempts")
    with op.batch_alter_table("magic_links") as batch:
        batch.drop_column("code_attempts")
        batch.drop_column("code")


def _set_rls(enable: bool) -> None:
    """Enable/disable RLS on this revision's tables (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    verb = "ENABLE" if enable else "DISABLE"
    for table in _RLS_TABLES:
        # Identifiers are constant literals from this file, not user input.
        op.execute(f'ALTER TABLE public."{table}" {verb} ROW LEVEL SECURITY')
