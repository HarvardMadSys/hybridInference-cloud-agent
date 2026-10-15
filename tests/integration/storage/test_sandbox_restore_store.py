"""Real Postgres checks for moving a stopped group to another host."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from tests.integration.storage.test_agent_job_store import (
    _SHARED_HOST_CAPABILITIES,
    _create_job,
    _shared_claim_attestation,
    pool as pool,
    store as store,
)

from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_CAPABILITY
from cloud_agent_shared.sandbox_restore import MAX_RESTORE_ATTEMPTS, SANDBOX_RESTORE_CAPABILITY

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BASE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
_ARCHIVES = {
    "enabled": True,
    "region": "us-east-1",
    "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
    "expected_bucket_owner": "123456789012",
    "max_archive_bytes": 2 * 1024**3,
    "multipart_threshold_bytes": 8 * 1024**2,
    "multipart_part_bytes": 8 * 1024**2,
}
_ARCHIVE_HOST = [*_SHARED_HOST_CAPABILITIES, SANDBOX_ARCHIVE_CAPABILITY]
_RESTORE_HOST = [*_ARCHIVE_HOST, SANDBOX_RESTORE_CAPABILITY]


async def _enable_archives(store: Storage) -> None:
    configured = await store.host_pool.set_sandbox_archive_settings(
        expected_revision=0, settings=_ARCHIVES
    )
    assert configured is not None and configured["revision"] == 1


async def _host(store: Storage, host: str, capabilities: list[str]) -> None:
    await store.host_pool.register_host(
        host=host,
        credential_hash=f"{host}-hash",
        capabilities=capabilities,
        slots_total=8,
    )
    await store.host_pool.record_host_heartbeat(
        host=host,
        credential_hash=f"{host}-hash",
        capabilities=capabilities,
        sandbox_image_digest=_BASE,
        sandbox_archive_settings_applied_revision=1,
        sandbox_archive_settings_revision_reported=True,
    )


async def _claim(store: Storage, host: str, capabilities: list[str]) -> dict | None:
    return await store.attempt_commands.claim_job(
        worker_id=f"{host}-worker",
        lease_ttl_seconds=60,
        host=host,
        host_credential_hash=f"{host}-hash",
        claim_attestation={
            **_shared_claim_attestation(),
            "sandbox_image_digest": _BASE,
            "runner_capabilities": capabilities,
            "sandbox_archive_settings_applied_revision": 1,
        },
    )


def _fence(job: dict, claim: dict, sandbox: dict) -> dict[str, Any]:
    return {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }


async def _reserve(store: Storage, job: dict, claim: dict, host: str) -> dict:
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=host,
        sandbox_id="asbx_restore",
        container_name="freeinference-chat-restore",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
    )
    assert sandbox is not None
    return sandbox


async def _settled_stopped_group(pool, store: Storage) -> tuple[dict, dict]:
    """One chat that ran a turn on ``runner-a`` and whose container stopped."""
    await _enable_archives(store)
    await _host(store, "runner-a", _ARCHIVE_HOST)
    job = await _create_job(store)
    claim = await _claim(store, "runner-a", _ARCHIVE_HOST)
    assert claim is not None and claim["id"] == job["id"]
    sandbox = await _reserve(store, job, claim, "runner-a")
    residency = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        sandbox_id=sandbox["sandbox_id"],
        runtime_id="claude-code",
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
    )
    assert residency is not None
    fence = _fence(job, claim, sandbox)
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-a", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=residency["generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=residency["generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-restore",
        idle_timeout_seconds=15,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', idle_deadline = NULL WHERE id = $1",
            "asbx_restore",
        )
    group = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert group is not None and group["state"] == "stopped"
    return job, group


def _receipt(group: dict, job: dict, **changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "checkpoint_id": "c" * 32,
        "sandbox_id": group["sandbox_id"],
        "generation": group["generation"],
        "container_id": "container-a",
        "base_image": _BASE,
        "runtime_id": "claude-code",
        "object_key": "cloud-agent/sandbox/v2/" + "d" * 64 + "/g1/" + "c" * 32 + ".oci.tar",
        "version_id": "version-1",
        "sha256": "e" * 64,
        "size_bytes": 4096,
        "new_layer_bytes": 2048,
        "layer_count": 3,
        "created_at": datetime(2026, 10, 14, 12, tzinfo=UTC).isoformat(),
        "members": [
            {
                "thread_id": job["thread_id"],
                "native_session_id": "native-restore",
                "last_settled_job_id": job["id"],
                "context_status": "live",
            }
        ],
        "format_version": 1,
        "start_seq": group["start_seq"],
    }
    values.update(changes)
    return values


async def _covered(pool, store: Storage) -> tuple[dict, dict]:
    job, group = await _settled_stopped_group(pool, store)
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT restore_checkpoint_id FROM agent_sandboxes WHERE id = $1",
                group["sandbox_id"],
            )
            == "c" * 32
        )
    return job, group


async def _state(pool, sandbox_id: str = "asbx_restore") -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, host, generation, container_id, restore_checkpoint_id, "
            "restore_attempts, restore_attempt_id FROM agent_sandboxes WHERE id = $1",
            sandbox_id,
        )
    assert row is not None
    return dict(row)


async def test_a_removed_hosts_covered_group_is_restored_on_another_host(pool, store: Storage):
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None

    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"

    # A host that cannot restore never takes the group.
    await _host(store, "runner-b", _ARCHIVE_HOST)
    assert await _claim(store, "runner-b", _ARCHIVE_HOST) is None

    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    carried = claim["thread_sandbox"]
    assert carried["state"] == "restoring"
    assert carried["host"] == "runner-c"
    assert carried["generation"] == 2
    assert carried["container_id"] is None
    assert carried["restore"]["checkpoint_id"] == "c" * 32
    assert carried["restore"]["base_image"] == _BASE
    assert carried["restore"]["sha256"] == "e" * 64
    assert (await _state(pool))["restore_attempt_id"] == claim["attempt_id"]

    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert reserved["state"] == "restoring"
    assert reserved["restore"]["checkpoint_id"] == "c" * 32
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )

    restored = await _state(pool)
    assert restored == {
        "state": "running",
        "host": "runner-c",
        "generation": 2,
        "container_id": "container-c",
        "restore_checkpoint_id": None,
        "restore_attempts": 0,
        "restore_attempt_id": None,
    }
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM agent_sandbox_checkpoints WHERE id = $1", "c" * 32
            )
            == "verified"
        )


async def test_a_stale_host_cannot_touch_a_rehomed_group(pool, store: Storage):
    job, group = await _covered(pool, store)
    await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    assert await _claim(store, "runner-c", _RESTORE_HOST) is not None

    # The old host's view of generation 1 changes nothing, running or not.
    for running in (True, False):
        assert (
            await store.sandbox.reconcile_thread_sandbox_observation(
                thread_id=job["thread_id"],
                host="runner-a",
                sandbox_id=group["sandbox_id"],
                container_id="container-a",
                sandbox_generation=1,
                running=running,
            )
            is None
        )
    assert not await store.sandbox.record_sandbox_checkpoint(
        host="runner-a",
        thread_id=job["thread_id"],
        checkpoint=_receipt(group, job, checkpoint_id="f" * 32),
    )
    # Nor does the new host's container before the restore confirms it.
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            thread_id=job["thread_id"],
            host="runner-c",
            sandbox_id=group["sandbox_id"],
            container_id="container-early",
            sandbox_generation=2,
            running=True,
        )
        is None
    )
    current = await _state(pool)
    assert current["state"] == "restoring"
    assert current["container_id"] is None


async def test_a_failed_restore_returns_to_the_checkpoint_until_attempts_run_out(
    pool, store: Storage
):
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)

    for attempt in range(1, MAX_RESTORE_ATTEMPTS + 1):
        claim = await _claim(store, "runner-c", _RESTORE_HOST)
        assert claim is not None and claim["id"] == follow_up["id"]
        assert await store.attempt_commands.release_claim(
            job_id=follow_up["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            reason="sandbox_restore_failed",
        )
        state = await _state(pool)
        assert state["restore_attempts"] == attempt
        assert state["restore_attempt_id"] is None
        assert state["container_id"] is None
        if attempt < MAX_RESTORE_ATTEMPTS:
            assert state["state"] == "cold"
            assert (await store.conversation_queries.get_job(follow_up["id"]))["state"] == "queued"

    assert (await _state(pool))["state"] == "lost"
    failed = await store.conversation_queries.get_job(follow_up["id"])
    assert failed is not None and failed["state"] == "failed"
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None


async def test_an_expired_restore_returns_the_group_and_requeues_the_turn(pool, store: Storage):
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None
    await _reserve(store, follow_up, claim, "runner-c")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            claim["attempt_id"],
        )

    await store.attempt_commands.reap_expired()

    state = await _state(pool)
    assert state["state"] == "cold"
    assert state["restore_attempt_id"] is None
    requeued = await store.conversation_queries.get_job(follow_up["id"])
    assert requeued is not None and requeued["state"] == "queued"
    member = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert member is not None
    # Nothing ran: the chat's context is the checkpoint's, not lost.
    assert member["context_status"] != "lost"
    assert member["active_attempt_id"] is None
    retry = await _claim(store, "runner-c", _RESTORE_HOST)
    assert retry is not None and retry["thread_sandbox"]["generation"] == 3


async def test_a_turn_finished_during_restore_keeps_the_context(pool, store: Storage):
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None
    await _reserve(store, follow_up, claim, "runner-c")

    assert await store.attempt_commands.transition(
        job_id=follow_up["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="restore failed",
    )

    assert (await _state(pool))["state"] == "cold"
    member = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert member is not None and member["context_status"] != "lost"


@pytest.mark.parametrize(
    "spoil",
    ["stale_sequence", "no_sequence", "local_image", "unsettled_member", "started_after"],
)
async def test_a_group_its_checkpoint_does_not_cover_stays_on_its_host(
    pool, store: Storage, spoil: str
):
    job, group = await _settled_stopped_group(pool, store)
    changes: dict[str, Any] = {}
    if spoil == "stale_sequence":
        changes["start_seq"] = group["start_seq"] - 1
    elif spoil == "no_sequence":
        changes["start_seq"] = None
    elif spoil == "local_image":
        changes["base_image"] = "cloud-agent-sandbox@sha256:" + "a" * 64
    elif spoil == "unsettled_member":
        changes["members"] = [{"thread_id": job["thread_id"], "last_settled_job_id": None}]
    await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job, **changes)
    )
    if spoil == "started_after":
        # Opening the workspace starts the container after the checkpoint.
        assert await store.sandbox.fence_workspace_start(thread_id=job["thread_id"]) == "stopped"

    assert await store.host_pool.remove_host(host="runner-a") == "ok"

    state = await _state(pool)
    assert state["state"] == "stopped"
    assert state["host"] == "runner-a"


async def test_a_repeated_receipt_cannot_cover_the_group_again(pool, store: Storage):
    job, group = await _covered(pool, store)
    assert await store.sandbox.fence_workspace_start(thread_id=job["thread_id"]) == "stopped"

    # The next beat repeats the same receipt; it must not restore the point.
    assert not await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )

    assert (await _state(pool))["restore_checkpoint_id"] is None


async def test_a_running_observation_drops_the_restore_point(pool, store: Storage):
    job, group = await _covered(pool, store)

    await store.sandbox.reconcile_thread_sandbox_observation(
        thread_id=job["thread_id"],
        host="runner-a",
        sandbox_id=group["sandbox_id"],
        container_id="container-a",
        sandbox_generation=1,
        running=True,
    )

    assert (await _state(pool))["restore_checkpoint_id"] is None


async def test_workspace_requests_wait_for_a_cold_group(pool, store: Storage):
    job, _ = await _covered(pool, store)
    assert await store.host_pool.remove_host(host="runner-a") == "ok"

    assert await store.sandbox.fence_workspace_start(thread_id=job["thread_id"]) == "cold"
    # The fence never discards the checkpoint a cold group is restored from.
    assert (await _state(pool))["restore_checkpoint_id"] == "c" * 32
