"""The rollout snapshot names the Hosts the control plane rolls forward itself."""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store
from tests.integration.storage.test_queue_health import _host, _owned_host

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _follows(store) -> dict[str, bool]:
    rows, _, _, _ = await store.host_pool.deployment_runner_pool()
    return {row["host"]: row["release_follows_control_plane"] for row in rows}


async def test_only_a_present_managed_allocation_follows_the_control_plane(pool, store):
    await _host(store, host="runner-a")
    await _owned_host(pool, store, host="compute-managed")
    observed = await _owned_host(pool, store, host="compute-observed")
    await pool.execute(
        "UPDATE agent_compute_allocations SET management_mode='observe' WHERE id=$1", observed
    )
    released = await _owned_host(pool, store, host="compute-released")
    await pool.execute(
        "UPDATE agent_compute_allocations SET release_committed_at=NOW() WHERE id=$1", released
    )

    assert await _follows(store) == {
        "runner-a": False,
        "compute-managed": True,
        "compute-observed": False,
        "compute-released": False,
    }
