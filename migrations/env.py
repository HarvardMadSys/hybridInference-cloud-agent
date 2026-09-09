"""Alembic environment for the agent-owned database.

Online mode only, and no ``target_metadata``: this schema has no SQLAlchemy
models. It arrived as verbatim DDL from the gateway and is maintained as
explicit migrations, so autogenerate has nothing to compare against and would
happily propose dropping every table it cannot see.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from cloud_agent.storage.dsn import sync_sqlalchemy_url

config = context.config

ENV_DATABASE_URL = "AGENT_DATABASE_URL"


def _database_url() -> str:
    """Return the DSN to migrate, preferring the environment over the file.

    Raises:
        RuntimeError: If neither source supplies one — better than migrating
            whatever default happened to be in alembic.ini.
    """
    url = os.environ.get(ENV_DATABASE_URL) or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(f"{ENV_DATABASE_URL} must be set to run migrations")
    # The service reads this same variable through asyncpg, so it is written as
    # a bare `postgresql://` — which SQLAlchemy resolves to psycopg2, a driver
    # this project does not ship. See `cloud_agent.storage.dsn`.
    return sync_sqlalchemy_url(url)


def run_migrations_offline() -> None:
    """Emit SQL without a connection, for review or manual application."""
    context.configure(
        url=_database_url(),
        literal_binds=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # A schema-only revision must commit before a following data backfill.
        # Otherwise PostgreSQL retains its ACCESS EXCLUSIVE ALTER lock across
        # every later revision in the same upgrade command.
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
