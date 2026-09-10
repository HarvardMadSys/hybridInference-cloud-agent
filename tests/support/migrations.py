"""Applying the agent schema in tests, the way production will.

``apply_migrations`` runs Alembic exactly as a deploy does: a fresh database
plus ``upgrade head`` must leave every store test passing, with no
application-startup DDL anywhere. ``downgrade_to`` exists for round-trip
tests of the (single) baseline revision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def dsn_from(db_config: dict[str, Any]) -> str:
    """Build a SQLAlchemy DSN from the asyncpg-style config tests already use."""
    return (
        f"postgresql+psycopg://{db_config['user']}:{db_config['password']}"
        f"@{db_config['host']}:{db_config['port']}/{db_config['database']}"
    )


def _alembic_config(db_config: dict[str, Any]):
    from alembic.config import Config

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn_from(db_config))
    return config


def apply_migrations(db_config: dict[str, Any], revision: str = "head") -> None:
    """Run ``alembic upgrade <revision>`` against the test database.

    ``head`` is what a deploy does. A named revision exists for upgrade tests:
    the only way to know a revision copes with the data the *previous* release
    wrote is to build that shape and then upgrade over it.
    """
    from alembic import command

    command.upgrade(_alembic_config(db_config), revision)


def downgrade_to(db_config: dict[str, Any], revision: str) -> None:
    """Run ``alembic downgrade <revision>`` — for down/up round-trip tests.

    A caller must upgrade back to head before its test ends: the shared
    per-worker database is expected at head by every other test.
    """
    from alembic import command

    command.downgrade(_alembic_config(db_config), revision)
