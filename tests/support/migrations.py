"""Applying the agent schema in tests, the way production will.

Two entry points, because two different properties are under test.

``apply_migrations`` runs Alembic exactly as a deploy does. That is E1's
acceptance: a fresh database plus ``upgrade head`` must leave every store test
passing, with no application-startup DDL anywhere.

``run_baseline_statements`` executes the baseline's statements directly,
bypassing Alembic's version table. Alembic will not re-run a revision it has
already recorded, so it cannot be used to test that the statements are
*themselves* idempotent — and they must be, because the source ran them on
every single boot.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "migrations/versions/0001_baseline.py"


def baseline_statements() -> tuple[str, ...]:
    """Return the baseline migration's statements, read without importing it.

    Read as data rather than imported: importing pulls in ``alembic.op``, which
    is only bound inside a migration context.
    """
    tree = ast.parse(BASELINE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", None)
        elif isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", None)
        if target == "_STATEMENTS":
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("_STATEMENTS not found in the baseline migration")


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


def apply_migrations(db_config: dict[str, Any]) -> None:
    """Run ``alembic upgrade head`` against the test database."""
    from alembic import command

    command.upgrade(_alembic_config(db_config), "head")


def downgrade_to(db_config: dict[str, Any], revision: str) -> None:
    """Run ``alembic downgrade <revision>`` — for down/up round-trip tests.

    A caller must upgrade back to head before its test ends: the shared
    per-worker database is expected at head by every other test.
    """
    from alembic import command

    command.downgrade(_alembic_config(db_config), revision)


async def run_baseline_statements(pool: Any) -> None:
    """Execute the baseline's statements directly, for idempotency tests."""
    async with pool.acquire() as conn:
        for statement in baseline_statements():
            await conn.execute(statement)
