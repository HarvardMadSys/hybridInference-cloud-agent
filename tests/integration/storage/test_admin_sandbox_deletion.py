"""Real Postgres safety fences and ledger retention for archive-before-delete."""

import asyncio

import asyncpg
import pytest
from tests.integration.storage.test_admin_sandbox_queries import archive_request, member, seed
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _reserve_shared_chat,
    _shared_claim_attestation,
)
from tests.integration.storage.test_attempt_ledger import db as db, store as store
from tests.unit.test_ledger_snapshots import START

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

ARCHIVE_URI = "s3://sandbox-archive-bucket/archives/exact-request.tar"
DIGEST = "a" * 64


async def request_delete(store, sandbox_id, **overrides):
    """Queue the exact stopped sandbox reviewed by an administrator."""
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None
    return await store.sandbox.request_admin_delete(
        **(
            {
                "sandbox_id": sandbox_id,
                "generation": detail.sandbox.generation,
                "container_id": detail.sandbox.container_id,
                "last_activity_at": detail.sandbox.last_activity_at,
                "actor_id": "admin:operator",
                "admin_ip": "127.0.0.1",
            }
            | overrides
        )
    )


def worker_fence(claim):
    """Bind every checkpoint to the immutable request and its current claim."""
    return {
        "sandbox_id": claim["id"],
        "request_id": claim["deletion_request_id"],
        "claim_token": claim["deletion_claim_token"],
        "generation": claim["generation"],
        "container_id": claim["container_id"],
    }


async def checkpoint_archive(store, claim, **overrides):
    """Persist a receipt only after the worker independently verifies S3."""
    return await store.sandbox.record_admin_sandbox_archive(
        **(
            worker_fence(claim)
            | {"archive_key": ARCHIVE_URI, "sha256": DIGEST, "size_bytes": 4096}
            | overrides
        )
    )


async def test_deletion_requires_verified_archive_and_retains_native_and_job_ledger(db, store):
    origin = await seed(db, store, "asbx_delete", state="stopped")
    await member(db, "asbx_delete", origin, 0)
    assert await request_delete(store, "asbx_delete") == "accepted"
    assert await request_delete(store, "asbx_delete") == "accepted"
    assert not await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None and claim["deletion_state"] == "pending"
    assert not await store.sandbox.complete_admin_sandbox_deletion(**worker_fence(claim))
    assert await checkpoint_archive(store, claim, archive_version_id="immutable-version")
    assert await checkpoint_archive(store, claim, archive_version_id="immutable-version")
    assert not await checkpoint_archive(store, claim, archive_key="s3://other-bucket/other.tar")
    assert await store.sandbox.complete_admin_sandbox_deletion(**worker_fence(claim))
    assert await store.sandbox.complete_admin_sandbox_deletion(**worker_fence(claim))
    detail = await store.admin_sandboxes.detail("asbx_delete")
    assert detail is not None
    assert detail.sandbox.deletion_state == "deleted" and detail.sandbox.state == "lost"
    assert detail.sandbox.deletion_archive_key == ARCHIVE_URI
    assert detail.sandbox.deletion_archive_version_id == "immutable-version"
    assert detail.sandbox.deletion_sha256 == DIGEST
    assert detail.sandbox.deletion_size_bytes == 4096
    assert detail.sandbox.deleted_at is not None and detail.sandbox.archived_at is not None
    native = await store.sandbox.thread_sandbox(thread_id=origin["thread_id"])
    assert native is not None and native["native_state"] == native["context_status"] == "lost"
    assert native["native_session_id"] == f"PRIVATE-session-{origin['thread_id']}"
    assert not await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    async with db.acquire() as conn:
        assert (
            await conn.fetchval("SELECT state FROM agent_jobs WHERE id=$1", origin["id"])
            == "failed"
        )
        logs = await conn.fetch("SELECT action, actor_id FROM agent_admin_audit_log ORDER BY id")
    assert [row["action"] for row in logs] == [
        "agent.sandbox.delete.requested",
        "agent.sandbox.delete.archived",
        "agent.sandbox.delete.completed",
    ]
    assert logs[0]["actor_id"] == "admin:operator"
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None
    assert await archive_request(store, "asbx_delete", archived=False) == "accepted"
    assert await archive_request(store, "asbx_delete", archived=True) == "accepted"
    assert "PRIVATE" not in detail.model_dump_json()


@pytest.mark.parametrize(
    ("change", "result"),
    [
        ("state='running'", "not_stopped"),
        ("state='lost'", "not_stopped"),
        ("active_operation_count=1", "busy"),
        ("snapshot_state='pending'", "busy"),
        ("snapshot_state='importing'", "busy"),
    ],
)
async def test_delete_refuses_live_or_unsettled_group(db, store, change, result):
    await seed(db, store, "asbx_busy", state="stopped")
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE agent_sandboxes SET {change} WHERE id='asbx_busy'")
    assert await request_delete(store, "asbx_busy") == result
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_admin_audit_log") == 0


@pytest.mark.parametrize(
    "changed", [{"generation": 2}, {"container_id": "other"}, {"last_activity_at": START}]
)
async def test_delete_rejects_changed_review(db, store, changed):
    await seed(db, store, "asbx_stale", state="stopped")
    assert await request_delete(store, "asbx_stale", **changed) == "stale"


async def test_broker_confirmed_force_delete_atomically_repairs_stale_running_state(db, store):
    origin = await seed(db, store, "asbx_force", state="running")
    await member(db, "asbx_force", origin, 0)
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET idle_deadline=NULL, terminal_count=0, terminal_count_observed_at=NOW(),
                resource_snapshot='{}'::jsonb, resource_sampled_at=NOW(),
                resource_received_at=NOW()
            WHERE id='asbx_force'
            """
        )

    assert await request_delete(store, "asbx_force", broker_confirmed_stopped=True) == "accepted"
    async with db.acquire() as conn:
        group = await conn.fetchrow(
            """
            SELECT state, deletion_state, idle_deadline, stop_requested_at,
                   resource_snapshot, resource_sampled_at, resource_received_at,
                   terminal_count, terminal_count_observed_at
            FROM agent_sandboxes WHERE id='asbx_force'
            """
        )
        resident_state = await conn.fetchval(
            "SELECT resident_state FROM agent_thread_sandbox_memberships "
            "WHERE sandbox_id='asbx_force'"
        )
        audit_proof = await conn.fetchrow(
            "SELECT details->>'force' AS force, "
            "details->>'broker_confirmed_stopped' AS broker_confirmed_stopped, "
            "details->>'docker_status' AS docker_status FROM agent_admin_audit_log "
            "WHERE action='agent.sandbox.delete.requested'"
        )
    assert group["state"] == "stopped" and group["deletion_state"] == "pending"
    for field in (
        "idle_deadline",
        "stop_requested_at",
        "resource_snapshot",
        "resource_sampled_at",
        "resource_received_at",
        "terminal_count",
        "terminal_count_observed_at",
    ):
        assert group[field] is None
    assert resident_state == "stopped"
    assert dict(audit_proof) == {
        "force": "true",
        "broker_confirmed_stopped": "true",
        "docker_status": "stopped",
    }
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None and claim["id"] == "asbx_force"


async def test_broker_confirmed_force_delete_keeps_all_busy_fences(db, store):
    await seed(db, store, "asbx_force_busy", state="running")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET active_operation_count=1 WHERE id='asbx_force_busy'"
        )
    assert await request_delete(store, "asbx_force_busy", broker_confirmed_stopped=True) == "busy"
    detail = await store.admin_sandboxes.detail("asbx_force_busy")
    assert detail is not None
    assert detail.sandbox.state == "running" and detail.sandbox.deletion_state == "none"


async def test_force_delete_audit_failure_rolls_back_repair_and_deletion_fence(db, store):
    origin = await seed(db, store, "asbx_force_audit", state="running")
    await member(db, "asbx_force_audit", origin, 0)
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE agent_admin_audit_log ADD CONSTRAINT reject_force_delete_audit "
            "CHECK (action <> 'agent.sandbox.delete.requested')"
        )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await request_delete(store, "asbx_force_audit", broker_confirmed_stopped=True)
        detail = await store.admin_sandboxes.detail("asbx_force_audit")
        assert detail is not None
        assert detail.sandbox.state == "running" and detail.sandbox.deletion_state == "none"
        async with db.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT resident_state FROM agent_thread_sandbox_memberships "
                    "WHERE sandbox_id='asbx_force_audit'"
                )
                == "starting"
            )
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "ALTER TABLE agent_admin_audit_log DROP CONSTRAINT reject_force_delete_audit"
            )


async def test_delete_claim_excludes_other_workers_and_expired_worker_receipts(db, store):
    await seed(db, store, "asbx_lease", state="stopped")
    assert await request_delete(store, "asbx_lease") == "accepted"
    claims = await asyncio.gather(
        store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600),
        store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600),
    )
    first = next(claim for claim in claims if claim is not None)
    assert sum(claim is not None for claim in claims) == 1
    assert await store.sandbox.renew_admin_sandbox_deletion_claim(
        **worker_fence(first), ttl_seconds=300
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_claim_expires_at=NOW()-INTERVAL '1 second'"
        )
    assert not await checkpoint_archive(store, first)
    assert not await store.sandbox.renew_admin_sandbox_deletion_claim(**worker_fence(first))
    replacement = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert replacement is not None
    assert replacement["deletion_request_id"] == first["deletion_request_id"]
    assert replacement["deletion_claim_token"] != first["deletion_claim_token"]
    assert not await store.sandbox.fail_admin_sandbox_deletion(**worker_fence(first), error="stale")
    assert await checkpoint_archive(store, replacement)


async def test_deletion_receipt_rechecks_claim_expiry_after_a_plain_row_lock(db, store):
    await seed(db, store, "asbx_blocked_lease", state="stopped")
    assert await request_delete(store, "asbx_blocked_lease") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=0.4)
    assert claim is not None
    archiving = None
    async with db.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
            claim["id"],
        )
        archiving = asyncio.create_task(checkpoint_archive(store, claim))
        waiting = False
        for _ in range(100):
            waiting = await blocker.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                blocker_pid,
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting, "archive receipt did not wait on the sandbox row lock"
        await asyncio.sleep(0.55)

    assert archiving is not None
    assert not await asyncio.wait_for(archiving, timeout=2)
    detail = await store.admin_sandboxes.detail(claim["id"])
    assert detail is not None
    assert detail.sandbox.deletion_state == "pending"
    assert detail.sandbox.deletion_archive_key is None


@pytest.mark.parametrize(
    "changed",
    [
        {"request_id": "other-request"},
        {"claim_token": "other-worker"},
        {"generation": 2},
        {"container_id": "other-container"},
    ],
)
async def test_archive_and_completion_reject_changed_worker_fence(db, store, changed):
    await seed(db, store, "asbx_fence", state="stopped")
    assert await request_delete(store, "asbx_fence") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert not await checkpoint_archive(store, claim, **changed)
    assert await checkpoint_archive(store, claim)
    assert not await store.sandbox.complete_admin_sandbox_deletion(
        **(worker_fence(claim) | changed)
    )


@pytest.mark.parametrize("checkpoint", ["pending", "archived"])
async def test_failure_keeps_verified_or_uncertain_archives_fenced(db, store, checkpoint):
    origin = await seed(db, store, "asbx_retry", state="stopped")
    await member(db, "asbx_retry", origin, 0)
    assert await request_delete(store, "asbx_retry") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    if checkpoint == "archived":
        assert await checkpoint_archive(store, claim)
    assert await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim), error="Archive operation will retry.", retain_pending=True
    )
    detail = await store.admin_sandboxes.detail("asbx_retry")
    assert detail is not None and detail.sandbox.deletion_state == checkpoint
    assert not await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET deletion_retry_at=NOW()")
    retry = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert retry is not None and retry["deletion_request_id"] == claim["deletion_request_id"]
    assert retry["deletion_archive_key"] == (ARCHIVE_URI if checkpoint == "archived" else None)


async def test_safe_prearchive_failure_preserves_native_resume(db, store):
    origin = await seed(db, store, "asbx_failed", state="stopped")
    await member(db, "asbx_failed", origin, 0)
    assert await request_delete(store, "asbx_failed") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim), error="Host confirmed archive preparation was aborted."
    )
    detail = await store.admin_sandboxes.detail("asbx_failed")
    assert detail is not None and detail.sandbox.deletion_state == "failed"
    assert await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    assert await request_delete(store, "asbx_failed") == "accepted"
    replacement = await store.admin_sandboxes.detail("asbx_failed")
    assert replacement is not None
    assert replacement.sandbox.deletion_request_id != claim["deletion_request_id"]


async def test_uncertain_host_abort_retries_cleanup_only_and_keeps_original_key(db, store):
    origin = await seed(db, store, "asbx_cleanup", state="stopped")
    await member(db, "asbx_cleanup", origin, 0)
    assert await request_delete(store, "asbx_cleanup") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert await store.sandbox.renew_admin_sandbox_deletion_claim(
        **worker_fence(claim), for_cleanup=True, cleanup_archive_key="original-prefix/request.tar"
    )
    assert await store.sandbox.renew_admin_sandbox_deletion_claim(**worker_fence(claim))
    assert not await checkpoint_archive(store, claim)
    assert await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim), error="Host archive cleanup will retry.", retain_pending=True
    )
    assert not await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET deletion_retry_at=NOW()")
    retry = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert retry is not None and retry["deletion_cleanup_required"]
    assert retry["deletion_cleanup_archive_key"] == "original-prefix/request.tar"
    assert not await store.sandbox.renew_admin_sandbox_deletion_claim(
        **worker_fence(retry), for_cleanup=True, cleanup_archive_key="changed-prefix/request.tar"
    )
    assert await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(retry), error="Host confirmed the original archive operation was aborted."
    )
    assert await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    assert await request_delete(store, "asbx_cleanup") == "accepted"
    replacement = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert replacement is not None and not replacement["deletion_cleanup_required"]
    assert replacement["deletion_cleanup_archive_key"] is None
    assert await checkpoint_archive(store, replacement)
    assert not await store.sandbox.renew_admin_sandbox_deletion_claim(
        **worker_fence(replacement), for_cleanup=True, cleanup_archive_key="new/request.tar"
    )


@pytest.mark.parametrize("transition", ["requested", "archived", "completed"])
async def test_deletion_audit_failure_rolls_back_each_durable_transition(db, store, transition):
    await seed(db, store, "asbx_audit", state="stopped")
    claim = None
    if transition != "requested":
        assert await request_delete(store, "asbx_audit") == "accepted"
        claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
        assert claim is not None
    if transition == "completed":
        assert await checkpoint_archive(store, claim)
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE agent_admin_audit_log ADD CONSTRAINT reject_deletion_audit "
            f"CHECK (action <> 'agent.sandbox.delete.{transition}')"
        )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            if transition == "requested":
                await request_delete(store, "asbx_audit")
            elif transition == "archived":
                await checkpoint_archive(store, claim)
            else:
                await store.sandbox.complete_admin_sandbox_deletion(**worker_fence(claim))
        detail = await store.admin_sandboxes.detail("asbx_audit")
        assert detail is not None and detail.sandbox.state == "stopped"
        assert (
            detail.sandbox.deletion_state
            == {"requested": "none", "archived": "pending", "completed": "archived"}[transition]
        )
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "ALTER TABLE agent_admin_audit_log DROP CONSTRAINT reject_deletion_audit"
            )


async def settled_group(db, store):
    """Settle native context and publication so a stopped group can be deleted."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-deletion", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-survives",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_snapshots SET state='failed'")
        await conn.execute("UPDATE agent_sandboxes SET state='stopped', snapshot_state='quiescent'")
    return job, sandbox


async def test_pending_deletion_fences_claim_fork_reserve_and_stale_running_heartbeat(db, store):
    job, sandbox = await settled_group(db, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="Wait during deletion"
    )
    assert follow_up is not None
    assert await request_delete(store, sandbox["sandbox_id"]) == "accepted"
    assert (
        await _claim(
            store,
            worker_id="delete-fence",
            lease_ttl_seconds=60,
            host="shared-runner",
            host_credential_hash="test-credential-hash",
            claim_attestation=_shared_claim_attestation(),
        )
        is None
    )
    assert await store.session_fork.prepare_session_fork(
        source_job_id=job["id"], user_id=job["user_id"], idempotency_key="delete-pending-fork"
    ) == {"error": "sandbox_not_ready"}
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            thread_id=job["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-deletion",
            sandbox_generation=sandbox["generation"],
            running=True,
        )
        is None
    )
    detail = await store.admin_sandboxes.detail(sandbox["sandbox_id"])
    assert detail is not None and detail.sandbox.deletion_state == "pending"
    assert detail.sandbox.state == "stopped"
    deletion = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert deletion is not None
    assert await checkpoint_archive(store, deletion)
    assert await store.sandbox.complete_admin_sandbox_deletion(**worker_fence(deletion))
    async with db.acquire() as conn:
        assert (
            await conn.fetchval("SELECT state FROM agent_jobs WHERE id=$1", follow_up["id"])
            == "failed"
        )
        assert (
            await conn.fetchval("SELECT COUNT(*) FROM agent_attempts WHERE job_id=$1", job["id"])
            == 1
        )


async def test_fork_preparation_makes_removed_stopped_group_visible_immediately(db, store):
    job, sandbox = await settled_group(db, store)
    assert await archive_request(store, sandbox["sandbox_id"]) == "accepted"
    operation = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"], user_id=job["user_id"], idempotency_key="restore-on-fork"
    )
    assert operation is not None and operation.get("error") is None
    detail = await store.admin_sandboxes.detail(sandbox["sandbox_id"])
    assert detail is not None and detail.sandbox.archived_at is None
