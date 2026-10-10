"""quarter dates on classes; the instructor's weekly-email opt-out

* ``classes.quarter_start`` / ``classes.quarter_end``: the dates the class
  runs, entered when it is created. The weekly instructor email goes out only
  on days inside them. Existing classes are backfilled with the quarter that
  was running when this landed (Sep 28 to Dec 20, 2026), so nothing stops.
* ``students.weekly_email_opt_out``: the instructor's own switch on
  /instructor; default off (the email is sent).

Guarded like the revisions before it: runs on a migrated database, on one
the app built at boot, and a second time. The downgrade leaves the columns.

Revision ID: a7c2e9f4b1d8
Revises: a9c4e7f1b2d3
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c2e9f4b1d8"
down_revision: Union[str, Sequence[str], None] = "a9c4e7f1b2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# No new tables, so nothing new to enable RLS on.
_RLS_TABLES: tuple[str, ...] = ()

# The quarter running when the columns arrived; classes made later carry their own.
_BACKFILL_START = sa.text("'2026-09-28'")
_BACKFILL_END = sa.text("'2026-12-20'")


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    fresh = not _has_column("classes", "quarter_start")
    if fresh:
        op.add_column("classes", sa.Column("quarter_start", sa.Date(), nullable=True))
    if not _has_column("classes", "quarter_end"):
        op.add_column("classes", sa.Column("quarter_end", sa.Date(), nullable=True))
    if fresh:
        classes = sa.table("classes", sa.column("quarter_start"), sa.column("quarter_end"))
        op.execute(classes.update()
                   .where(sa.and_(classes.c.quarter_start.is_(None), classes.c.quarter_end.is_(None)))
                   .values(quarter_start=_BACKFILL_START, quarter_end=_BACKFILL_END))
    if not _has_column("students", "weekly_email_opt_out"):
        # server_default so the ALTER runs on a table with rows; the ORM default
        # keeps new rows false without relying on it.
        op.add_column("students", sa.Column("weekly_email_opt_out", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))


def downgrade() -> None:
    pass
