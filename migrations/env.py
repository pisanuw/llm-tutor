"""Alembic environment.

Wired to the application's own engine and metadata so migrations target the
exact same database the app uses (DATABASE_URL -> Supabase Postgres in prod,
SQLite file in dev) and autogenerate diffs against tutor's models.

Run:
    python -m tutor.migrate       # what entrypoint.sh runs before the app starts
    alembic upgrade head          # the same, by hand, on a versioned database
    alembic revision --autogenerate -m "..."   # author a new migration
"""
from logging.config import fileConfig

from alembic import context

from tutor.db.models import Base, get_engine

config = context.config

if config.config_file_name is not None:
    # Keep the app's loggers on (plan step 2.8, finding C13): the default
    # disable_existing_loggers=True switched off every logger that existed,
    # which silenced app logging whenever migrations ran in-process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate support: diff migrations against the ORM models.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (alembic upgrade --sql)."""
    engine = get_engine()
    context.configure(
        url=engine.url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the app's engine."""
    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
