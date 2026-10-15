"""A snapshot's pushed commit has its own column, on Postgres.

Migration ``0081``. The publisher records the commit it is about to push so
that the group's next publication from the same base builds on it. That record
used to share ``published_commit_sha`` with the published commit. The upgrade
moves an unpublished snapshot's commit into ``pushed_commit_sha`` and leaves a
published one where it is; the downgrade moves it back.
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


async def test_an_unpublished_push_moves_to_its_own_column_and_back(store, pool):
    """Down, the push is where the previous release reads it; up, it is apart again."""
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
        assert await published_commits() == {
            failed["snapshot_id"]: None,
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
