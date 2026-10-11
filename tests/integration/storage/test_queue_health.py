"""Real queue/host rows distinguish a stranded queue from normal ordering."""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import (
    _SHARED_HOST_CAPABILITIES,
    _create_job,
    _reserve_shared_chat,
    pool as pool,
    store as store,
)

from cloud_agent_host.sandbox import KATA_RUNTIME
from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_CAPABILITY

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


async def test_archive_policy_revision_is_part_of_queue_health_serving_capacity(pool, store):
    await _create_job(store)
    await _age(pool)
    await _host(store)
    policy = {
        "enabled": True,
        "region": "us-east-1",
        "kms_key_arn": (
            "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
        ),
        "expected_bucket_owner": "123456789012",
        "max_archive_bytes": 2 * 1024**3,
        "multipart_threshold_bytes": 8 * 1024**2,
        "multipart_part_bytes": 8 * 1024**2,
    }
    first = await store.host_pool.set_sandbox_archive_settings(
        expected_revision=0,
        settings=policy,
    )
    assert first is not None and first["revision"] == 1
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "unavailable"
    ] == 1

    await store.host_pool.record_host_heartbeat(
        host="runner-a",
        credential_hash="test",
        capabilities=[*_SHARED_HOST_CAPABILITIES, SANDBOX_ARCHIVE_CAPABILITY],
        sandbox_archive_settings_applied_revision=1,
        sandbox_archive_settings_revision_reported=True,
    )
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "unavailable"
    ] == 0

    second = await store.host_pool.set_sandbox_archive_settings(
        expected_revision=1,
        settings={**policy, "max_archive_bytes": 1024**3},
    )
    assert second is not None and second["revision"] == 2
    assert (await store.host_pool.queue_health(expected_deployment_sha="test-deployment"))[
        "unavailable"
    ] == 1

    await pool.execute(
        "UPDATE agent_runner_policy SET sandbox_archive_settings = '{\"enabled\": true}'::jsonb"
    )
    await store.host_pool.record_host_heartbeat(
        host="runner-a",
        credential_hash="test",
        sandbox_archive_settings_applied_revision=2,
        sandbox_archive_settings_revision_reported=True,
    )
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
