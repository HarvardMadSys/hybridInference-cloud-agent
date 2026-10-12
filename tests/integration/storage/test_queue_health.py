"""Real queue/host rows distinguish a stranded queue from normal ordering."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tests.integration.storage.test_agent_job_store import (
    _SHARED_HOST_CAPABILITIES,
    _create_job,
    _reserve_shared_chat,
    _shared_claim_attestation,
    pool as pool,
    store as store,
)

from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent_host.sandbox import KATA_RUNTIME
from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_CAPABILITY

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _age(pool):
    await pool.execute("UPDATE agent_jobs SET updated_at = NOW() - INTERVAL '11 minutes'")


async def _host(
    store,
    *,
    host: str = "runner-a",
    slots: int = 3,
    capabilities: list[str] | None = None,
    sandbox_runtime: str | None = KATA_RUNTIME,
) -> None:
    credential_hash = "test" if host == "runner-a" else f"{host}-credential"
    await store.host_pool.register_host(
        host=host,
        credential_hash=credential_hash,
        slots_total=slots,
    )
    await store.host_pool.record_host_heartbeat(
        host=host,
        capabilities=_SHARED_HOST_CAPABILITIES if capabilities is None else capabilities,
        sandbox_runtime=sandbox_runtime,
    )


async def _owned_host(
    pool,
    store,
    *,
    host: str = "autoscaled-a",
    slots: int = 2,
    capabilities: list[str] | None = None,
    sandbox_runtime: str | None = KATA_RUNTIME,
) -> str:
    allocation_id = f"allocation-{host}"
    node_id = f"node-{host}"
    await _host(
        store,
        host=host,
        slots=slots,
        capabilities=capabilities,
        sandbox_runtime=sandbox_runtime,
    )
    await pool.execute(
        """INSERT INTO agent_compute_allocations
               (id,name,provider_id,mode,management_mode,configuration,
                deployment_profile,policy,observed_state,scope_complete,observed_at)
           VALUES ($1,$2,'cloudlab','provision','manage','{}'::jsonb,
                   $3::jsonb,'{}'::jsonb,'ready',TRUE,NOW())""",
        allocation_id,
        f"Autoscaled {host}",
        f'{{"id":"profile-{host}"}}',
    )
    await pool.execute(
        """INSERT INTO agent_compute_nodes
               (id,allocation_id,provider_node_id,instance_identity,name)
           VALUES ($1,$2,$3,$3,$4)""",
        node_id,
        allocation_id,
        f"provider-{host}",
        host,
    )
    await pool.execute(
        "UPDATE agent_runner_hosts SET compute_node_id=$2 WHERE host=$1",
        host,
        node_id,
    )
    await pool.execute(
        """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
           VALUES ($1,$2,'active')""",
        allocation_id,
        f"profile-{host}",
    )
    return allocation_id


class _PauseAfterAutoscalingJobsConnection:
    """Pause one observation after its job query has established a snapshot."""

    def __init__(self, conn: Any, *, jobs_read: asyncio.Event, resume: asyncio.Event) -> None:
        self._conn = conn
        self._jobs_read = jobs_read
        self._resume = resume

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        rows = await self._conn.fetch(query, *args, **kwargs)
        if "SELECT t.execution_profile" in query:
            self._jobs_read.set()
            await self._resume.wait()
        return rows


class _AutoscalingObservationBarrierPool:
    """Delegate to the real pool while wrapping only the observation connection."""

    def __init__(self, pool: Any, *, jobs_read: asyncio.Event, resume: asyncio.Event) -> None:
        self._pool = pool
        self._jobs_read = jobs_read
        self._resume = resume

    @asynccontextmanager
    async def acquire(self):
        async with self._pool.acquire() as conn:
            yield _PauseAfterAutoscalingJobsConnection(
                conn,
                jobs_read=self._jobs_read,
                resume=self._resume,
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


async def test_autoscaling_observes_only_new_groups_and_effective_free_slots(pool, store):
    await _create_job(store)
    await _host(store)
    initial = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )
    assert initial["active_host"] is None
    assert initial["runnable_new_group_jobs"] == 1
    assert initial["fresh_effective_free_slots"] == 3
    assert initial["unmatched_new_group_jobs"] == 0
    assert initial["autoscaling_template_hosts"] == []
    assert initial["observed_at"] is not None
    assert [row["host"] for row in initial["hosts"]] == ["runner-a"]

    await store.host_pool.set_host_slots_desired(host="runner-a", slots=2)
    await pool.execute("UPDATE agent_runner_hosts SET slots_used=1 WHERE host='runner-a'")
    limited = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )
    assert limited["fresh_effective_free_slots"] == 1

    await pool.execute(
        "UPDATE agent_runner_hosts SET last_heartbeat_at=NOW() - INTERVAL '91 seconds' "
        "WHERE host='runner-a'"
    )
    stale = await store.host_pool.autoscaling_observation(expected_deployment_sha="test-deployment")
    assert stale["fresh_effective_free_slots"] == 0
    assert stale["unmatched_new_group_jobs"] == 1
    assert stale["autoscaling_template_hosts"] == []


async def test_autoscaling_observation_keeps_queue_and_slots_in_one_snapshot(pool, store):
    job = await _create_job(store)
    await _host(store, slots=1)
    jobs_read = asyncio.Event()
    resume = asyncio.Event()
    observation_repository = PostgresHostPoolRepository(
        _AutoscalingObservationBarrierPool(pool, jobs_read=jobs_read, resume=resume)
    )
    observation_task = asyncio.create_task(
        observation_repository.autoscaling_observation(expected_deployment_sha="test-deployment")
    )

    await asyncio.wait_for(jobs_read.wait(), timeout=10)
    claim = None
    committed = None
    try:
        claim = await store.attempt_commands.claim_job(
            worker_id="snapshot-race-worker",
            lease_ttl_seconds=60,
            host="runner-a",
            host_credential_hash="test",
            claim_attestation=_shared_claim_attestation(),
            expected_deployment_sha="test-deployment",
        )
        committed = await pool.fetchrow(
            """SELECT j.state, h.slots_used
               FROM agent_jobs j CROSS JOIN agent_runner_hosts h
               WHERE j.id=$1 AND h.host='runner-a'""",
            job["id"],
        )
    finally:
        resume.set()

    snapshot = await asyncio.wait_for(observation_task, timeout=10)
    assert claim is not None
    assert committed is not None
    assert committed["state"] == "running"
    assert committed["slots_used"] == 1
    # READ COMMITTED would combine the pre-claim job with post-claim capacity,
    # inventing one unmatched job even though no such database state existed.
    assert snapshot["runnable_new_group_jobs"] == 1
    assert snapshot["fresh_effective_free_slots"] == 1
    assert snapshot["unmatched_new_group_jobs"] == 0


async def test_retained_queue_never_creates_autoscaling_pressure(pool, store):
    parent, _, _ = await _reserve_shared_chat(store)
    await pool.execute("UPDATE agent_jobs SET state='queued' WHERE id=$1", parent["id"])

    snapshot = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )

    assert snapshot["runnable_new_group_jobs"] == 0
    assert snapshot["unmatched_new_group_jobs"] == 0


async def test_autoscaling_does_not_treat_incompatible_manual_slots_as_capacity(store):
    await _create_job(
        store,
        execution_profile="trusted-v1",
        metadata={"requires": ["special-runtime"]},
    )
    await _host(store, slots=3)

    snapshot = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )

    assert snapshot["fresh_effective_free_slots"] == 3
    assert snapshot["unmatched_new_group_jobs"] == 1
    assert snapshot["autoscaling_template_hosts"] == []


async def test_autoscaling_requires_kata_for_untrusted_work(store):
    await _create_job(store, execution_profile="untrusted-kata-v1")
    await _host(store, slots=1, sandbox_runtime="runc")

    docker = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )
    assert docker["fresh_effective_free_slots"] == 1
    assert docker["unmatched_new_group_jobs"] == 1

    await store.host_pool.record_host_heartbeat(
        host="runner-a",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_runtime=KATA_RUNTIME,
    )
    kata = await store.host_pool.autoscaling_observation(expected_deployment_sha="test-deployment")
    assert kata["unmatched_new_group_jobs"] == 0


async def test_autoscaling_matches_capacity_not_just_host_presence(store):
    await _create_job(store, execution_profile="trusted-v1")
    await _create_job(store, execution_profile="trusted-v1")
    await _host(store, slots=1)

    snapshot = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )

    assert snapshot["runnable_new_group_jobs"] == 2
    assert snapshot["fresh_effective_free_slots"] == 1
    assert snapshot["unmatched_new_group_jobs"] == 1


async def test_autoscaling_uses_maximum_matching_for_generic_and_constrained_work(store):
    await _create_job(
        store,
        execution_profile="trusted-v1",
        metadata={"requires": ["common-runtime"]},
    )
    await _create_job(
        store,
        execution_profile="trusted-v1",
        metadata={"requires": ["common-runtime", "special-runtime"]},
    )
    await _host(
        store,
        host="flexible",
        slots=1,
        capabilities=[*_SHARED_HOST_CAPABILITIES, "common-runtime", "special-runtime"],
    )
    await _host(
        store,
        host="common-only",
        slots=1,
        capabilities=[*_SHARED_HOST_CAPABILITIES, "common-runtime"],
    )

    snapshot = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )

    assert snapshot["runnable_new_group_jobs"] == 2
    assert snapshot["fresh_effective_free_slots"] == 2
    assert snapshot["unmatched_new_group_jobs"] == 0


async def test_owned_template_filters_demand_that_identical_nodes_cannot_run(pool, store):
    await _create_job(
        store,
        execution_profile="trusted-v1",
        metadata={"requires": ["special-runtime"]},
    )
    await _owned_host(pool, store)

    snapshot = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )

    assert snapshot["runnable_new_group_jobs"] == 1
    assert snapshot["autoscaling_template_hosts"] == ["autoscaled-a"]
    assert snapshot["unmatched_new_group_jobs"] == 0


async def test_launch_failing_owned_host_is_not_capacity_or_template(pool, store):
    await _create_job(store, execution_profile="trusted-v1")
    await _owned_host(pool, store, slots=1)
    await store.host_pool.record_host_heartbeat(
        host="autoscaled-a",
        launch_error="executor launch failed",
    )

    snapshot = await store.host_pool.autoscaling_observation(
        expected_deployment_sha="test-deployment"
    )

    assert snapshot["fresh_effective_free_slots"] == 0
    assert snapshot["autoscaling_template_hosts"] == []
    assert snapshot["unmatched_new_group_jobs"] == 1


async def test_stale_and_held_owned_hosts_are_not_capacity_or_templates(pool, store):
    await _create_job(store, execution_profile="trusted-v1")
    allocation_id = await _owned_host(pool, store, slots=1)
    await pool.execute(
        "UPDATE agent_runner_hosts SET last_heartbeat_at=NOW() - INTERVAL '91 seconds' "
        "WHERE host='autoscaled-a'"
    )

    stale = await store.host_pool.autoscaling_observation(expected_deployment_sha="test-deployment")
    assert stale["fresh_effective_free_slots"] == 0
    assert stale["autoscaling_template_hosts"] == []
    assert stale["unmatched_new_group_jobs"] == 1

    await store.host_pool.record_host_heartbeat(
        host="autoscaled-a",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_runtime=KATA_RUNTIME,
    )
    await pool.execute(
        """INSERT INTO agent_host_admission_holds
               (host,allocation_id,purpose,source_id,scope)
           VALUES ('autoscaled-a',$1,'autoscaling_slots','test-hold','all_claims')""",
        allocation_id,
    )
    held = await store.host_pool.autoscaling_observation(expected_deployment_sha="test-deployment")
    assert held["fresh_effective_free_slots"] == 0
    assert held["autoscaling_template_hosts"] == []
    assert held["unmatched_new_group_jobs"] == 1
