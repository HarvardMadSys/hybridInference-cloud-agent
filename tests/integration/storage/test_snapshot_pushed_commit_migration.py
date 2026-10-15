"""A snapshot's pushed commit has its own column, on Postgres.

Migration ``0081``. The publisher records the commit it is about to push so
that the group's next publication from the same base builds on it. That record
used to share ``published_commit_sha`` with the published commit. The upgrade
copies an unpublished snapshot's commit into ``pushed_commit_sha``; the
downgrade copies it back. ``deploy.sh`` migrates before it replaces the API, so
the release still running keeps recording in the old column meanwhile, and the
next claim reads both.
"""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import (
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.integration.storage.test_snapshot_publish_retry import (
    _capture_settled_workspace,
    _insert_later_snapshot,
)
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0080_user_ssh_servers"


async def test_an_unpublished_push_is_copied_to_its_own_column_and_back(store, pool):
    """Down, the push is where the previous release reads it; up, the new column has it too."""
    job, capture, _patch = await _capture_settled_workspace(store, "migrated")
    failed = await store.snapshot.claim_for_publish()
    assert failed is not None
    pushed = "c" * 40
    assert await store.snapshot.record_snapshot_push(
        snapshot_id=failed["snapshot_id"],
        expected_publish_attempt_count=failed["publish_attempt_count"],
        commit_sha=pushed,
    )
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=failed["snapshot_id"],
        expected_publish_attempt_count=failed["publish_attempt_count"],
        detail="GitHub refused the draft PR (422)",
    )
    await _insert_later_snapshot(
        pool, job=job, capture=capture, patch="diff --git a/later.txt b/later.txt\n"
    )
    later = await store.snapshot.claim_for_publish()
    assert later is not None
    published = "d" * 40
    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=later["snapshot_id"],
        expected_publish_attempt_count=later["publish_attempt_count"],
        pr_url="https://example.test/pull/1",
        commit_sha=published,
    )

    async def published_commits() -> dict[int, str | None]:
        rows = await pool.fetch(
            "SELECT id, published_commit_sha FROM agent_sandbox_snapshots WHERE id = ANY($1)",
            [failed["snapshot_id"], later["snapshot_id"]],
        )
        return {int(row["id"]): row["published_commit_sha"] for row in rows}

    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        assert await published_commits() == {
            failed["snapshot_id"]: pushed,
            later["snapshot_id"]: published,
        }
        apply_migrations(config)
        # Left in place for the release that is still running until replaced.
        assert await published_commits() == {
            failed["snapshot_id"]: pushed,
            later["snapshot_id"]: published,
        }
        assert (
            await pool.fetchval(
                "SELECT pushed_commit_sha FROM agent_sandbox_snapshots WHERE id = $1",
                failed["snapshot_id"],
            )
            == pushed
        )
    finally:
        apply_migrations(config)


async def test_a_push_the_replaced_release_records_after_the_migration_still_counts(store, pool):
    """Between the migration and the API's replacement, the old publisher writes the old column."""
    job, capture, _patch = await _capture_settled_workspace(store, "rolling")
    claimed = await store.snapshot.claim_for_publish()
    assert claimed is not None
    old_release_push = "e" * 40
    async with pool.acquire() as conn:
        # What the previous release's record_snapshot_push writes.
        await conn.execute(
            """
            UPDATE agent_sandbox_snapshots SET published_commit_sha = $2
            WHERE id = $1 AND state = 'publishing'
            """,
            claimed["snapshot_id"],
            old_release_push,
        )
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=claimed["snapshot_id"],
        expected_publish_attempt_count=claimed["publish_attempt_count"],
        detail="GitHub refused the draft PR (422)",
    )
    await _insert_later_snapshot(
        pool, job=job, capture=capture, patch="diff --git a/later.txt b/later.txt\n"
    )

    later = await store.snapshot.claim_for_publish()

    assert later is not None and later["own_pushes"] == [old_release_push]
