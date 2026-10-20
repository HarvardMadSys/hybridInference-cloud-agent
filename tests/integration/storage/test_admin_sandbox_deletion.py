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
from tests.integration.storage.test_sandbox_restore_store import _RESTORE_HOST, _host
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to
from tests.unit.test_ledger_snapshots import START

from cloud_agent.storage.repositories.sandbox import _DELETION_RETRY_EXHAUSTION_BATCH
from cloud_agent.workers import sandbox_deletion

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


async def test_discard_deletes_without_an_archive_receipt(db, store):
    origin = await seed(db, store, "asbx_discard", state="stopped")
    await member(db, "asbx_discard", origin, 0)
    assert await request_delete(store, "asbx_discard", discard=True) == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None and claim["deletion_discard"] is True
    assert claim["deletion_state"] == "pending"
    assert not await store.sandbox.complete_admin_sandbox_deletion(**worker_fence(claim))
    assert await store.sandbox.complete_admin_sandbox_discard(**worker_fence(claim))
    assert await store.sandbox.complete_admin_sandbox_discard(**worker_fence(claim))
    detail = await store.admin_sandboxes.detail("asbx_discard")
    assert detail is not None
    assert detail.sandbox.deletion_state == "deleted"
    assert detail.sandbox.deletion_discard is True
    assert detail.sandbox.state == "lost"
    assert detail.sandbox.deletion_archive_key is None
    assert detail.sandbox.deleted_at is not None
    assert detail.sandbox.archived_at is None
    native = await store.sandbox.thread_sandbox(thread_id=origin["thread_id"])
    assert native is not None and native["native_state"] == "lost"
    async with db.acquire() as conn:
        actions = await conn.fetch("SELECT action FROM agent_admin_audit_log ORDER BY id")
    assert [row["action"] for row in actions] == [
        "agent.sandbox.delete.requested",
        "agent.sandbox.delete.discarded",
    ]


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


async def test_sandbox_manager_confirmed_force_delete_atomically_repairs_stale_running_state(
    db, store
):
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

    assert (
        await request_delete(
            store,
            "asbx_force",
            sandbox_manager_confirmed_stopped=True,
            confirmed_docker_status="stopped",
        )
        == "accepted"
    )
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
            "details->>'sandbox_manager_confirmed_stopped' AS sandbox_manager_confirmed_stopped, "
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
        "sandbox_manager_confirmed_stopped": "true",
        "docker_status": "stopped",
    }
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None and claim["id"] == "asbx_force"


@pytest.mark.parametrize("docker_status", ["dead", "paused", None])
@pytest.mark.parametrize("discard", [False, True])
async def test_force_delete_repair_accepts_only_stopped_or_dead_host_status(
    db, store, docker_status, discard
):
    sandbox_id = f"asbx_force_{docker_status or 'missing'}"
    await seed(db, store, sandbox_id, state="running")
    result = await request_delete(
        store,
        sandbox_id,
        sandbox_manager_confirmed_stopped=True,
        confirmed_docker_status=docker_status,
        discard=discard,
    )
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None
    if docker_status == "dead":
        assert result == "accepted"
        assert detail.sandbox.state == "stopped" and detail.sandbox.deletion_state == "pending"
        assert detail.sandbox.deletion_discard is discard
        async with db.acquire() as conn:
            recorded = await conn.fetchval(
                "SELECT details->>'docker_status' FROM agent_admin_audit_log "
                "WHERE action='agent.sandbox.delete.requested'"
            )
        assert recorded == "dead"
    else:
        assert result == "not_stopped"
        assert detail.sandbox.state == "running" and detail.sandbox.deletion_state == "none"


async def test_broker_confirmed_force_delete_keeps_all_busy_fences(db, store):
    await seed(db, store, "asbx_force_busy", state="running")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET active_operation_count=1 WHERE id='asbx_force_busy'"
        )
    assert (
        await request_delete(
            store,
            "asbx_force_busy",
            sandbox_manager_confirmed_stopped=True,
            confirmed_docker_status="dead",
        )
        == "busy"
    )
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
            await request_delete(
                store,
                "asbx_force_audit",
                sandbox_manager_confirmed_stopped=True,
                confirmed_docker_status="stopped",
            )
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


async def test_claim_serves_a_newer_request_before_older_ones_due_again(db, store):
    """Two slow failures taking turns ahead of a newer request must not starve it."""
    for sandbox_id in ("asbx_old_a", "asbx_old_b", "asbx_new"):
        await seed(db, store, sandbox_id, state="stopped")
    assert await request_delete(store, "asbx_old_a", discard=True) == "accepted"
    assert await request_delete(store, "asbx_old_b", discard=True) == "accepted"
    old_a = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert old_a is not None and old_a["id"] == "asbx_old_a"
    assert await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(old_a),
        error="The host timed out.",
        retain_pending=True,
        retry_class="transient",
    )
    old_b = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert old_b is not None and old_b["id"] == "asbx_old_b"
    # The newer request arrives while that attempt is still waiting on its host.
    assert await request_delete(store, "asbx_new", discard=True) == "accepted"
    assert await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(old_b),
        error="The host timed out.",
        retain_pending=True,
        retry_class="transient",
    )
    # The attempt outlasted the retry delay, so the first request is due again.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_retry_at = clock_timestamp() WHERE id = $1",
            "asbx_old_a",
        )
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None and claim["id"] == "asbx_new"
    retry = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert retry is not None and retry["id"] == "asbx_old_a"


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
    decision = await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim),
        error="Archive operation will retry.",
        retain_pending=True,
        retry_class="transient",
    )
    assert decision.recorded is True
    assert decision.retry_scheduled is True
    assert decision.retry_at is not None
    assert decision.terminal_reason is None
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
    decision = await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim), error="Host confirmed archive preparation was aborted."
    )
    assert decision.recorded is True
    assert decision.retry_scheduled is False
    assert decision.retry_at is None
    assert decision.terminal_reason == "operator_action_required"
    detail = await store.admin_sandboxes.detail("asbx_failed")
    assert detail is not None and detail.sandbox.deletion_state == "failed"
    assert detail.sandbox.deletion_retry_exhausted_at is None
    assert await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    assert await request_delete(store, "asbx_failed") == "accepted"
    replacement = await store.admin_sandboxes.detail("asbx_failed")
    assert replacement is not None
    assert replacement.sandbox.deletion_request_id != claim["deletion_request_id"]
    assert replacement.sandbox.deletion_retry_exhausted_at is None


async def test_deletion_attempt_ledger_records_sanitized_failure_details(db, store):
    await seed(db, store, "asbx_attempt_ledger", state="stopped")
    assert await request_delete(store, "asbx_attempt_ledger") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    correlation_id = claim["deletion_attempt_correlation_id"]
    assert claim["deletion_attempt_number"] == 1
    assert await store.sandbox.update_admin_sandbox_deletion_attempt_stage(
        correlation_id=correlation_id,
        stage="archive_upload",
        sandbox_manager_status=503,
        cleanup_outcome="unconfirmed",
    )
    assert await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim),
        error="The container is retained.",
        retain_pending=True,
        retry_class="transient",
    )
    assert await store.sandbox.finish_admin_sandbox_deletion_attempt(
        correlation_id=correlation_id,
        outcome="retrying",
        failure_code="sandbox_manager_unavailable",
        sandbox_manager_status=503,
        retry_class="transient",
        retry_scheduled=True,
        cleanup_outcome="unconfirmed",
    )
    detail = await store.admin_sandboxes.detail("asbx_attempt_ledger")
    assert detail is not None and detail.sandbox.deletion_state == "pending"
    assert len(detail.deletion_attempts) == 1
    attempt = detail.deletion_attempts[0]
    assert attempt.correlation_id == correlation_id
    assert attempt.request_id == claim["deletion_request_id"]
    assert attempt.host_id == claim["host"]
    assert attempt.stage == "archive_upload"
    assert attempt.outcome == "retrying"
    assert attempt.failure_code == "sandbox_manager_unavailable"
    assert attempt.sandbox_manager_status == 503
    assert attempt.retry_class == "transient"
    assert attempt.retry_scheduled is True
    assert attempt.cleanup_outcome == "unconfirmed"


async def test_attempt_ledger_insert_failure_does_not_rollback_deletion_claim(db, store):
    """A telemetry outage cannot undo a properly fenced deletion claim."""
    sandbox_id = "asbx_attempt_telemetry_failure"
    await seed(db, store, sandbox_id, state="stopped")
    assert await request_delete(store, sandbox_id) == "accepted"
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION reject_deletion_attempt_ledger_insert()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.sandbox_id = 'asbx_attempt_telemetry_failure' THEN
                    RAISE EXCEPTION 'injected telemetry write failure';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER reject_deletion_attempt_ledger_insert
            BEFORE INSERT ON agent_sandbox_deletion_attempts
            FOR EACH ROW EXECUTE FUNCTION reject_deletion_attempt_ledger_insert()
            """
        )
    try:
        claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
        assert claim is not None
        assert claim["deletion_attempt_correlation_id"] is None
        assert claim["deletion_attempt_number"] is None
        async with db.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_sandbox_deletion_attempts WHERE sandbox_id=$1",
                    sandbox_id,
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT deletion_claim_token FROM agent_sandboxes WHERE id=$1", sandbox_id
                )
                == claim["deletion_claim_token"]
            )
            assert (
                await conn.fetchval(
                    "SELECT deletion_retry_count FROM agent_sandboxes WHERE id=$1", sandbox_id
                )
                == 1
            )
        assert await checkpoint_archive(store, claim)
        assert await store.sandbox.complete_admin_sandbox_deletion(**worker_fence(claim))
        detail = await store.admin_sandboxes.detail(sandbox_id)
        assert detail is not None and detail.sandbox.deletion_state == "deleted"
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS reject_deletion_attempt_ledger_insert "
                "ON agent_sandbox_deletion_attempts"
            )
            await conn.execute("DROP FUNCTION IF EXISTS reject_deletion_attempt_ledger_insert()")


async def test_attempt_ledger_insert_timeout_does_not_rollback_or_strand_claim(db, store):
    sandbox_id = "asbx_attempt_telemetry_timeout"
    await seed(db, store, sandbox_id, state="stopped")
    assert await request_delete(store, sandbox_id) == "accepted"
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION stall_deletion_attempt_ledger_insert()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.sandbox_id = 'asbx_attempt_telemetry_timeout' THEN
                    PERFORM pg_sleep(10);
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER stall_deletion_attempt_ledger_insert
            BEFORE INSERT ON agent_sandbox_deletion_attempts
            FOR EACH ROW EXECUTE FUNCTION stall_deletion_attempt_ledger_insert()
            """
        )
    try:
        async with asyncio.timeout(2):
            claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
        assert claim is not None
        assert claim["deletion_attempt_correlation_id"] is None
        assert claim["deletion_attempt_number"] is None
        async with db.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT deletion_claim_token FROM agent_sandboxes WHERE id=$1", sandbox_id
                )
                == claim["deletion_claim_token"]
            )
            assert (
                await conn.fetchval(
                    "SELECT deletion_retry_count FROM agent_sandboxes WHERE id=$1", sandbox_id
                )
                == 1
            )
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS stall_deletion_attempt_ledger_insert "
                "ON agent_sandbox_deletion_attempts"
            )
            await conn.execute("DROP FUNCTION IF EXISTS stall_deletion_attempt_ledger_insert()")

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_claim_expires_at=NOW()-INTERVAL '1 second' "
            "WHERE id=$1",
            sandbox_id,
        )
    reclaimed = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert reclaimed is not None
    assert reclaimed["id"] == sandbox_id
    assert reclaimed["deletion_retry_count"] == 2
    assert reclaimed["deletion_attempt_number"] == 1


async def test_attempt_ledger_finish_failure_does_not_rollback_deletion(db, store):
    """A diagnostic write cannot undo an externally confirmed deletion."""
    sandbox_id = "asbx_attempt_finish_telemetry_failure"
    await seed(db, store, sandbox_id, state="stopped")
    assert await request_delete(store, sandbox_id) == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert await checkpoint_archive(store, claim)
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION reject_deletion_attempt_ledger_finish()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.sandbox_id = 'asbx_attempt_finish_telemetry_failure'
                   AND NEW.outcome <> 'running' THEN
                    RAISE EXCEPTION 'injected telemetry finish failure';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER reject_deletion_attempt_ledger_finish
            BEFORE UPDATE ON agent_sandbox_deletion_attempts
            FOR EACH ROW EXECUTE FUNCTION reject_deletion_attempt_ledger_finish()
            """
        )
    try:
        assert await store.sandbox.complete_admin_sandbox_deletion(
            **worker_fence(claim),
            attempt_correlation_id=claim["deletion_attempt_correlation_id"],
        )
        detail = await store.admin_sandboxes.detail(sandbox_id)
        assert detail is not None and detail.sandbox.deletion_state == "deleted"
        assert detail.sandbox.deletion_archive_key == ARCHIVE_URI
        assert detail.sandbox.deletion_sha256 == DIGEST
        assert detail.sandbox.deletion_size_bytes == 4096
        assert detail.deletion_attempts[0].outcome == "running"
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS reject_deletion_attempt_ledger_finish "
                "ON agent_sandbox_deletion_attempts"
            )
            await conn.execute("DROP FUNCTION IF EXISTS reject_deletion_attempt_ledger_finish()")


async def test_attempt_ledger_finish_timeout_does_not_rollback_deletion(db, store):
    sandbox_id = "asbx_attempt_finish_telemetry_timeout"
    await seed(db, store, sandbox_id, state="stopped")
    assert await request_delete(store, sandbox_id) == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert await checkpoint_archive(store, claim)
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION stall_deletion_attempt_ledger_finish()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.sandbox_id = 'asbx_attempt_finish_telemetry_timeout'
                   AND NEW.outcome <> 'running' THEN
                    PERFORM pg_sleep(10);
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER stall_deletion_attempt_ledger_finish
            BEFORE UPDATE ON agent_sandbox_deletion_attempts
            FOR EACH ROW EXECUTE FUNCTION stall_deletion_attempt_ledger_finish()
            """
        )
    try:
        async with asyncio.timeout(2):
            assert await store.sandbox.complete_admin_sandbox_deletion(
                **worker_fence(claim),
                attempt_correlation_id=claim["deletion_attempt_correlation_id"],
            )
        detail = await store.admin_sandboxes.detail(sandbox_id)
        assert detail is not None and detail.sandbox.deletion_state == "deleted"
        assert detail.sandbox.deletion_archive_key == ARCHIVE_URI
        assert detail.sandbox.deletion_sha256 == DIGEST
        assert detail.sandbox.deletion_size_bytes == 4096
        assert detail.deletion_attempts[0].outcome == "running"
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS stall_deletion_attempt_ledger_finish "
                "ON agent_sandbox_deletion_attempts"
            )
            await conn.execute("DROP FUNCTION IF EXISTS stall_deletion_attempt_ledger_finish()")


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
        **worker_fence(claim),
        error="Host archive cleanup will retry.",
        retain_pending=True,
        retry_class="transient",
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
    decision = await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(retry),
        error="Host confirmed the original archive operation was aborted.",
        cleanup_outcome="acknowledged",
    )
    assert decision.recorded is True
    assert decision.retry_scheduled is False
    assert decision.retry_at is None
    assert decision.terminal_reason == "operator_action_required"
    assert await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    assert await request_delete(store, "asbx_cleanup") == "accepted"
    replacement = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert replacement is not None and not replacement["deletion_cleanup_required"]
    assert replacement["deletion_request_id"] != claim["deletion_request_id"]
    assert replacement["deletion_cleanup_archive_key"] is None
    assert await checkpoint_archive(store, replacement)
    assert not await store.sandbox.renew_admin_sandbox_deletion_claim(
        **worker_fence(replacement), for_cleanup=True, cleanup_archive_key="new/request.tar"
    )


@pytest.mark.parametrize(
    ("retry_class", "failure_code"),
    [
        ("permanent", "sandbox_manager_rejected"),
        ("operator_action", "sandbox_manager_precondition"),
    ],
)
async def test_nontransient_failure_stops_retries_and_preserves_archive(
    db, store, retry_class, failure_code
):
    await seed(db, store, "asbx_nontransient", state="stopped")
    assert await request_delete(store, "asbx_nontransient") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert await checkpoint_archive(store, claim)

    decision = await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim),
        error="The host rejected this deletion request.",
        retain_pending=True,
        attempt_correlation_id=claim["deletion_attempt_correlation_id"],
        failure_code=failure_code,
        retry_class=retry_class,
    )
    assert decision.recorded is True
    assert decision.retry_scheduled is False
    assert decision.retry_at is None
    assert decision.terminal_reason == (
        "permanent_failure" if retry_class == "permanent" else "operator_action_required"
    )

    detail = await store.admin_sandboxes.detail("asbx_nontransient")
    assert detail is not None
    assert detail.sandbox.deletion_state == "archived"
    assert detail.sandbox.deletion_retry_exhausted_at is not None
    assert detail.sandbox.deletion_request_id == claim["deletion_request_id"]
    assert detail.sandbox.deletion_archive_key == ARCHIVE_URI
    assert detail.deletion_attempts[0].outcome == "failed"
    assert detail.deletion_attempts[0].failure_code == failure_code
    assert detail.deletion_attempts[0].retry_class == retry_class
    assert detail.deletion_attempts[0].retry_scheduled is False
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT deletion_retry_at FROM agent_sandboxes WHERE id=$1",
                "asbx_nontransient",
            )
            is None
        )
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None

    assert await request_delete(store, "asbx_nontransient") == "accepted"
    resumed = await store.admin_sandboxes.detail("asbx_nontransient")
    assert resumed is not None
    assert resumed.sandbox.deletion_request_id == claim["deletion_request_id"]
    assert resumed.sandbox.deletion_archive_key == ARCHIVE_URI
    assert resumed.sandbox.deletion_retry_exhausted_at is None
    retry = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert retry is not None
    assert retry["deletion_request_id"] == claim["deletion_request_id"]
    assert retry["deletion_archive_key"] == ARCHIVE_URI


async def test_automatic_retry_budget_exhaustion_preserves_receipt_and_resumes_same_request(
    db, store
):
    origin = await seed(db, store, "asbx_retry_budget", state="stopped")
    await member(db, "asbx_retry_budget", origin, 0)
    assert await request_delete(store, "asbx_retry_budget") == "accepted"
    request_id = None
    for attempt_number in range(1, 9):
        if attempt_number > 1:
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_sandboxes SET deletion_retry_at=NOW() WHERE id=$1",
                    "asbx_retry_budget",
                )
        claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
        assert claim is not None
        request_id = request_id or claim["deletion_request_id"]
        assert claim["deletion_request_id"] == request_id
        assert claim["deletion_retry_count"] == attempt_number
        if attempt_number == 1:
            assert await checkpoint_archive(store, claim)
        assert await store.sandbox.fail_admin_sandbox_deletion(
            **worker_fence(claim),
            error="The host timed out.",
            retain_pending=True,
            attempt_correlation_id=claim["deletion_attempt_correlation_id"],
            failure_code="sandbox_manager_unavailable",
            retry_class="transient",
        )

    detail = await store.admin_sandboxes.detail("asbx_retry_budget")
    assert detail is not None
    assert detail.sandbox.deletion_state == "archived"
    assert detail.sandbox.deletion_retry_exhausted_at is not None
    assert detail.sandbox.deletion_request_id == request_id
    assert detail.sandbox.deletion_archive_key == ARCHIVE_URI
    # Detail returns attempts newest first.
    assert detail.deletion_attempts[0].outcome == "failed"
    assert detail.deletion_attempts[0].failure_code == "retry_budget_exhausted"
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None

    assert await request_delete(store, "asbx_retry_budget") == "accepted"
    resumed = await store.admin_sandboxes.detail("asbx_retry_budget")
    assert resumed is not None
    assert resumed.sandbox.deletion_request_id == request_id
    assert resumed.sandbox.deletion_archive_key == ARCHIVE_URI
    assert resumed.sandbox.deletion_retry_exhausted_at is None
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert claim["deletion_request_id"] == request_id
    assert claim["deletion_retry_count"] == 1
    assert claim["deletion_attempt_number"] == 9
    assert claim["deletion_archive_key"] == ARCHIVE_URI


async def test_retry_budget_migrations_upgrade_previous_release_request(db, store):
    """The previous release's pending request survives the merged migration chain."""
    sandbox_id = "asbx_retry_budget_upgrade"
    await seed(db, store, sandbox_id, state="stopped")
    assert await request_delete(store, sandbox_id) == "accepted"
    before = await store.admin_sandboxes.detail(sandbox_id)
    assert before is not None
    request_id = before.sandbox.deletion_request_id
    requested_at = before.sandbox.deletion_requested_at
    assert request_id is not None
    assert requested_at is not None

    config = _db_config()
    try:
        downgrade_to(config, "0085_terminal_resume_index")
        async with db.acquire() as conn:
            versions = {
                row["version_num"]
                for row in await conn.fetch("SELECT version_num FROM alembic_version")
            }
            attempt_table = await conn.fetchval(
                "SELECT to_regclass('agent_sandbox_deletion_attempts')"
            )
            retry_columns = {
                row["column_name"]
                for row in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='agent_sandboxes' AND column_name IN "
                    "('deletion_retry_started_at', 'deletion_retry_count', "
                    "'deletion_retry_exhausted_at')"
                )
            }
            old_request = await conn.fetchrow(
                "SELECT deletion_state, deletion_request_id, deletion_requested_at "
                "FROM agent_sandboxes WHERE id=$1",
                sandbox_id,
            )
        assert versions == {"0085_terminal_resume_index"}
        assert attempt_table is None
        assert retry_columns == set()
        assert old_request["deletion_state"] == "pending"
        assert old_request["deletion_request_id"] == request_id
        assert old_request["deletion_requested_at"] == requested_at

        apply_migrations(config, "0088_sandbox_retry_budget")

        async with db.acquire() as conn:
            versions = {
                row["version_num"]
                for row in await conn.fetch("SELECT version_num FROM alembic_version")
            }
            attempt_table = await conn.fetchval(
                "SELECT to_regclass('agent_sandbox_deletion_attempts')"
            )
            row = await conn.fetchrow(
                "SELECT deletion_state, deletion_request_id, deletion_requested_at, "
                "deletion_retry_started_at, deletion_retry_count, "
                "deletion_retry_exhausted_at FROM agent_sandboxes WHERE id=$1",
                sandbox_id,
            )
            attempt_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_sandbox_deletion_attempts WHERE sandbox_id=$1",
                sandbox_id,
            )
        assert versions == {"0088_sandbox_retry_budget"}
        assert attempt_table == "agent_sandbox_deletion_attempts"
        assert row["deletion_state"] == "pending"
        assert row["deletion_request_id"] == request_id
        assert row["deletion_retry_started_at"] == requested_at
        assert row["deletion_retry_count"] == 0
        assert row["deletion_retry_exhausted_at"] is None
        assert attempt_count == 0
    finally:
        apply_migrations(config)


@pytest.mark.parametrize("retry_stop_mode", ["budget_exhausted", "operator_action"])
async def test_reclaimed_discard_recovers_after_automatic_retries_stop(db, store, retry_stop_mode):
    sandbox_id = f"asbx_reclaimed_after_{retry_stop_mode}"
    origin = await seed(db, store, sandbox_id, state="stopped")
    await member(db, sandbox_id, origin, 0)
    assert await request_delete(store, sandbox_id, discard=True) == "accepted"

    if retry_stop_mode == "budget_exhausted":
        for attempt_number in range(1, 9):
            if attempt_number > 1:
                async with db.acquire() as conn:
                    await conn.execute(
                        "UPDATE agent_sandboxes SET deletion_retry_at=NOW() WHERE id=$1",
                        sandbox_id,
                    )
            claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
            assert claim is not None and claim["deletion_retry_count"] == attempt_number
            decision = await store.sandbox.fail_admin_sandbox_deletion(
                **worker_fence(claim),
                error="The host timed out.",
                retain_pending=True,
                attempt_correlation_id=claim["deletion_attempt_correlation_id"],
                failure_code="sandbox_manager_unavailable",
                retry_class="transient",
            )
            assert decision.recorded is True
            assert decision.retry_scheduled is (attempt_number < 8)
        expected_retry_count = 8
    else:
        claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
        assert claim is not None and claim["deletion_retry_count"] == 1
        decision = await store.sandbox.fail_admin_sandbox_deletion(
            **worker_fence(claim),
            error="The sandbox manager is not configured.",
            retain_pending=True,
            attempt_correlation_id=claim["deletion_attempt_correlation_id"],
            failure_code="sandbox_manager_resolution_rejected",
            retry_class="operator_action",
        )
        assert decision.recorded is True
        assert decision.retry_scheduled is False
        assert decision.terminal_reason == "operator_action_required"
        expected_retry_count = 1

    await _host(store, "runner-one", _RESTORE_HOST)
    allocation_id = f"{sandbox_id}_allocation"
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_compute_allocations "
            "(id,name,provider_id,mode,observed_state) "
            "VALUES ($1,$1,'cloudlab','provision','ready')",
            allocation_id,
        )
        await conn.execute(
            "INSERT INTO agent_compute_nodes "
            "(id,allocation_id,provider_node_id,instance_identity,name) "
            "VALUES ($1 || '-node',$1,'node0',$1 || ':node0',$1)",
            allocation_id,
        )
        await conn.execute(
            "UPDATE agent_runner_hosts SET compute_node_id=$1 || '-node', "
            "last_heartbeat_at=clock_timestamp() WHERE host='runner-one'",
            allocation_id,
        )

    # Stopped retries remain terminal while the provider still says the host is live.
    assert not await store.sandbox.recover_reclaimed_admin_sandbox_discard(
        reclaimed_quiet_seconds=600
    )
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET observed_state='terminated' WHERE id=$1",
            allocation_id,
        )
        await conn.execute(
            "UPDATE agent_runner_hosts SET last_heartbeat_at="
            "clock_timestamp()-INTERVAL '20 minutes' WHERE host='runner-one'"
        )

    # The storage-only recovery completes without another worker lease or host call.
    assert await sandbox_deletion.delete_one_archived_sandbox(store)

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, deletion_state, deletion_retry_count, "
            "deletion_retry_exhausted_at, deletion_error "
            "FROM agent_sandboxes WHERE id=$1",
            sandbox_id,
        )
        attempt_counts = await conn.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE outcome='running') AS running "
            "FROM agent_sandbox_deletion_attempts WHERE sandbox_id=$1",
            sandbox_id,
        )
        event = await conn.fetchrow(
            "SELECT details->>'host_reclaimed' AS host_reclaimed "
            "FROM agent_sandbox_events WHERE sandbox_id=$1 "
            "AND event_type='deletion_completed' ORDER BY id DESC LIMIT 1",
            sandbox_id,
        )
    assert dict(row) == {
        "state": "lost",
        "deletion_state": "deleted",
        "deletion_retry_count": expected_retry_count,
        "deletion_retry_exhausted_at": None,
        "deletion_error": None,
    }
    assert dict(attempt_counts) == {"total": expected_retry_count, "running": 0}
    assert event["host_reclaimed"] == "true"
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None


async def test_concurrent_reclaimed_discard_recovery_finalizes_once(db, store):
    """Competing recovery sweeps cannot double-finalize a reclaimed container."""
    sandbox_id = "asbx_concurrent_reclaimed_recovery"
    origin = await seed(db, store, sandbox_id, state="stopped")
    await member(db, sandbox_id, origin, 0)
    assert await request_delete(store, sandbox_id, discard=True) == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    decision = await store.sandbox.fail_admin_sandbox_deletion(
        **worker_fence(claim),
        error="The sandbox manager needs operator action.",
        retain_pending=True,
        attempt_correlation_id=claim["deletion_attempt_correlation_id"],
        failure_code="sandbox_manager_resolution_rejected",
        retry_class="operator_action",
    )
    assert decision.retry_scheduled is False

    await _host(store, "runner-one", _RESTORE_HOST)
    allocation_id = f"{sandbox_id}_allocation"
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_compute_allocations "
            "(id,name,provider_id,mode,observed_state) "
            "VALUES ($1,$1,'cloudlab','provision','terminated')",
            allocation_id,
        )
        await conn.execute(
            "INSERT INTO agent_compute_nodes "
            "(id,allocation_id,provider_node_id,instance_identity,name) "
            "VALUES ($1 || '-node',$1,'node0',$1 || ':node0',$1)",
            allocation_id,
        )
        await conn.execute(
            "UPDATE agent_runner_hosts SET compute_node_id=$1 || '-node', "
            "last_heartbeat_at=clock_timestamp()-INTERVAL '20 minutes' "
            "WHERE host='runner-one'",
            allocation_id,
        )

    outcomes = await asyncio.gather(
        store.sandbox.recover_reclaimed_admin_sandbox_discard(reclaimed_quiet_seconds=600),
        store.sandbox.recover_reclaimed_admin_sandbox_discard(reclaimed_quiet_seconds=600),
    )
    assert sorted(outcomes) == [False, True]

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, deletion_state FROM agent_sandboxes WHERE id=$1",
            sandbox_id,
        )
        completion_count = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_sandbox_events "
            "WHERE sandbox_id=$1 AND event_type='deletion_completed'",
            sandbox_id,
        )
        attempts = await conn.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE outcome='running') AS running "
            "FROM agent_sandbox_deletion_attempts WHERE sandbox_id=$1",
            sandbox_id,
        )
    assert dict(row) == {"state": "lost", "deletion_state": "deleted"}
    assert completion_count == 1
    assert dict(attempts) == {"total": 1, "running": 0}


async def test_expired_final_attempt_marks_budget_exhausted_without_reclaiming(db, store):
    await seed(db, store, "asbx_crashed_final_attempt", state="stopped")
    assert await request_delete(store, "asbx_crashed_final_attempt") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_retry_count=8, "
            "deletion_claim_expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",
            claim["id"],
        )
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None
    detail = await store.admin_sandboxes.detail(claim["id"])
    assert detail is not None
    assert detail.sandbox.deletion_state == "pending"
    assert detail.sandbox.deletion_retry_exhausted_at is not None
    assert detail.deletion_attempts[0].outcome == "failed"
    assert detail.deletion_attempts[0].failure_code == "retry_budget_exhausted"


async def test_retry_exhaustion_is_batched_and_concurrent_passes_do_not_duplicate_audit(db, store):
    exhausted_ids = [
        f"asbx_retry_sweep_{index:03d}" for index in range(_DELETION_RETRY_EXHAUSTION_BATCH + 3)
    ]
    eligible_id = "asbx_retry_sweep_eligible"
    for sandbox_id in [*exhausted_ids, eligible_id]:
        await seed(db, store, sandbox_id, state="stopped")
        assert await request_delete(store, sandbox_id) == "accepted"
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_sandboxes AS sandbox
            SET deletion_retry_at=NOW(),
                deletion_requested_at=NOW()
                    + ($2 - seeded.ordinal) * INTERVAL '1 second'
            FROM unnest($1::text[]) WITH ORDINALITY AS seeded(id, ordinal)
            WHERE sandbox.id=seeded.id
            """,
            exhausted_ids,
            len(exhausted_ids) - 1,
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_retry_at=NOW(), "
            "deletion_requested_at=NOW()+INTERVAL '1 day' WHERE id=$1",
            eligible_id,
        )
    first_attempt = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert first_attempt is not None and first_attempt["id"] == exhausted_ids[-1]
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_retry_count=8 WHERE id=ANY($1::text[])",
            exhausted_ids,
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_claim_expires_at=NOW()-INTERVAL '1 second' "
            "WHERE id=$1",
            first_attempt["id"],
        )
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION stall_retry_exhaustion_attempt_update()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.sandbox_id LIKE 'asbx_retry_sweep_%'
                   AND NEW.failure_code = 'retry_budget_exhausted' THEN
                    PERFORM pg_sleep(10);
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER stall_retry_exhaustion_attempt_update
            BEFORE UPDATE ON agent_sandbox_deletion_attempts
            FOR EACH ROW EXECUTE FUNCTION stall_retry_exhaustion_attempt_update()
            """
        )

    try:
        async with asyncio.timeout(2):
            claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
        assert claim is not None and claim["id"] == eligible_id
        async with db.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_sandboxes "
                    "WHERE id=ANY($1::text[]) AND deletion_retry_exhausted_at IS NOT NULL",
                    exhausted_ids,
                )
                == _DELETION_RETRY_EXHAUSTION_BATCH
            )
            selected = await conn.fetch(
                "SELECT id FROM agent_sandboxes "
                "WHERE id=ANY($1::text[]) AND deletion_retry_exhausted_at IS NOT NULL",
                exhausted_ids,
            )
            assert {row["id"] for row in selected} == set(
                exhausted_ids[-_DELETION_RETRY_EXHAUSTION_BATCH:]
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_admin_audit_log "
                    "WHERE action='agent.sandbox.delete.retry_exhausted'"
                )
                == _DELETION_RETRY_EXHAUSTION_BATCH
            )
            assert (
                await conn.fetchval(
                    "SELECT outcome FROM agent_sandbox_deletion_attempts WHERE request_id=$1",
                    first_attempt["deletion_request_id"],
                )
                == "running"
            )
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS stall_retry_exhaustion_attempt_update "
                "ON agent_sandbox_deletion_attempts"
            )
            await conn.execute("DROP FUNCTION IF EXISTS stall_retry_exhaustion_attempt_update()")

    remaining_ids = set(exhausted_ids[:-_DELETION_RETRY_EXHAUSTION_BATCH])
    assert len(remaining_ids) == 3
    locked_id = min(remaining_ids)
    async with db.acquire() as lock_conn, lock_conn.transaction():
        assert (
            await lock_conn.fetchval(
                "SELECT id FROM agent_sandboxes WHERE id=$1 FOR UPDATE", locked_id
            )
            == locked_id
        )
        results = await asyncio.gather(
            store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600),
            store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600),
        )
        assert results == [None, None]
        async with db.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_sandboxes "
                    "WHERE id=ANY($1::text[]) AND deletion_retry_exhausted_at IS NOT NULL",
                    exhausted_ids,
                )
                == _DELETION_RETRY_EXHAUSTION_BATCH + 2
            )
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None
    async with db.acquire() as conn:
        events = await conn.fetch(
            """
            SELECT details->>'sandbox_id' AS sandbox_id, COUNT(*) AS count
            FROM agent_admin_audit_log
            WHERE action='agent.sandbox.delete.retry_exhausted'
            GROUP BY details->>'sandbox_id'
            """
        )
    assert {row["sandbox_id"]: int(row["count"]) for row in events} == dict.fromkeys(
        exhausted_ids, 1
    )


async def test_retry_window_expiry_is_durable_and_keeps_archive_receipt(db, store):
    await seed(db, store, "asbx_retry_window", state="stopped")
    assert await request_delete(store, "asbx_retry_window") == "accepted"
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None
    assert await checkpoint_archive(store, claim)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_claim_expires_at=NULL, "
            "deletion_retry_started_at=NOW()-INTERVAL '25 hours' WHERE id=$1",
            claim["id"],
        )
    assert await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600) is None
    detail = await store.admin_sandboxes.detail(claim["id"])
    assert detail is not None
    assert detail.sandbox.deletion_state == "archived"
    assert detail.sandbox.deletion_retry_exhausted_at is not None
    assert detail.sandbox.deletion_request_id == claim["deletion_request_id"]
    assert detail.sandbox.deletion_archive_key == ARCHIVE_URI


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
