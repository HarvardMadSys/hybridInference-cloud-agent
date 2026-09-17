"""Real queue/host rows distinguish a stranded queue from normal ordering."""

from __future__ import annotations

import pytest

from cloud_agent_host.sandbox import KATA_RUNTIME
from tests.integration.storage.test_agent_job_store import (
    _SHARED_HOST_CAPABILITIES,
    _create_job,
    _reserve_shared_chat,
    pool as pool,
    store as store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _age(pool):
    await pool.execute("UPDATE agent_jobs SET updated_at = NOW() - INTERVAL '11 minutes'")


async def _host(store):
    await store.host_pool.register_host(host="runner-a", credential_hash="test", slots_total=3)
    await store.host_pool.record_host_heartbeat(
        host="runner-a",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_runtime=KATA_RUNTIME,
    )


async def test_queue_health_ignores_new_jobs_and_blocked_followups(pool, store):
    parent = await _create_job(store)
    await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"],
        user_id=parent["user_id"],
        prompt="Later",
    )
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "queued"
    ] == 0
    await _age(pool)
    assert await store.host_pool.queue_health(expected_deployment_sha="test-deployment") == {
        "queued": 1,
        "unavailable": 1,
        "running": 0,
    }
    await pool.execute("UPDATE agent_jobs SET cancel_requested = TRUE WHERE id = $1", parent["id"])
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "queued"
    ] == 0


async def test_missing_stale_wrong_generation_and_wrong_pin_are_not_serving(pool, store):
    await _create_job(store, execution_profile="untrusted-kata-v1")
    await _age(pool)
    await _host(store)
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "unavailable"
    ] == 0
    assert (await store.host_pool.queue_health(expected_deployment_sha="different"))[
        "unavailable"
    ] == 1
    await store.host_pool.register_host(host="other", credential_hash="other-test", slots_total=3)
    await pool.execute("UPDATE agent_runner_policy SET active_host = 'other' WHERE id")
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "unavailable"
    ] == 1
    await pool.execute("UPDATE agent_runner_policy SET active_host = NULL WHERE id")
    await pool.execute(
        "UPDATE agent_runner_hosts SET last_heartbeat_at = NOW() - INTERVAL '2 minutes'"
    )
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "unavailable"
    ] == 1


async def test_retained_owner_is_used_even_when_new_chats_are_pinned_elsewhere(pool, store):
    parent, _, _ = await _reserve_shared_chat(store)
    # The task is queued again on its retained owner, as after a lease retry.
    await pool.execute("UPDATE agent_jobs SET state = 'queued' WHERE id = $1", parent["id"])
    await _age(pool)
    await store.host_pool.register_host(host="new-host", credential_hash="new", slots_total=3)
    await pool.execute("UPDATE agent_runner_policy SET active_host = 'new-host' WHERE id")
    snapshot = await store.host_pool.queue_health(expected_deployment_sha="new-generation")
    assert snapshot["queued"] == 1
    assert snapshot["unavailable"] == 0
    await pool.execute(
        "UPDATE agent_runner_hosts SET status = 'offline' WHERE host = 'shared-runner'"
    )
    assert (await store.host_pool.queue_health(expected_deployment_sha="new-generation"))[
        "unavailable"
    ] == 1
