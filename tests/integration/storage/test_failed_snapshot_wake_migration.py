"""A failed snapshot wakes the capture coordinator, on Postgres.

Migration ``0067``. A failed snapshot no longer holds back its group's later
one, so a snapshot pending behind it becomes capturable the moment it fails.
Turn settlement opened such snapshots before publication became an owner's
request; a deployment upgraded while one was publishing still holds one. The
coordinator sleeps until it is told of work; without this it waited for its
idle timeout. Negative answers use the sentinel of
``test_push_usage_and_workers``: notifications arrive in commit order.
"""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import (
    pool as pool,
    store as store,
)
from tests.integration.storage.test_lock_order import _legacy_turn_snapshot
from tests.integration.storage.test_push_usage_and_workers import _listen
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.integration.storage.test_snapshot_supersession import (
    _FIXED_PATCH,
    _REJECTED_PATCH,
    _first_turn,
    _next_turn,
    _record,
    _request,
)
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.workers.background_wake import CHANNEL as BACKGROUND

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0066_admin_job_listing_index"


async def test_a_failure_wakes_the_coordinator_for_the_snapshot_behind_it(store, pool):
    """The later turn's snapshot is captured when the earlier one fails, not later."""
    first, sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    earlier = await store.snapshot.claim_due_sandbox_snapshot()
    assert earlier is not None
    assert await _record(store, earlier, _REJECTED_PATCH) == "captured"
    second = await _next_turn(store, parent=first, sandbox=sandbox, prompt="drop the workflow")
    await _legacy_turn_snapshot(pool, sandbox["sandbox_id"], second)
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == earlier["snapshot_id"]
    background = await _listen(BACKGROUND)
    try:
        await store.snapshot.fail_snapshot_publish(
            snapshot_id=earlier["snapshot_id"],
            expected_publish_attempt_count=publication["publish_attempt_count"],
            detail="patch rejected: .github/workflows/ci.yml changes workflows",
        )
        assert await background.until_quiet(pool) == ["snapshot"]

        # What the woken coordinator finds.
        later = await store.snapshot.claim_due_sandbox_snapshot()
        assert later is not None and later["anchor_job_id"] == second["id"]
        assert await _record(store, later, _FIXED_PATCH) == "captured"
        assert await background.until_quiet(pool) == ["publish"]
    finally:
        await background.connection.close()


async def test_a_refused_capture_wakes_the_coordinator_too(store, pool):
    """A capture the host refused fails as well; nothing behind it waits for a timer."""
    first, _sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    background = await _listen(BACKGROUND)
    try:
        assert await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            detail="the workspace holds content the capture refused",
            dirty=True,
        )
        assert await background.until_quiet(pool) == ["snapshot"]
    finally:
        await background.connection.close()


async def test_the_wake_goes_with_a_downgrade_and_comes_back(store, pool):
    """Downgraded, a failure announces nothing, as before; upgraded, it wakes again."""
    first, _sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None

    async def fail(snapshot_id: int, background) -> list[str]:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_sandbox_snapshots SET state = 'failed' WHERE id = $1", snapshot_id
            )
            await conn.execute(
                "UPDATE agent_sandbox_snapshots SET state = 'capturing' WHERE id = $1",
                snapshot_id,
            )
        return await background.until_quiet(pool)

    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        background = await _listen(BACKGROUND)
        try:
            assert await fail(capture["snapshot_id"], background) == []
        finally:
            await background.connection.close()
    finally:
        apply_migrations(config)
    background = await _listen(BACKGROUND)
    try:
        assert await fail(capture["snapshot_id"], background) == ["snapshot"]
    finally:
        await background.connection.close()
