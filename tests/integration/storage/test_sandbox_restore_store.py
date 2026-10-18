"""Real Postgres checks for moving a stopped group to another host."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from tests.integration.storage.test_agent_job_store import (
    _SHARED_HOST_CAPABILITIES,
    _create_job,
    _shared_claim_attestation,
    _waiting_on,
    pool as pool,
    store as store,
)

from cloud_agent.storage._sql.compute_admission import host_compute_admission
from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_CAPABILITY
from cloud_agent_shared.sandbox_restore import (
    MAX_RESTORE_ATTEMPTS,
    RESTORE_RETRY_DELAYS_S,
    SANDBOX_RESTORE_CAPABILITY,
)

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


async def _age_restore_failure(pool, seconds: float, sandbox_id: str = "asbx_restore") -> None:
    """Move the group's last failed restore ``seconds`` further into the past."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes "
            "SET last_activity_at = last_activity_at - make_interval(secs => $2) WHERE id = $1",
            sandbox_id,
            seconds,
        )


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


async def test_a_moved_group_is_reached_on_its_new_host_from_any_of_its_turns(pool, store: Storage):
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )

    # A workspace request, a capture or a base import may name a turn that
    # ran before the move, such as a sibling chat's latest one. Its attempt's
    # host no longer holds the group; the request must reach the group itself.
    for named in (job["id"], follow_up["id"]):
        owner = await store.host_pool.host_for_job(job_id=named)
        assert owner is not None
        assert owner["host"] == "runner-c"
        assert owner["status"] == "online"


async def _expiring_host(store: Storage, host: str, capabilities: list[str]) -> None:
    """A restore-capable Host on a managed machine whose lease ends in 20 minutes."""
    repo = store.compute_resources
    request = f"allocation-{host}"
    op = await repo.create_allocation_operation(
        request_id=request,
        fingerprint=request,
        kind="add",
        allocation={
            "name": host,
            "provider_id": "cloudlab",
            "mode": "attach",
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
        },
        parameters={},
        requested_by="admin",
    )
    claimed = await repo.claim_operation(worker_id="fixture")
    assert claimed is not None and claimed["id"] == op["id"]
    node = await repo.upsert_node(
        allocation_id=op["allocation_id"],
        node={"provider_node_id": "node-1", "instance_identity": request, "name": host},
    )
    await repo.observe_allocation(
        allocation_id=op["allocation_id"],
        observation={
            "external_id": request,
            "observed_state": "ready",
            "expiration_known": True,
            # Inside the default 30 minutes before expiry in which a machine
            # takes no new group, but still claims follow-ups of its own.
            "expires_at": datetime.now(UTC) + timedelta(minutes=20),
            "scope_complete": True,
        },
    )
    await repo.prepare_bootstrap_binding(node_id=node["id"], host=host, operation_id=op["id"])
    await _host(store, host, capabilities)
    assert await repo.clear_bootstrap_hold(node_id=node["id"], host=host, operation_id=op["id"])
    assert await repo.finish_operation(
        operation_id=op["id"],
        generation=claimed["generation"],
        worker_id="fixture",
        state="succeeded",
        step="ready",
    )


async def test_a_host_that_may_take_no_new_group_does_not_restore_one(pool, store: Storage):
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"

    # Restoring creates the group's only container on the claiming machine.
    # One about to expire would take the chat down with it.
    await _expiring_host(store, "runner-c", _RESTORE_HOST)
    async with pool.acquire() as conn:
        assert await host_compute_admission(conn, "runner-c") == (False, True)
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None
    assert (await _state(pool))["state"] == "cold"

    await _host(store, "runner-d", _RESTORE_HOST)
    claim = await _claim(store, "runner-d", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    assert claim["thread_sandbox"]["host"] == "runner-d"


async def _run_turn_and_stop(pool, store: Storage, parent: dict) -> dict:
    """Run one more turn of the chat on ``runner-a`` and let its container stop."""
    job = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="continue"
    )
    assert job is not None
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
        **fence, container_id="container-a", context_status="restored"
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
        base_sha=parent["base_sha"],
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
    return job


async def test_a_cold_groups_base_import_waits_for_the_restore(pool, store: Storage):
    job, _ = await _settled_stopped_group(pool, store)
    requested = await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )
    assert requested.get("requested") is True, requested
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        == "captured"
    )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    commit_sha = "b" * 40
    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        pr_url="https://example.test/pull/1",
        commit_sha=commit_sha,
    )
    # The base import does not land (GitHub may ask the retry to wait for an
    # hour), and the chat goes on: another turn, a clean stop, a checkpoint
    # of that stop. Then the host is removed while still up.
    latest = await _run_turn_and_stop(pool, store, job)
    group = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert group is not None
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a",
        thread_id=job["thread_id"],
        checkpoint=_receipt(
            group,
            job,
            members=[
                {
                    "thread_id": job["thread_id"],
                    "native_session_id": "native-restore",
                    "last_settled_job_id": latest["id"],
                    "context_status": "live",
                }
            ],
        ),
    )
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"

    # Only a checkpoint holds the group: the import must wait for the
    # restore, and an import the removed host still answered must not bring
    # the group back to life there, where no host can ever claim it again.
    assert await store.snapshot.pending_snapshot_base_import() is None
    assert not await store.snapshot.complete_snapshot_base_import(
        snapshot_id=capture["snapshot_id"],
        commit_sha=commit_sha,
        generation=capture["generation"],
        container_id=capture["container_id"],
    )
    assert (await _state(pool))["state"] == "cold"

    await _host(store, "runner-c", _RESTORE_HOST)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=latest["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    pending = await store.snapshot.pending_snapshot_base_import()
    assert pending is not None
    assert pending["generation"] == 2 and pending["container_id"] == "container-c"


async def test_an_import_answered_by_the_old_host_after_the_restore_changes_nothing(
    pool, store: Storage, monkeypatch: pytest.MonkeyPatch
):
    """The import was sent before the move and comes back after the restore was confirmed."""
    from cloud_agent import publish_worker, sandbox_manager_routing

    job, _ = await _settled_stopped_group(pool, store)
    requested = await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )
    assert requested.get("requested") is True, requested
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        == "captured"
    )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    commit_sha = "b" * 40
    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        pr_url="https://example.test/pull/1",
        commit_sha=commit_sha,
    )
    # The publisher sends the base import to runner-a, which holds the group.
    sent = await store.snapshot.pending_snapshot_base_import()
    assert sent is not None
    assert (sent["generation"], sent["container_id"]) == (1, "container-a")

    # Before runner-a answers, the chat goes on, runner-a is removed, and
    # runner-c restores the group from the checkpoint of its last stop.
    latest = await _run_turn_and_stop(pool, store, job)
    group = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert group is not None
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a",
        thread_id=job["thread_id"],
        checkpoint=_receipt(
            group,
            job,
            members=[
                {
                    "thread_id": job["thread_id"],
                    "native_session_id": "native-restore",
                    "last_settled_job_id": latest["id"],
                    "context_status": "live",
                }
            ],
        ),
    )
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=latest["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )

    # Now runner-a answers: the commit landed in its old container.
    class _OldHost:
        async def import_shared_base(self, row, *, commit_sha: str, bundle: bytes):
            assert (row["generation"], row["container_id"]) == (1, "container-a")
            return {"ok": True}

    async def route(_store, job_id: str):
        assert job_id == sent["anchor_job_id"]
        return _OldHost()

    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_job", route)

    assert not await publish_worker._import_snapshot_base(
        store, snapshot=sent, commit_sha=commit_sha, bundle=b"bundle"
    )

    # The restored container still lacks the base, and it is what the import
    # is sent to next.
    async with pool.acquire() as conn:
        importing = await conn.fetchval(
            "SELECT snapshot_state FROM agent_sandboxes WHERE id = $1", "asbx_restore"
        )
    assert importing == "importing"
    pending = await store.snapshot.pending_snapshot_base_import()
    assert pending is not None
    assert (pending["generation"], pending["container_id"]) == (2, "container-c")
    assert await store.snapshot.complete_snapshot_base_import(
        snapshot_id=pending["snapshot_id"],
        commit_sha=commit_sha,
        generation=pending["generation"],
        container_id=pending["container_id"],
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
            # The next try waits out this failure's delay, however soon the
            # queue hands the turn out again.
            assert await _claim(store, "runner-c", _RESTORE_HOST) is None
            await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[attempt - 1])

    assert (await _state(pool))["state"] == "lost"
    failed = await store.conversation_queries.get_job(follow_up["id"])
    assert failed is not None and failed["state"] == "failed"
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None


async def test_a_group_its_restores_lost_fails_every_chats_queued_turns(pool, store: Storage):
    """A Fork sibling waiting in the group gets an answer when the group is lost.

    The release that ran out the group's attempts failed the queued turns of
    its own chat only. A sibling's turn queued behind a turn that did not
    succeed -- one its owner stopped, say -- could never be claimed again and
    was never failed either: it stayed queued for good.
    """
    job, _ = await _covered(pool, store)
    sibling = await _create_job(store, task_prompt="the sibling chat")
    assert await store.attempt_commands.request_cancel(job_id=sibling["id"]) == "cancelled"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_thread_sandbox_memberships "
            "(thread_id, user_id, sandbox_id, resident_slot_id) VALUES ($1, $2, $3, 1)",
            sibling["thread_id"],
            sibling["user_id"],
            "asbx_restore",
        )
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"
    waiting = await store.conversation_commands.create_follow_up(
        parent_job_id=sibling["id"], user_id="user-1", prompt="try the sibling again"
    )
    assert waiting is not None and waiting.get("error") is None
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
        if attempt < MAX_RESTORE_ATTEMPTS:
            await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[attempt - 1])
    assert (await _state(pool))["state"] == "lost"
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None

    # The sweep every reaper pass runs answers what can never be claimed.
    await store.attempt_commands.sweep_queued_follow_ups()
    answered = await store.conversation_queries.get_job(waiting["id"])
    assert answered is not None and answered["state"] == "failed"
    assert "sandbox" in answered["detail"]


async def test_restore_failures_during_a_short_outage_do_not_lose_the_chat(pool, store: Storage):
    """Two minutes of a registry or S3 outage fail every restore tried in them."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)

    # A claim every ten seconds, as fast as the queue hands the turn out, and
    # every restore fails at once.
    for _ in range(12):
        claim = await _claim(store, "runner-c", _RESTORE_HOST)
        if claim is not None:
            assert await store.attempt_commands.release_claim(
                job_id=follow_up["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                reason="sandbox_restore_failed",
            )
        await _age_restore_failure(pool, 10)

    state = await _state(pool)
    assert state["state"] == "cold"
    assert state["restore_attempts"] == 2
    waiting = await store.conversation_queries.get_job(follow_up["id"])
    assert waiting is not None and waiting["state"] == "queued"

    # The outage is over before the next wait is.
    await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[1])
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    restored = await _state(pool)
    assert restored["state"] == "running"
    assert restored["restore_attempts"] == 0


async def test_claims_handed_back_before_the_restore_began_spend_no_attempt(pool, store: Storage):
    """A claim given back before its host reserved the group never reached the checkpoint.

    The hand-over gives a claim back when it cannot mint the turn's credentials
    or the host lacks something the turn needs, and a host gives one back when
    it cannot launch the turn. Each of them counted as a failed restore: it
    started the next wait, and the fourth lost the chat for good though nothing
    had read its checkpoint.
    """
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    # One restore did fail, and its wait has passed.
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None
    assert await store.attempt_commands.release_claim(
        job_id=follow_up["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        reason="sandbox_restore_failed",
    )
    await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[0])

    # The hand-over's reasons, then the host's.
    for reason in ("credential_unavailable", "claim_failed", "launch_failed", "storage_pressure"):
        claim = await _claim(store, "runner-c", _RESTORE_HOST)
        assert claim is not None and claim["id"] == follow_up["id"]
        assert await store.attempt_commands.release_claim(
            job_id=follow_up["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            reason=reason,
            retry_after_seconds=30,
        )
        state = await _state(pool)
        assert (state["state"], state["restore_attempts"], state["restore_attempt_id"]) == (
            "cold",
            1,
            None,
        )
        waiting = await store.conversation_queries.get_job(follow_up["id"])
        assert waiting is not None and waiting["state"] == "queued"
        # Only the queue's own delay holds the turn back: no new wait starts.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_jobs SET claim_retry_at = NULL WHERE id = $1", follow_up["id"]
            )

    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    assert (await _state(pool))["state"] == "running"


async def test_a_claim_handed_back_after_its_host_reserved_the_group_counts(pool, store: Storage):
    """Once its host reserved the group, a restore may have begun, whatever ended it."""
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

    # Grant renewal gives up on the attempt while the host restores.
    assert await store.attempt_commands.release_claim(
        job_id=follow_up["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    state = await _state(pool)
    assert (state["state"], state["restore_attempts"], state["restore_attempt_id"]) == (
        "cold",
        1,
        None,
    )
    member = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert member is not None and member["active_attempt_id"] is None
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None
    await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[0])
    retry = await _claim(store, "runner-c", _RESTORE_HOST)
    assert retry is not None and retry["id"] == follow_up["id"]


async def _moved_chat_after_one_failed_restore(pool, store: Storage) -> tuple[dict, dict]:
    """A moved chat whose one real restore failure has waited out its delay."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None
    assert await store.attempt_commands.release_claim(
        job_id=follow_up["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        reason="sandbox_restore_failed",
    )
    await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[0])
    return job, follow_up


async def test_turns_failed_before_their_host_reserved_the_group_spend_no_attempt(
    pool, store: Storage
):
    """A turn the hand-over or its host fails before the reservation read no checkpoint.

    The hand-over fails a claimed turn outright when the owner may no longer
    use Agent, the gateway no longer grants the turn's model, or the
    repository is no longer allowed; a host fails one it cannot even start.
    Each of these counted as a failed restore: it started the next wait, and
    the fourth lost the chat for good though nothing had read its checkpoint.
    """
    job, follow_up = await _moved_chat_after_one_failed_restore(pool, store)
    turn = follow_up
    for _ in range(MAX_RESTORE_ATTEMPTS):
        claim = await _claim(store, "runner-c", _RESTORE_HOST)
        assert claim is not None and claim["id"] == turn["id"]
        assert await store.attempt_commands.transition(
            job_id=turn["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="failed",
            detail="Your Agent access was withdrawn.",
        )
        state = await _state(pool)
        assert (state["state"], state["restore_attempts"], state["restore_attempt_id"]) == (
            "cold",
            1,
            None,
        )
        member = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
        assert member is not None and member["context_status"] != "lost"
        # The owner's next message is claimed at once: no new wait started.
        turn = await store.conversation_commands.create_follow_up(
            parent_job_id=job["id"], user_id="user-1", prompt="try again"
        )
        assert turn is not None

    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == turn["id"]
    reserved = await _reserve(store, turn, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(turn, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    assert (await _state(pool))["state"] == "running"


async def test_a_lease_that_lapses_before_its_host_reserved_the_group_spends_no_attempt(
    pool, store: Storage
):
    """A host that died, or whose turn process never answered, right after its claim."""
    job, follow_up = await _moved_chat_after_one_failed_restore(pool, store)
    for _ in range(MAX_RESTORE_ATTEMPTS):
        claim = await _claim(store, "runner-c", _RESTORE_HOST)
        assert claim is not None and claim["id"] == follow_up["id"]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE id = $1",
                claim["attempt_id"],
            )
        await store.attempt_commands.reap_expired(max_attempts=MAX_RESTORE_ATTEMPTS + 1)

        state = await _state(pool)
        assert (state["state"], state["restore_attempts"], state["restore_attempt_id"]) == (
            "cold",
            1,
            None,
        )
        requeued = await store.conversation_queries.get_job(follow_up["id"])
        assert requeued is not None and requeued["state"] == "queued"
        member = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
        assert member is not None and member["context_status"] != "lost"

    # Nothing started a new wait: the next claim restores at once.
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    assert (await _state(pool))["state"] == "running"


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
    # The group's activity before the restore lies long in the past.
    await _age_restore_failure(pool, 3600)

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
    # The lapsed lease counts as a failed restore: the next try waits from it.
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None
    await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[0])
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

    # Nowhere to move it, so the administrator removes the host anyway.
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"

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


async def test_a_checkpoint_landing_after_the_revocation_still_moves_the_group(
    pool, store: Storage
):
    """A beat authenticated before the revocation may finish writing after it.

    Its checkpoint then covers a stopped group bound to a host that will never
    beat again, and the revocation's hand-over has already looked.
    """
    job, group = await _settled_stopped_group(pool, store)
    # Its checkpoint is not here yet, so the administrator agrees to leave it.
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )
    assert (await _state(pool))["state"] == "stopped"

    assert await store.host_pool.finish_host_removals() == []

    assert (await _state(pool))["state"] == "cold"


async def test_a_workspace_request_hands_on_a_group_its_late_checkpoint_covers(
    pool, store: Storage
):
    """A request before the reaper's next pass moves the group instead of starting it.

    Started on the removed machine, which never reports its stop, the group
    would lose the checkpoint's cover for good.
    """
    job, group = await _settled_stopped_group(pool, store)
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )

    assert await store.sandbox.fence_workspace_start(thread_id=job["thread_id"]) == "cold"

    state = await _state(pool)
    assert (state["state"], state["restore_checkpoint_id"]) == ("cold", "c" * 32)


async def test_a_workspace_request_to_a_removed_hosts_group_starts_nothing(pool, store: Storage):
    """Refused without a fence, so a checkpoint still in flight moves the group."""
    job, group = await _settled_stopped_group(pool, store)
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"

    assert await store.sandbox.fence_workspace_start(thread_id=job["thread_id"]) == "host_revoked"

    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )
    assert await store.host_pool.finish_host_removals() == []
    assert (await _state(pool))["state"] == "cold"


async def test_a_removed_hosts_group_is_not_captured_for_publication(pool, store: Storage):
    """A capture starts the container too, so it waits for the group to move."""
    job, group = await _settled_stopped_group(pool, store)
    requested = await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )
    assert requested.get("requested") is True, requested
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"

    assert await store.snapshot.claim_due_sandbox_snapshot() is None

    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )
    assert await store.host_pool.finish_host_removals() == []
    assert (await _state(pool))["state"] == "cold"


@pytest.mark.parametrize("checkpoint_first", [False, True])
async def test_a_publication_recorded_after_the_removal_starts_nothing(
    pool, store: Storage, checkpoint_first: bool
):
    """A publication out when its host was removed lands after the removal.

    Importing its base would start the container on the removed machine, so
    the import waits for the group's next host, and the late checkpoint still
    moves the group whichever lands first.
    """
    job, _ = await _settled_stopped_group(pool, store)
    requested = await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )
    assert requested.get("requested") is True, requested
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        == "captured"
    )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    # The capture started the container; it stops again, and the host is
    # removed while the publication is out.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', idle_deadline = NULL WHERE id = $1",
            "asbx_restore",
        )
    group = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert group is not None
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    if checkpoint_first:
        assert await store.sandbox.record_sandbox_checkpoint(
            host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
        )

    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        pr_url="https://example.test/pull/1",
        commit_sha="b" * 40,
    )
    assert await store.snapshot.pending_snapshot_base_import() is None

    if not checkpoint_first:
        assert await store.sandbox.record_sandbox_checkpoint(
            host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
        )
    assert await store.host_pool.finish_host_removals() == []
    assert (await _state(pool))["state"] == "cold"


async def test_a_fork_that_waited_on_the_revocation_starts_nothing(pool, store: Storage):
    """A Fork that read its host before the revocation committed checks it again.

    It waits on the group the revocation holds, and finds the host gone once
    it has the group.
    """
    job, group = await _settled_stopped_group(pool, store)
    async with pool.acquire() as revoking, revoking.transaction():
        await revoking.execute(
            "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE", group["sandbox_id"]
        )
        fork = asyncio.create_task(
            store.session_fork.prepare_session_fork(
                source_job_id=job["id"],
                user_id=job["user_id"],
                idempotency_key="fork-across-removal",
            )
        )
        await _waiting_on(pool, await revoking.fetchval("SELECT pg_backend_pid()"))
        await revoking.execute(
            "UPDATE agent_runner_hosts SET status = 'disabled', credential_hash = NULL "
            "WHERE host = 'runner-a'"
        )

    assert await asyncio.wait_for(fork, 10) == {"error": "host_removed"}
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )
    assert await store.host_pool.finish_host_removals() == []
    assert (await _state(pool))["state"] == "cold"


async def test_forgetting_a_removed_host_leaves_its_cold_groups_restorable(pool, store: Storage):
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"

    # The group no longer needs this machine: any restore-capable host takes it.
    assert await store.host_pool.forget_runner_host(host="runner-a") == "deleted"

    assert await store.host_pool.get_host(host="runner-a") is None
    # It keeps naming the host it last ran on until a claim moves it.
    forgotten = await _state(pool)
    assert forgotten["state"] == "cold"
    assert forgotten["host"] == "runner-a"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert reserved["restore"]["checkpoint_id"] == "c" * 32
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    restored = await _state(pool)
    assert (restored["state"], restored["host"], restored["generation"]) == (
        "running",
        "runner-c",
        2,
    )


async def test_a_removed_hosts_uncovered_group_still_keeps_it_from_being_forgotten(
    pool, store: Storage
):
    """A stopped group with no covering checkpoint continues only on that host."""
    await _settled_stopped_group(pool, store)
    # It cannot move, so the administrator is asked before it is left there.
    assert await store.host_pool.remove_host(host="runner-a") == "cannot_move"
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    assert (await _state(pool))["state"] == "stopped"

    assert await store.host_pool.forget_runner_host(host="runner-a") == "retained_groups_present"
    assert await store.host_pool.get_host(host="runner-a") is not None
