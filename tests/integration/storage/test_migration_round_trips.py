"""Every revision can be rolled back one step and applied again.

A rollback is a recovery path only if the revision it undid can be applied
again afterwards. ``0020`` could not be: its upgrade dropped a constraint by a
name its own ``downgrade`` never re-created, so ``upgrade -> downgrade ->
upgrade`` failed even on an empty database, and no test went back up. Walking
the whole chain one step at a time is the check that finds that, and it is
cheap enough to run for every revision.
"""

from __future__ import annotations

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import REPO, apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _oldest_first() -> list[Script]:
    """Every revision, each after all of its ancestors."""
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    return list(reversed(list(ScriptDirectory.from_config(config).walk_revisions())))


async def _empty_schema(config: dict[str, object]) -> None:
    """Start from nothing: the walk has to begin below the first revision."""
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
    finally:
        await conn.close()


async def test_every_revision_rolls_back_one_step_and_applies_again() -> None:
    """Upgrade to each revision, step down to its parent, and upgrade again."""
    config = _db_config()
    await _empty_schema(config)
    try:
        for revision in _oldest_first():
            name = revision.revision
            parent = revision.down_revision
            apply_migrations(config, name)
            if isinstance(parent, tuple):
                # A merge: one step down is ambiguous, and each parent it
                # joins has already been walked on its own.
                continue
            try:
                downgrade_to(config, "base" if parent is None else parent)
                apply_migrations(config, name)
            except Exception as exc:
                pytest.fail(f"{name} cannot be rolled back one step and applied again: {exc}")
    finally:
        # Whatever happened, the shared per-worker database is expected at head.
        await _empty_schema(config)
        apply_migrations(config, "head")
