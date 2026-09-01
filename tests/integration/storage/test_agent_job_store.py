"""Integration tests for AgentJobStore (Postgres): fencing, reaper, publish.

These pin the concurrency-ownership contract from issue #1041: attempts +
lease_generation fencing (zombie workers can never write after a reap),
append-only events with a global cursor, one-shot publish, and reaper
close-and-requeue semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from cloud_agent.storage.store import AgentJobStore
from tests.support.migrations import apply_migrations, run_baseline_statements

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_TABLES_IN_FK_ORDER = (
    "agent_native_session_edges",
    "agent_thread_sandbox_memberships",
    "agent_sandbox_snapshots",
    "agent_session_forks",
    "agent_sandboxes",
    "agent_thread_residencies",
    "agent_thread_messages",
    "agent_job_artifacts",
    "agent_job_events",
    "agent_thread_sandboxes",
    "agent_attempts",
    "agent_jobs",
    "agent_threads",
    # No FK, but every claim writes here now: a host left active by one test
    # would gate the claims of the next one.
    "agent_runner_hosts",
)


@pytest_asyncio.fixture
async def store():
    """Provide a clean AgentJobStore over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    job_store = AgentJobStore(pool)
    # Schema and backfills come from the baseline migration now, not from
    # the store. This is E1's acceptance: `alembic upgrade head` on a fresh
    # database has to leave every one of these tests passing.
    apply_migrations(db_config)

    async with pool.acquire() as conn:
        for table in _TABLES_IN_FK_ORDER:
            await conn.execute(f"DELETE FROM {table}")
        # Reset rather than delete: the policy is a singleton row, and a test
        # that left a host pinned would otherwise gate every later claim.
        await conn.execute(
            "UPDATE agent_runner_policy SET active_host = NULL, runtime_versions = NULL"
        )

    try:
        yield job_store
    finally:
        async with pool.acquire() as conn:
            for table in _TABLES_IN_FK_ORDER:
                await conn.execute(f"DELETE FROM {table}")
            await conn.execute(
                "UPDATE agent_runner_policy SET active_host = NULL, runtime_versions = NULL"
            )
        await pool.close()


async def _create_job(store: AgentJobStore, **overrides) -> dict:
    """Create a job with sensible defaults."""
    params = {
        "user_id": "user-1",
        "repo": "example-org/example-repo",
        "task_prompt": "fix the flaky test",
        "runtime": "claude-code",
        "model": "glm-5.1",
        "base_sha": "deadbeef",
    }
    params.update(overrides)
    return await store.create_job(**params)


async def _expire_attempt(store: AgentJobStore, attempt_id: int) -> None:
    """Force an attempt's lease into the past."""
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            attempt_id,
        )


def _shared_claim_attestation() -> dict[str, object]:
    """Return the exact process-local fence required for shared work."""
    return {
        "sandbox_image_digest": "sha256:shared-image",
        "shared_sandbox_protocol": "v1",
        "runner_capabilities": ["shared-sandbox-v1"],
        "runtime_versions": {
            "claude-code": "2.1.220",
            "codex": "0.145.0",
            "pi": "0.73.1",
            "opencode": "1.18.9",
            "kilo": "7.4.17",
        },
        "helper_versions": {"claude-agent-sdk": "0.2.148"},
    }


async def _reserve_retained_chat(
    store: AgentJobStore,
) -> tuple[dict, dict, dict]:
    """Create, claim, and reserve one retained chat under a real DB fence."""
    job = await _create_job(store)
    claim = await store.claim_job(
        worker_id="w1",
        lease_ttl_seconds=60,
        host="runner-a",
    )
    assert claim is not None
    sandbox = await store.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        sandbox_id="asbx_test",
        container_name="freeinference-chat-test",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
    )
    assert sandbox is not None
    return job, claim, sandbox


async def _reserve_shared_chat(
    store: AgentJobStore,
) -> tuple[dict, dict, dict]:
    """Create, claim, and reserve one capability-gated shared-live chat."""
    await store.register_host(
        host="shared-runner",
        credential_hash="test-credential-hash",
        capabilities=["shared-sandbox-v1"],
        slots_total=4,
    )
    online = await store.record_host_heartbeat(
        host="shared-runner",
        capabilities=["shared-sandbox-v1"],
    )
    assert online is not None and online["status"] == "online"
    job = await _create_job(
        store,
        base_sha="a" * 40,
        metadata={"requires": ["shared-sandbox-v1"]},
    )
    claim = await store.claim_job(
        worker_id="shared-worker",
        lease_ttl_seconds=60,
        host="shared-runner",
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None and claim["id"] == job["id"]
    sandbox = await store.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        sandbox_id="asbx_shared_test",
        container_name="freeinference-shared-test",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
        shared_live=True,
    )
    assert sandbox is not None and sandbox["shared_live"] is True
    return job, claim, sandbox


async def test_shared_workspace_snapshot_publishes_without_rewriting_turn_state(
    store: AgentJobStore,
):
    """A shared turn, snapshot, publication, and base import form one lineage."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    running = await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    assert running is not None and running["shared_live"] is True
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    settled = await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-one",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert settled is not None
    assert settled["workspace_publication_state"] == "pending"
    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    capture = await store.claim_due_sandbox_snapshot()
    assert capture is not None
    assert capture["sandbox_id"] == sandbox["sandbox_id"]
    assert capture["anchor_job_id"] == job["id"]
    assert capture["base_sha"] == job["base_sha"]
    patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    digest = hashlib.sha256(patch.encode()).hexdigest()
    assert (
        await store.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=digest,
        )
        == "captured"
    )

    publication = await store.claim_for_publish()
    assert publication is not None
    assert publication["publication_kind"] == "shared_snapshot"
    assert publication["snapshot_id"] == capture["snapshot_id"]
    assert publication["included_job_ids"] == [job["id"]]
    assert publication["branch_id"] == job["thread_id"]
    assert publication["patch"] == patch
    # Publication is group bookkeeping, not another state of the model turn.
    assert (await store.get_job(job["id"]))["state"] == "succeeded"

    commit_sha = "b" * 40
    assert await store.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        pr_url="https://example.test/pull/1",
        commit_sha=commit_sha,
    )
    published_job = await store.get_job(job["id"])
    assert published_job is not None
    assert published_job["state"] == "succeeded"
    assert published_job["published_commit_sha"] == commit_sha

    pending_import = await store.pending_snapshot_base_import()
    assert pending_import is not None
    assert pending_import["snapshot_id"] == capture["snapshot_id"]
    assert pending_import["published_commit_sha"] == commit_sha
    assert await store.complete_snapshot_base_import(
        snapshot_id=capture["snapshot_id"],
        commit_sha=commit_sha,
    )
    final_sandbox = await store.thread_sandbox(thread_id=job["thread_id"])
    assert final_sandbox is not None
    assert final_sandbox["workspace_publication_state"] == "published"
    async with store._pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT published_base_sha, snapshot_state FROM agent_sandboxes WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert group is not None
    assert group["published_base_sha"] == commit_sha
    assert group["snapshot_state"] == "quiescent"
    assert await store.pending_snapshot_base_import() is None


async def test_idle_stopped_shared_workspace_snapshot_remains_claimable(
    store: AgentJobStore,
):
    """An idle Docker stop must not strand the final publication snapshot."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-stopped-snapshot",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-stopped-snapshot",
        idle_timeout_seconds=15,
        base_sha=job["base_sha"],
    )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', idle_deadline = NULL WHERE id = $1",
            sandbox["sandbox_id"],
        )

    capture = await store.claim_due_sandbox_snapshot()

    assert capture is not None
    assert capture["sandbox_id"] == sandbox["sandbox_id"]
    async with store._pool.acquire() as conn:
        capturing = await conn.fetchrow(
            "SELECT state, snapshot_state FROM agent_sandboxes WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert capturing is not None
    assert capturing["state"] == "stopped"
    assert capturing["snapshot_state"] == "snapshotting"

    # A valid receipt proves that the host started the exact container before
    # running Git. Durable state follows that Docker fact and re-arms timeout.
    assert (
        await store.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch="",
            content_sha256=hashlib.sha256(b"").hexdigest(),
        )
        == "published"
    )
    durable = await store.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["workspace_publication_state"] == "published"
    assert durable["idle_deadline"] is not None


async def test_stale_shared_snapshot_receipt_cannot_mutate_a_replacement_container(
    store: AgentJobStore,
):
    """Capture completion is fenced by the Docker generation and exact id."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-before-replacement",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-before-replacement",
        idle_timeout_seconds=15,
        base_sha=job["base_sha"],
    )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    capture = await store.claim_due_sandbox_snapshot()
    assert capture is not None

    async with store._pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET generation = generation + 1,
                container_id = 'container-after-replacement',
                state = 'stopped', snapshot_state = 'quiescent'
            WHERE id = $1
            """,
            sandbox["sandbox_id"],
        )

    assert (
        await store.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch="",
            content_sha256=hashlib.sha256(b"").hexdigest(),
        )
        is None
    )
    assert not await store.defer_sandbox_snapshot_capture(
        snapshot_id=capture["snapshot_id"],
        sandbox_id=capture["sandbox_id"],
        sandbox_generation=capture["generation"],
        container_id=capture["container_id"],
        detail="receipt lost its lifecycle fence",
        dirty=True,
    )
    async with store._pool.acquire() as conn:
        group = await conn.fetchrow(
            """
            SELECT generation, container_id, state, snapshot_state
            FROM agent_sandboxes WHERE id = $1
            """,
            sandbox["sandbox_id"],
        )
        snapshot_state = await conn.fetchval(
            "SELECT state FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
    assert group is not None
    assert dict(group) == {
        "generation": capture["generation"] + 1,
        "container_id": "container-after-replacement",
        "state": "stopped",
        "snapshot_state": "quiescent",
    }
    assert snapshot_state == "failed"


@pytest.mark.parametrize(
    ("failure_mode", "expected_snapshot_state"),
    [
        ("capture_dirty", "dirty"),
        ("capture_stalled", "pending"),
        ("publish_failed", "dirty"),
        ("publish_stalled", "dirty"),
    ],
)
async def test_shared_snapshot_failure_rearms_the_group_idle_deadline(
    store: AgentJobStore,
    failure_mode: str,
    expected_snapshot_state: str,
):
    """A failed coordinator must not leave an otherwise idle container running forever."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-failure",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-failure",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    capture = await store.claim_due_sandbox_snapshot()
    assert capture is not None

    if failure_mode == "capture_dirty":
        assert await store.defer_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            detail="unsafe writer census",
            dirty=True,
        )
    elif failure_mode == "capture_stalled":
        assert await store.reap_stalled_sandbox_snapshot_captures(stall_seconds=0) == [
            capture["snapshot_id"]
        ]
    else:
        patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
        assert await store.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        publication = await store.claim_for_publish()
        assert publication is not None
        if failure_mode == "publish_failed":
            await store.fail_snapshot_publish(
                snapshot_id=capture["snapshot_id"],
                detail="publisher rejected the immutable patch",
            )
        else:
            assert await store.reap_stalled_snapshot_publishes(stall_seconds=0) == [
                capture["snapshot_id"]
            ]

    async with store._pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT snapshot_state, idle_deadline FROM agent_sandboxes WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert group is not None
    assert group["snapshot_state"] == expected_snapshot_state
    assert group["idle_deadline"] is not None


async def test_native_fork_finalizes_two_concurrent_members_in_one_sandbox(
    store: AgentJobStore,
):
    """The durable saga creates a second session alias, never a Docker clone."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-source",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    operation = await store.prepare_session_fork(
        source_job_id=job["id"],
        user_id=job["user_id"],
        idempotency_key="native-fork-one",
    )
    assert operation is not None and operation.get("error") is None
    assert operation["state"] == "preparing"
    assert operation["sandbox_id"] == sandbox["sandbox_id"]
    assert operation["container_id"] == "container-shared-one"
    assert operation["target_thread_id"] != job["thread_id"]
    async with store._pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT 1 FROM agent_threads WHERE id = $1",
                operation["target_thread_id"],
            )
            is None
        )

    # The fork transaction cleared the durable group idle deadline. A
    # heartbeat sampled just before that transaction may still report the
    # supervisor as locally quiescent; it must not re-arm a stop while the
    # native mutation is in flight.
    assert (
        await store.reconcile_thread_sandbox_observation(
            thread_id=job["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-shared-one",
            sandbox_generation=sandbox["generation"],
            activation_generation=0,
            running=True,
            activity_active=False,
            idle_remaining_seconds=0,
            shared_live=True,
            members=[],
        )
        == "running"
    )
    async with store._pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT active_operation_count, idle_deadline FROM agent_sandboxes WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert group is not None
    assert group["active_operation_count"] == 1
    assert group["idle_deadline"] is None
    assert (
        await store.due_thread_sandbox_stop(
            thread_id=job["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-shared-one",
            sandbox_generation=sandbox["generation"],
            shared_live=True,
        )
        is None
    )

    coordinator = "coordinator-one"
    assert await store.claim_session_fork_materialization(
        operation_id=operation["id"],
        coordinator_token=coordinator,
        lease_seconds=210,
    )
    assert not await store.claim_session_fork_materialization(
        operation_id=operation["id"],
        coordinator_token="competing-coordinator",
        lease_seconds=210,
    )
    residencies = await store.establish_session_fork_residencies(
        operation_id=operation["id"],
        coordinator_token=coordinator,
        ttl_seconds=120,
    )
    assert residencies is not None
    assert await store.link_residency_grant(
        thread_id=residencies["source"]["thread_id"],
        generation=residencies["source"]["generation"],
        grant_id="grant-source-first",
    )
    assert await store.link_residency_grant(
        thread_id=residencies["target"]["thread_id"],
        generation=residencies["target"]["generation"],
        grant_id="grant-target-first",
    )
    # A heartbeat can have sampled STOPPED immediately before this fork
    # rotated the source residency. Its release must share the durable
    # operation fence or it can revoke the brand-new source process grant.
    assert (
        await store.release_shared_residency_if_quiescent(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            process_generation=1,
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
        )
        is None
    )
    assert (
        await store.heartbeat_residency(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            host="shared-runner",
            ttl_seconds=120,
        )
        == "grant-source-first"
    )
    retried_residencies = await store.establish_session_fork_residencies(
        operation_id=operation["id"],
        coordinator_token=coordinator,
        ttl_seconds=120,
    )
    assert retried_residencies is not None
    assert retried_residencies["source"]["generation"] == residencies["source"]["generation"] + 1
    assert retried_residencies["target"]["generation"] == residencies["target"]["generation"] + 1
    assert retried_residencies["source"]["previous_grant_id"] == "grant-source-first"
    assert retried_residencies["target"]["previous_grant_id"] == "grant-target-first"
    residencies = retried_residencies
    receipt = {
        "operation_id": operation["id"],
        "sandbox_id": operation["sandbox_id"],
        "sandbox_generation": operation["sandbox_generation"],
        "container_id": operation["container_id"],
        "runtime_id": operation["runtime_id"],
        "runtime_version": operation["runtime_version"],
        "source_thread_id": operation["source_thread_id"],
        "source_native_session_id": operation["source_native_session_id"],
        "target_thread_id": operation["target_thread_id"],
        "target_native_session_id": "native-child",
        "target_resident_slot_id": operation["target_resident_slot_id"],
        "source_residency_generation": residencies["source"]["generation"],
        "target_residency_generation": residencies["target"]["generation"],
        "source_process_generation": 2,
        "target_process_generation": 1,
    }
    materialized = await store.record_session_fork_materialized(
        operation_id=operation["id"],
        coordinator_token=coordinator,
        receipt=receipt,
    )
    assert materialized is not None and materialized["state"] == "materialized"
    child = await store.finalize_session_fork(operation_id=operation["id"])
    assert child is not None
    assert child["thread_id"] == operation["target_thread_id"]
    assert child["fork_source_job_id"] == job["id"]
    child_sandbox = await store.thread_sandbox(thread_id=child["thread_id"])
    source_sandbox = await store.thread_sandbox(thread_id=job["thread_id"])
    assert child_sandbox is not None and source_sandbox is not None
    assert child_sandbox["sandbox_id"] == source_sandbox["sandbox_id"] == sandbox["sandbox_id"]
    assert child_sandbox["container_id"] == source_sandbox["container_id"]
    assert child_sandbox["native_session_id"] == "native-child"
    assert source_sandbox["native_session_id"] == "native-source"
    assert len(child_sandbox["group_members"]) == 2
    assert await store.finalize_session_fork(operation_id=operation["id"]) == child

    # A STOPPED heartbeat can be sampled after the fork registered its new
    # residency generation but before the source process was restarted. By the
    # time it is committed, Fork is READY and the durable process generation is
    # newer. Neither reconciliation nor grant release may roll that process
    # back to stopped after the operation fence itself has cleared.
    assert (
        await store.reconcile_thread_sandbox_observation(
            thread_id=job["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-shared-one",
            sandbox_generation=sandbox["generation"],
            activation_generation=fence["activation_generation"],
            running=False,
            activity_active=False,
            idle_remaining_seconds=None,
            shared_live=True,
            members=[
                {
                    "thread_id": job["thread_id"],
                    "activation_generation": fence["activation_generation"],
                    "process_generation": 1,
                    "residency_generation": residencies["source"]["generation"],
                    "resident_state": "stopped",
                }
            ],
        )
        is None
    )
    source_after_stale_stop = await store.thread_sandbox(thread_id=job["thread_id"])
    assert source_after_stale_stop is not None
    assert source_after_stale_stop["process_generation"] == 2
    assert source_after_stale_stop["resident_state"] == "live"
    assert (
        await store.release_shared_residency_if_quiescent(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            process_generation=1,
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
        )
        is None
    )
    assert (
        await store.heartbeat_residency(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            host="shared-runner",
            ttl_seconds=120,
        )
        == ""
    )
    same_request = await store.prepare_session_fork(
        source_job_id=job["id"],
        user_id=job["user_id"],
        idempotency_key="native-fork-one",
    )
    assert same_request is not None and same_request["state"] == "ready"

    source_follow_up = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue source",
    )
    child_follow_up = await store.create_follow_up(
        parent_job_id=child["id"],
        user_id=child["user_id"],
        prompt="continue child",
    )
    assert source_follow_up is not None and source_follow_up["state"] == "queued"
    assert child_follow_up is not None and child_follow_up["state"] == "queued"
    first_claim = await store.claim_job(
        worker_id="shared-worker-a",
        lease_ttl_seconds=60,
        host="shared-runner",
        claim_attestation=_shared_claim_attestation(),
    )
    second_claim = await store.claim_job(
        worker_id="shared-worker-b",
        lease_ttl_seconds=60,
        host="shared-runner",
        claim_attestation=_shared_claim_attestation(),
    )
    assert first_claim is not None and second_claim is not None
    assert {first_claim["id"], second_claim["id"]} == {
        source_follow_up["id"],
        child_follow_up["id"],
    }


async def test_create_get_list_round_trip(store: AgentJobStore):
    """create -> get -> list returns the queued job with its fields."""
    job = await _create_job(store, metadata={"origin": "test"})
    assert job["state"] == "queued"
    assert job["id"].startswith("ajob_")

    fetched = await store.get_job(job["id"])
    assert fetched is not None
    assert fetched["task_prompt"] == "fix the flaky test"
    assert fetched["metadata"] == {"origin": "test"}

    listed = await store.list_jobs(user_id="user-1")
    assert [item["id"] for item in listed] == [job["id"]]
    assert await store.get_job("ajob_missing") is None


async def test_execution_profile_persists_and_is_inherited_by_follow_up_and_fork(
    store, monkeypatch
):
    legacy_default = await _create_job(store, task_prompt="trusted default")
    assert legacy_default["execution_profile"] == "trusted-v1"

    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "untrusted-kata-v1")
    store_default = await _create_job(store, task_prompt="secure store default")
    assert store_default["execution_profile"] == "untrusted-kata-v1"

    source = await _create_job(
        store,
        task_prompt="secure thread",
        execution_profile="untrusted-kata-v1",
        metadata={"execution_profile": "trusted-v1"},
    )
    assert source["execution_profile"] == "untrusted-kata-v1"
    async with store._pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'succeeded' WHERE id = $1", source["id"])

    follow_up = await store.create_follow_up(
        parent_job_id=source["id"], user_id="user-1", prompt="continue"
    )
    fork = await store.fork_thread(source_job_id=source["id"], user_id="user-1")

    assert follow_up is not None
    assert fork is not None
    assert follow_up["execution_profile"] == "untrusted-kata-v1"
    assert fork["execution_profile"] == "untrusted-kata-v1"
    assert (await store.get_job(source["id"]))["execution_profile"] == "untrusted-kata-v1"


async def test_projects_summarize_a_user_s_repos_and_page_one_at_a_time(store: AgentJobStore):
    """The sidebar's task tree: a folder per repo, and a page inside one."""
    first = await _create_job(store)
    follow_up = await store.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="and the docs"
    )
    assert follow_up is not None
    other = await _create_job(store, repo="example-org/other-repo")
    await _create_job(store, user_id="user-2", repo="example-org/not-mine")

    projects = await store.list_projects(user_id="user-1")

    by_repo = {project["repo"]: project for project in projects}
    assert set(by_repo) == {"example-org/example-repo", "example-org/other-repo"}
    # Two turns of one conversation are one task, and both are still queued.
    assert by_repo["example-org/example-repo"]["task_count"] == 1
    assert by_repo["example-org/example-repo"]["active_count"] == 2
    # Most recently active first.
    assert projects[0]["repo"] == "example-org/other-repo"

    paged = await store.list_jobs(user_id="user-1", repo="example-org/other-repo")
    assert [job["id"] for job in paged] == [other["id"]]
    # No filter still means every project.
    assert len(await store.list_jobs(user_id="user-1")) == 3


async def test_archived_threads_leave_the_active_project_tree(store: AgentJobStore):
    """A repo whose only thread is archived is not an active folder."""
    job = await _create_job(store)
    await store.set_thread_archived(job_id=job["id"], user_id="user-1", archived=True)

    assert await store.list_projects(user_id="user-1") == []
    archived = await store.list_projects(user_id="user-1", archived=True)
    assert [project["repo"] for project in archived] == ["example-org/example-repo"]


async def test_the_baseline_idempotently_migrates_legacy_threads(store: AgentJobStore):
    """A pre-existing agent_threads table gains the thread-state columns.

    The behaviour is unchanged; its owner is not. This used to be `initialize`
    running on every boot, and the ADD COLUMN trail it left behind is exactly
    why: creating the table "if not exists" does nothing when the table exists
    with the wrong columns. Running the migration twice pins that it is still
    idempotent, because the source ran it on every single startup.
    """
    async with store._pool.acquire() as conn:
        await conn.execute("ALTER TABLE agent_threads DROP COLUMN archived_at")
        await conn.execute("ALTER TABLE agent_threads DROP COLUMN pinned_at")

    await run_baseline_statements(store._pool)
    await run_baseline_statements(store._pool)

    async with store._pool.acquire() as conn:
        columns = await conn.fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'agent_threads'
              AND column_name IN ('archived_at', 'pinned_at')
            """
        )
    assert {row["column_name"]: row["data_type"] for row in columns} == {
        "archived_at": "timestamp with time zone",
        "pinned_at": "timestamp with time zone",
    }


async def test_archive_and_restore_filter_owned_threads_without_cancelling(
    store: AgentJobStore,
):
    """One owned job id archives every turn without changing run state."""
    parent = await _create_job(store)
    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="also update the docs"
    )
    assert child is not None
    foreign = await _create_job(
        store,
        user_id="user-2",
        repo="example-org/other-repo",
        task_prompt="unrelated task",
    )
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    assert claim["id"] == parent["id"]

    archived = await store.set_thread_archived(job_id=child["id"], user_id="user-1", archived=True)

    assert archived is not None
    assert archived["thread_id"] == parent["thread_id"]
    assert archived["archived_at"] is not None
    archived_again = await store.set_thread_archived(
        job_id=parent["id"], user_id="user-1", archived=True
    )
    assert archived_again == archived
    assert await store.list_jobs(user_id="user-1") == []
    assert {job["id"] for job in await store.list_jobs(user_id="user-1", archived=True)} == {
        parent["id"],
        child["id"],
    }
    live_parent = await store.get_job(parent["id"])
    assert live_parent is not None
    assert live_parent["state"] == "running"
    assert live_parent["cancel_requested"] is False

    assert (
        await store.set_thread_archived(job_id=foreign["id"], user_id="user-1", archived=True)
        is None
    )
    assert (
        await store.set_thread_archived(job_id="ajob_missing", user_id="user-1", archived=True)
        is None
    )
    assert [job["id"] for job in await store.list_jobs(user_id="user-2")] == [foreign["id"]]

    restored = await store.set_thread_archived(
        job_id=parent["id"], user_id="user-1", archived=False
    )

    assert restored == {"thread_id": parent["thread_id"], "archived_at": None}
    assert await store.list_jobs(user_id="user-1", archived=True) == []
    assert {job["id"] for job in await store.list_jobs(user_id="user-1")} == {
        parent["id"],
        child["id"],
    }


async def test_pin_and_unpin_owned_threads_are_idempotent_and_reorder_lists(
    store: AgentJobStore,
):
    """A pin covers every turn and its project, while foreign jobs remain private."""
    parent = await _create_job(store)
    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="also update the docs"
    )
    assert child is not None
    newer = await _create_job(store, repo="example-org/newer-repo")
    foreign = await _create_job(store, user_id="user-2", repo="example-org/foreign")
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET created_at = $2 WHERE id = $1",
            parent["id"],
            datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        await conn.execute(
            "UPDATE agent_jobs SET created_at = $2 WHERE id = $1",
            child["id"],
            datetime(2026, 7, 2, tzinfo=timezone.utc),
        )
        await conn.execute(
            "UPDATE agent_jobs SET created_at = $2 WHERE id = $1",
            newer["id"],
            datetime(2026, 7, 3, tzinfo=timezone.utc),
        )

    pinned = await store.set_thread_pinned(job_id=child["id"], user_id="user-1", pinned=True)

    assert pinned is not None
    assert pinned["thread_id"] == parent["thread_id"]
    assert pinned["pinned_at"] is not None
    assert (
        await store.set_thread_pinned(job_id=parent["id"], user_id="user-1", pinned=True) == pinned
    )
    jobs = await store.list_jobs(user_id="user-1")
    assert [job["id"] for job in jobs] == [child["id"], parent["id"], newer["id"]]
    assert jobs[0]["pinned_at"] == jobs[1]["pinned_at"] == pinned["pinned_at"]
    projects = await store.list_projects(user_id="user-1")
    assert [project["repo"] for project in projects] == [
        "example-org/example-repo",
        "example-org/newer-repo",
    ]
    assert projects[0]["pinned_count"] == 1
    assert projects[0]["pinned_at"] == pinned["pinned_at"]
    assert (
        await store.set_thread_pinned(job_id=foreign["id"], user_id="user-1", pinned=True) is None
    )
    assert (
        await store.set_thread_pinned(job_id="ajob_missing", user_id="user-1", pinned=True) is None
    )

    unpinned = await store.set_thread_pinned(job_id=parent["id"], user_id="user-1", pinned=False)

    assert unpinned == {"thread_id": parent["thread_id"], "pinned_at": None}
    assert [job["id"] for job in await store.list_jobs(user_id="user-1")] == [
        newer["id"],
        child["id"],
        parent["id"],
    ]
    assert [project["repo"] for project in await store.list_projects(user_id="user-1")] == [
        "example-org/newer-repo",
        "example-org/example-repo",
    ]


async def test_follow_up_waits_then_inherits_thread_messages_and_patch(store: AgentJobStore):
    parent = await _create_job(store)
    child = await store.create_follow_up(
        parent_job_id=parent["id"],
        user_id="user-1",
        prompt="now add a regression test",
        model="qwen-next",
    )
    assert child is not None
    assert child["state"] == "waiting"
    assert child["turn_no"] == 2

    parent_claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.append_event(
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        event_type="message",
        payload={"text": "implemented the fix"},
    )
    await store.save_artifact(
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    assert (await store.get_job(child["id"]))["state"] == "waiting"
    assert (await store.claim_for_publish())["job_id"] == parent["id"]
    assert await store.record_publish(
        job_id=parent["id"], pr_url="https://x/pr/1", commit_sha="a" * 40
    )
    child_after_publish = await store.get_job(child["id"])
    assert child_after_publish["state"] == "queued"
    assert child_after_publish["base_sha"] == "a" * 40
    context = await store.follow_up_context(job_id=child["id"])
    assert context == {
        "messages": [
            {"role": "user", "content": "fix the flaky test"},
            {"role": "assistant", "content": "implemented the fix"},
        ],
        "patch": None,
    }
    thread = await store.get_thread_for_job(job_id=child["id"], user_id="user-1")
    assert thread is not None
    assert [job["id"] for job in thread["jobs"]] == [parent["id"], child["id"]]


async def test_initialize_backfills_assistant_messages_from_legacy_events(store: AgentJobStore):
    """Existing job transcripts survive the conversation-table migration."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "legacy answer"},
    )
    async with store._pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_thread_messages WHERE job_id = $1 AND role = 'assistant'", job["id"]
        )

    # The message backfill moved into the baseline migration with the rest
    # of what initialize() used to do on every boot.
    await run_baseline_statements(store._pool)

    thread = await store.get_thread_for_job(job_id=job["id"], user_id="user-1")
    assert thread is not None
    assert [(message["role"], message["content"]) for message in thread["messages"]] == [
        ("user", "fix the flaky test"),
        ("assistant", "legacy answer"),
    ]


async def test_concurrent_follow_ups_form_one_linear_thread(store: AgentJobStore):
    """The thread lock serializes simultaneous sends instead of forking."""
    parent = await _create_job(store)
    children = await asyncio.gather(
        store.create_follow_up(
            parent_job_id=parent["id"], user_id="user-1", prompt="first concurrent turn"
        ),
        store.create_follow_up(
            parent_job_id=parent["id"], user_id="user-1", prompt="second concurrent turn"
        ),
    )
    ordered = sorted(
        (child for child in children if child is not None), key=lambda job: job["turn_no"]
    )
    assert [job["turn_no"] for job in ordered] == [2, 3]
    assert ordered[0]["parent_job_id"] == parent["id"]
    assert ordered[1]["parent_job_id"] == ordered[0]["id"]
    assert [job["state"] for job in ordered] == ["waiting", "waiting"]


async def _run_to_success(
    store: AgentJobStore, job: dict, *, message: str | None = None, patch: str | None = None
) -> dict:
    """Claim the queued job and finish it as succeeded, with optional output."""
    claim = await store.claim_job(worker_id="w-fork", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == job["id"]
    if message is not None:
        await store.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"text": message},
        )
    if patch is not None:
        await store.save_artifact(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            kind="patch",
            content=patch,
        )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    return claim


async def test_fork_thread_copies_history_and_carries_the_unpublished_patch(
    store: AgentJobStore,
):
    """A fork is a new thread whose next turn resumes the source's work."""
    source = await _create_job(store)
    await _run_to_success(
        store, source, message="implemented the fix", patch="diff --git a/x b/x\n"
    )

    fork = await store.fork_thread(source_job_id=source["id"], user_id="user-1")

    assert fork is not None
    assert fork["thread_id"] != source["thread_id"]
    assert fork["fork_source_job_id"] == source["id"]
    assert fork["state"] == "succeeded"
    assert fork["turn_no"] == 1
    assert fork["parent_job_id"] is None
    # The PR (if any) belongs to the source thread; the fork starts unpublished.
    assert fork["published_pr_url"] is None
    # Forking queues nothing.
    assert await store.claim_job(worker_id="w-idle", lease_ttl_seconds=60) is None

    thread = await store.get_thread_for_job(job_id=fork["id"], user_id="user-1")
    assert thread is not None
    assert thread["title"].endswith("(fork)")
    assert [job["id"] for job in thread["jobs"]] == [fork["id"]]
    assert [(message["role"], message["content"]) for message in thread["messages"]] == [
        ("user", "fix the flaky test"),
        ("assistant", "implemented the fix"),
    ]

    # The next turn in the fork runs immediately (a copy has no patch of its
    # own to wait on) from the same base, with the source's history and its
    # still-unpublished patch as context.
    child = await store.create_follow_up(
        parent_job_id=fork["id"], user_id="user-1", prompt="continue in the fork"
    )
    assert child is not None
    assert child["state"] == "queued"
    assert child["turn_no"] == 2
    assert child["base_sha"] == source["base_sha"]
    context = await store.follow_up_context(job_id=child["id"])
    assert context == {
        "messages": [
            {"role": "user", "content": "fix the flaky test"},
            {"role": "assistant", "content": "implemented the fix"},
        ],
        "patch": "diff --git a/x b/x\n",
    }

    # The publisher sees the source turn, never the copy.
    assert (await store.claim_for_publish())["job_id"] == source["id"]
    assert await store.claim_for_publish() is None


async def test_cancelled_parent_patch_resumes_in_its_waiting_follow_up(
    store: AgentJobStore,
):
    """A stopped run releases its queued conversation turn with partial work."""
    parent = await _create_job(store)
    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="continue after the stop"
    )
    assert child is not None
    assert child["state"] == "waiting"

    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    patch = "diff --git a/partial b/partial\n"
    await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "I started the fix before you stopped me"},
    )
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content=patch,
    )
    assert await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
    )

    assert (await store.get_job(child["id"]))["state"] == "queued"
    assert await store.follow_up_context(job_id=child["id"]) == {
        "messages": [
            {"role": "user", "content": "fix the flaky test"},
            {"role": "assistant", "content": "I started the fix before you stopped me"},
        ],
        "patch": patch,
    }
    resumed = await store.claim_job(worker_id="w2", lease_ttl_seconds=60)
    assert resumed["id"] == child["id"]
    assert await store.claim_for_publish() is None


async def test_forked_cancelled_source_patch_resumes_in_the_fork(
    store: AgentJobStore,
):
    """A fork of a stopped turn retains its source's recoverable partial patch."""
    source = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    patch = "diff --git a/partial b/partial\n"
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content=patch,
    )
    assert await store.transition(
        job_id=source["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
    )

    fork = await store.fork_thread(source_job_id=source["id"], user_id="user-1")
    assert fork is not None
    assert fork["state"] == "cancelled"
    child = await store.create_follow_up(
        parent_job_id=fork["id"], user_id="user-1", prompt="continue in the fork"
    )
    assert child is not None
    assert child["state"] == "queued"
    assert (await store.follow_up_context(job_id=child["id"]))["patch"] == patch
    assert await store.claim_for_publish() is None


async def test_fork_after_publish_carries_the_commit_not_the_patch(store: AgentJobStore):
    """A fork of published work bases on the commit instead of re-applying."""
    source = await _create_job(store)
    await _run_to_success(store, source, patch="diff --git a/x b/x\n")
    assert (await store.claim_for_publish())["job_id"] == source["id"]
    assert await store.record_publish(
        job_id=source["id"], pr_url="https://x/pr/1", commit_sha="a" * 40
    )

    fork = await store.fork_thread(source_job_id=source["id"], user_id="user-1")

    assert fork is not None
    assert fork["published_commit_sha"] == "a" * 40
    assert fork["published_pr_url"] is None
    child = await store.create_follow_up(
        parent_job_id=fork["id"], user_id="user-1", prompt="continue"
    )
    assert child is not None
    assert child["state"] == "queued"
    assert child["base_sha"] == "a" * 40
    assert (await store.follow_up_context(job_id=child["id"]))["patch"] is None


async def test_fork_thread_refuses_unsettled_and_foreign_anchors(store: AgentJobStore):
    """Only the owner may fork, and only from a settled turn."""
    source = await _create_job(store)
    assert await store.fork_thread(source_job_id=source["id"], user_id="user-1") is None

    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    assert await store.fork_thread(source_job_id=source["id"], user_id="user-1") is None

    assert await store.transition(
        job_id=source["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert await store.fork_thread(source_job_id=source["id"], user_id="someone-else") is None
    assert await store.fork_thread(source_job_id=source["id"], user_id="user-1") is not None


async def test_fork_from_an_earlier_turn_copies_only_that_prefix(store: AgentJobStore):
    """Rewind: forking turn one leaves later turns behind in the source."""
    first = await _create_job(store)
    await _run_to_success(store, first, message="answer one")
    second = await store.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="second question"
    )
    assert second is not None
    await _run_to_success(store, second, message="answer two")

    fork = await store.fork_thread(source_job_id=first["id"], user_id="user-1")

    assert fork is not None
    thread = await store.get_thread_for_job(job_id=fork["id"], user_id="user-1")
    assert thread is not None
    assert [(message["role"], message["content"]) for message in thread["messages"]] == [
        ("user", "fix the flaky test"),
        ("assistant", "answer one"),
    ]
    source_thread = await store.get_thread_for_job(job_id=second["id"], user_id="user-1")
    assert source_thread is not None
    assert len(source_thread["messages"]) == 4


async def test_follow_up_waits_for_successful_patch_to_publish(store: AgentJobStore):
    """Runner success alone is not a usable base for the next turn."""
    parent = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="continue"
    )
    assert child is not None
    assert child["state"] == "waiting"


async def test_shared_follow_up_ignores_a_legacy_job_patch(store: AgentJobStore):
    """Shared workspaces advance in place; their group snapshot publishes later."""
    parent, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": parent["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-follow-up",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=parent["id"])
    assert await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="legacy patch from before the shared-live cutover",
    )
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=parent["id"],
        context_status="live",
        native_session_id="native-shared-follow-up",
        idle_timeout_seconds=1800,
        base_sha=parent["base_sha"],
    )
    assert await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id=parent["user_id"], prompt="continue in place"
    )

    assert child is not None
    assert child["state"] == "queued"
    assert child["base_sha"] == parent["base_sha"]
    # A migration-era per-job patch must not bypass the shared group snapshot
    # publisher even though the turn remains usable immediately.
    assert await store.claim_for_publish() is None


async def test_shared_waiting_follow_up_releases_before_snapshot_publish(store: AgentJobStore):
    """A queued live turn never waits for the asynchronous group publisher."""
    parent, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": parent["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-waiting",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=parent["id"])
    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id=parent["user_id"], prompt="already waiting"
    )
    assert child is not None and child["state"] == "waiting"
    assert await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="legacy patch from before the shared-live cutover",
    )
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=parent["id"],
        context_status="live",
        native_session_id="native-shared-waiting",
        idle_timeout_seconds=1800,
        base_sha=parent["base_sha"],
    )
    assert await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    released = await store.get_job(child["id"])
    assert released is not None
    assert released["state"] == "queued"


async def test_follow_up_created_after_publish_uses_published_commit(store: AgentJobStore):
    """A later send starts from the branch tip even when no child was waiting."""
    parent = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (await store.claim_for_publish())["job_id"] == parent["id"]
    published_sha = "b" * 40
    assert await store.record_publish(
        job_id=parent["id"], pr_url="https://x/pr/2", commit_sha=published_sha
    )

    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="continue from the PR"
    )
    assert child is not None
    assert child["state"] == "queued"
    assert child["base_sha"] == published_sha


async def test_legacy_publish_branch_tip_releases_follow_up(store: AgentJobStore):
    """A pre-thread PR can be resumed from its existing branch head."""
    parent = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (await store.claim_for_publish())["job_id"] == parent["id"]
    assert await store.record_publish(
        job_id=parent["id"], pr_url="https://x/pr/legacy", commit_sha=None
    )
    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="resume the old PR"
    )
    assert child is not None
    assert child["state"] == "waiting"

    branch_tip = "c" * 40
    assert await store.resolve_legacy_published_commit(
        parent_job_id=parent["id"], commit_sha=branch_tip
    )
    assert (await store.get_job(parent["id"]))["published_commit_sha"] == branch_tip
    resumed = await store.get_job(child["id"])
    assert resumed["state"] == "queued"
    assert resumed["base_sha"] == branch_tip
    assert (await store.follow_up_context(job_id=child["id"]))["patch"] is None


async def test_claim_creates_fenced_attempt(store: AgentJobStore):
    """Claiming moves the job to running with attempt_no = generation = 1."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    assert claim["id"] == job["id"]
    assert claim["state"] == "running"
    assert claim["attempt_no"] == 1
    assert claim["lease_generation"] == 1

    # The job is no longer queued, so a second claim finds nothing.
    assert await store.claim_job(worker_id="w2", lease_ttl_seconds=60) is None


async def test_heartbeat_extends_and_fences(store: AgentJobStore):
    """Heartbeat succeeds with the right generation and fences the wrong one."""
    await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    beat = await store.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        lease_ttl_seconds=60,
    )
    assert beat["ok"] is True
    assert beat["cancel_requested"] is False

    stale = await store.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        lease_ttl_seconds=60,
    )
    assert stale["ok"] is False


async def test_event_seq_and_global_cursor(store: AgentJobStore):
    """Events get per-attempt seq 1..n and a strictly increasing global id."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    ids = []
    for index in range(3):
        event_id = await store.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"index": index},
        )
        assert event_id is not None
        ids.append(event_id)
    assert ids == sorted(ids)

    events = await store.list_events_after(job_id=job["id"], after_id=0)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["payload"]["index"] for event in events] == [0, 1, 2]

    tail = await store.list_events_after(job_id=job["id"], after_id=ids[1])
    assert [event["id"] for event in tail] == [ids[2]]


async def test_terminal_readiness_is_fenced_attempt_state(store: AgentJobStore):
    """Hot-path terminal checks do not replay events and stale workers cannot toggle them."""
    await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    assert await store.terminal_workspace_ready(attempt_id=claim["attempt_id"]) is False
    assert (
        await store.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        is not None
    )
    assert await store.terminal_workspace_ready(attempt_id=claim["attempt_id"]) is True
    assert (
        await store.fence_terminal_workspace(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"] + 1,
        )
        is False
    )
    assert await store.terminal_workspace_ready(attempt_id=claim["attempt_id"]) is True
    assert (
        await store.fence_terminal_workspace(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is True
    )
    assert await store.terminal_workspace_ready(attempt_id=claim["attempt_id"]) is False


async def test_retained_workspace_readiness_is_a_durable_thread_wide_fence(
    store: AgentJobStore,
):
    """Old turn URLs cannot mutate while an activation or fork owns the chat."""
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }

    assert await store.thread_workspace_ready(thread_id=job["thread_id"]) is False
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-retained-one",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.thread_workspace_ready(thread_id=job["thread_id"]) is False

    assert await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="lifecycle",
        payload={"phase": "workspace_ready"},
    )
    assert await store.thread_workspace_ready(thread_id=job["thread_id"]) is True
    assert await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="lifecycle",
        payload={"phase": "workspace_finalizing"},
    )
    assert await store.thread_workspace_ready(thread_id=job["thread_id"]) is False

    assert await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="new",
        native_session_id="native-session-one",
        idle_timeout_seconds=1800,
    )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert await store.thread_workspace_ready(thread_id=job["thread_id"]) is True


async def test_zombie_worker_is_fenced_out_after_reap(store: AgentJobStore):
    """After a reap, every write path of the old attempt is rejected."""
    job = await _create_job(store)
    old = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert (
        await store.append_event(
            attempt_id=old["attempt_id"],
            lease_generation=old["lease_generation"],
            event_type="message",
            payload={"n": 1},
        )
        is not None
    )

    await _expire_attempt(store, old["attempt_id"])
    actions = await store.reap_expired(max_attempts=3)
    assert actions == [{"job_id": job["id"], "attempt_id": old["attempt_id"], "action": "queued"}]

    # The supersede is announced as an append-only control event.
    events = await store.list_events_after(job_id=job["id"], after_id=0)
    assert events[-1]["event_type"] == "attempt_superseded"
    assert events[-1]["payload"]["reason"] == "lease_expired"

    # A new worker claims attempt 2 / generation 2.
    new = await store.claim_job(worker_id="w2", lease_ttl_seconds=60)
    assert new is not None
    assert new["attempt_no"] == 2
    assert new["lease_generation"] == 2

    # The zombie's every write path is fenced out.
    zombie_kwargs = {
        "attempt_id": old["attempt_id"],
        "lease_generation": old["lease_generation"],
    }
    assert await store.append_event(**zombie_kwargs, event_type="message", payload={}) is None
    assert (await store.heartbeat(**zombie_kwargs, lease_ttl_seconds=60))["ok"] is False
    assert (
        await store.transition(
            job_id=job["id"],
            **zombie_kwargs,
            from_states=("running",),
            to_state="succeeded",
        )
        is False
    )
    assert await store.save_artifact(**zombie_kwargs, kind="patch", content="diff") is None

    # The live attempt keeps working.
    assert (
        await store.append_event(
            attempt_id=new["attempt_id"],
            lease_generation=new["lease_generation"],
            event_type="message",
            payload={"n": 2},
        )
        is not None
    )


async def test_retained_admission_settlement_and_job_finish_share_one_boundary(
    store: AgentJobStore,
):
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    running = await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-retained-one",
        context_status="new",
    )
    assert running is not None
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    settled = await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="new",
        native_session_id="claude-session-one",
        idle_timeout_seconds=1800,
    )
    assert settled is not None
    assert settled["admitted_job_id"] == job["id"]

    observation = {
        "thread_id": job["thread_id"],
        "host": "runner-a",
        "sandbox_id": "asbx_test",
        "container_id": "container-retained-one",
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
        "running": True,
    }
    await store.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=True,
    )
    assert (await store.thread_sandbox(thread_id=job["thread_id"]))["idle_deadline"] is None
    await store.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=False,
        idle_remaining_seconds=120,
    )
    rearmed = await store.thread_sandbox(thread_id=job["thread_id"])
    assert rearmed["idle_deadline"] > datetime.now(timezone.utc)

    # A broker can restart between turns, reconstruct the immutable container
    # identity from Docker, and know no activation generation. A terminal that
    # wakes the stopped container must still promote the durable row and own
    # its idle clock, but only while no worker attempt owns it.
    assert await store.mark_thread_sandbox_stopped(
        thread_id=job["thread_id"],
        host="runner-a",
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
    )
    observation["activation_generation"] = 0
    promoted = await store.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=True,
    )
    assert promoted == "running"
    await store.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=False,
        idle_remaining_seconds=120,
    )
    rearmed_after_restart = await store.thread_sandbox(thread_id=job["thread_id"])
    assert rearmed_after_restart["state"] == "running"
    assert rearmed_after_restart["idle_deadline"] > datetime.now(timezone.utc)

    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    durable = await store.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["active_attempt_id"] is None
    assert durable["admitted_job_id"] is None
    assert durable["last_settled_job_id"] == job["id"]
    assert durable["native_session_id"] == "claude-session-one"


async def test_retained_container_identity_cannot_be_replaced_or_reimaged(
    store: AgentJobStore,
):
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert (
        await store.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )
    assert (
        await store.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-replacement",
            context_status="new",
        )
        is None
    )
    with pytest.raises(ValueError, match="immutable identity"):
        await store.reserve_thread_sandbox(
            thread_id=job["thread_id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host="runner-a",
            sandbox_id=sandbox["sandbox_id"],
            container_name=sandbox["container_name"],
            runtime_id=sandbox["runtime_id"],
            runtime_version=sandbox["runtime_version"],
            sandbox_image_digest="sha256:a-different-image",
            execution_profile=sandbox["execution_profile"],
        )


async def test_retained_follow_up_attempt_keeps_original_image_and_cli_pin(
    store: AgentJobStore,
):
    job = await _create_job(store)
    original = await store.claim_job(
        worker_id="w1",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation={
            "sandbox_runtime": "runc",
            "sandbox_image_digest": "sha256:original-image",
            "runtime_versions": {"claude-code": "2.1.220"},
            "runtime_versions_available": {"claude-code": ["2.1.220"]},
        },
    )
    assert original is not None
    sandbox = await store.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=original["attempt_id"],
        lease_generation=original["lease_generation"],
        host="runner-a",
        sandbox_id="asbx_pinned",
        container_name="freeinference-chat-pinned",
        runtime_id="claude-code",
        runtime_version=original["runtime_version"],
        sandbox_image_digest=original["sandbox_image_digest"],
        execution_profile=job["execution_profile"],
    )
    assert sandbox is not None
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": original["attempt_id"],
        "lease_generation": original["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-pinned",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="new",
        native_session_id="session-pinned",
        idle_timeout_seconds=1800,
    )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=original["attempt_id"],
        lease_generation=original["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    child = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue",
    )
    assert child is not None and child["state"] == "queued"

    follow_up = await store.claim_job(
        worker_id="w2",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation={
            "sandbox_runtime": "runc",
            "sandbox_image_digest": "sha256:new-runner-image",
            "runtime_versions": {"claude-code": "2.1.999"},
            "runtime_versions_available": {"claude-code": ["2.1.999"]},
        },
    )

    assert follow_up is not None
    assert follow_up["runtime_version"] == "2.1.220"
    assert follow_up["sandbox_image_digest"] == "sha256:original-image"
    assert follow_up["thread_sandbox"]["container_id"] == "container-pinned"


async def test_failed_preadmission_activation_requests_an_immediate_stop(
    store: AgentJobStore,
):
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert (
        await store.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )

    abandoned = await store.abandon_thread_sandbox(
        **fence,
        lost=False,
        detail="checkout failed",
        idle_timeout_seconds=1800,
    )

    assert abandoned is not None
    assert abandoned["state"] == "stopping"
    assert abandoned["active_attempt_id"] is None
    assert abandoned["idle_deadline"] is None


async def test_failed_preadmission_before_container_creation_can_be_reprovisioned(
    store: AgentJobStore,
):
    job, claim, sandbox = await _reserve_retained_chat(store)

    abandoned = await store.abandon_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
        lost=False,
        detail="image pull failed",
        idle_timeout_seconds=1800,
    )

    assert abandoned is not None
    assert abandoned["container_id"] is None
    assert abandoned["state"] == "provisioning"
    assert abandoned["context_status"] == "new"
    assert abandoned["active_attempt_id"] is None
    assert abandoned["idle_deadline"] is None


async def test_invalid_context_before_admission_marks_chat_and_queued_turns_lost(
    store: AgentJobStore,
):
    """A missing native locator fails closed before any new prompt is sent."""
    job, claim, sandbox = await _reserve_retained_chat(store)
    child = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="do not leave this turn waiting forever",
    )
    assert child is not None and child["state"] == "waiting"

    abandoned = await store.abandon_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
        lost=True,
        detail="native session locator is missing",
        idle_timeout_seconds=1800,
    )

    assert abandoned is not None
    assert abandoned["state"] == "stopped_dirty"
    assert abandoned["context_status"] == "lost"
    queued = await store.get_job(child["id"])
    assert queued["state"] == "failed"
    assert "lost the retained sandbox context" in queued["detail"]
    assert await store.create_follow_up(
        parent_job_id=child["id"],
        user_id=job["user_id"],
        prompt="still must not be accepted",
    ) == {"error": "sandbox_context_lost"}


async def test_failed_admitted_turn_does_not_promote_queued_follow_up(
    store: AgentJobStore,
):
    """A terminal job and a LOST context are different scheduling signals."""
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-retained-one",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    child = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="this cannot run after an ambiguous turn",
    )
    assert child is not None and child["state"] == "waiting"

    assert await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="agent exited after prompt admission",
    )

    durable = await store.thread_sandbox(thread_id=job["thread_id"])
    assert durable["context_status"] == "lost"
    blocked = await store.get_job(child["id"])
    assert blocked["state"] == "failed"
    assert "lost the retained sandbox context" in blocked["detail"]


async def test_failed_preadmission_late_container_observation_is_stopped(
    store: AgentJobStore,
):
    """A Docker create that outran /running cannot become an immortal orphan."""
    job, claim, sandbox = await _reserve_retained_chat(store)
    abandoned = await store.abandon_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
        lost=False,
        detail="supervisor response was lost",
        idle_timeout_seconds=1800,
    )
    assert abandoned is not None and abandoned["state"] == "provisioning"

    recovered = await store.reconcile_thread_sandbox_observation(
        thread_id=job["thread_id"],
        host="runner-a",
        sandbox_id="asbx_test",
        container_id="container-created-before-response-loss",
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
        running=True,
        # Model the supervisor lease whose acquire response never reached the
        # runner. Durable ownership is already gone, so it must not block stop.
        activity_active=True,
    )

    assert recovered == "stopping"
    durable = await store.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["container_id"] == "container-created-before-response-loss"
    assert durable["state"] == "stopping"
    due = await store.due_thread_sandbox_stop(
        thread_id=job["thread_id"],
        host="runner-a",
        sandbox_id="asbx_test",
        container_id="container-created-before-response-loss",
        sandbox_generation=sandbox["generation"],
    )
    assert due is not None
    assert due["reason"] == "orphaned_attempt"
    assert due["activation_generation"] == sandbox["activation_generation"]


async def test_expired_shared_admission_loses_only_that_member_and_residency(
    store: AgentJobStore,
):
    """One ambiguous fork member cannot poison its siblings or keep spending."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    residency = await store.establish_residency(
        thread_id=job["thread_id"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        runtime_id="claude-code",
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
    )
    assert residency is not None
    assert await store.record_shared_thread_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_id=sandbox["sandbox_id"],
        residency_generation=residency["generation"],
    )
    assert await store.link_residency_grant(
        thread_id=job["thread_id"],
        generation=residency["generation"],
        grant_id="grant-shared-expired",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    child = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="must not run after an ambiguous shared turn",
    )
    assert child is not None and child["state"] == "waiting"

    sibling = await _create_job(store, task_prompt="independent fork sibling")
    async with store._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_thread_sandbox_memberships (
                thread_id, sandbox_id, native_session_id, native_state,
                context_status, process_generation, resident_state,
                resident_slot_id
            ) VALUES ($1, $2, 'native-sibling', 'ready', 'live', 7, 'live', 1)
            """,
            sibling["thread_id"],
            sandbox["sandbox_id"],
        )
    await _expire_attempt(store, claim["attempt_id"])

    actions = await store.reap_expired(max_attempts=3)

    assert actions == [
        {
            "job_id": job["id"],
            "attempt_id": claim["attempt_id"],
            "action": "failed",
            "cleanup_residency": {
                "thread_id": job["thread_id"],
                "generation": residency["generation"],
                "host": "shared-runner",
                "grant_id": "grant-shared-expired",
            },
        }
    ]
    failed = await store.thread_sandbox(thread_id=job["thread_id"])
    surviving = await store.thread_sandbox(thread_id=sibling["thread_id"])
    assert failed is not None and surviving is not None
    assert failed["native_state"] == "lost"
    assert failed["context_status"] == "lost"
    assert failed["resident_state"] == "lost"
    assert failed["active_attempt_id"] is None
    assert failed["admitted_job_id"] is None
    assert surviving["native_state"] == "ready"
    assert surviving["context_status"] == "live"
    assert surviving["resident_state"] == "live"
    assert surviving["state"] == "running"
    assert surviving["idle_deadline"] is not None
    assert (await store.get_job(child["id"]))["state"] == "failed"
    async with store._pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT status FROM agent_thread_residencies WHERE thread_id = $1",
                job["thread_id"],
            )
            == "none"
        )

    # A late host census can report the orphan process, but LOST is durable
    # and may not be promoted back to LIVE by observation alone.
    await store.reconcile_thread_sandbox_observation(
        thread_id=job["thread_id"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        container_id="container-shared-one",
        sandbox_generation=sandbox["generation"],
        activation_generation=0,
        running=True,
        activity_active=False,
        idle_remaining_seconds=120,
        shared_live=True,
        members=[
            {
                "thread_id": job["thread_id"],
                "activation_generation": sandbox["activation_generation"],
                "process_generation": 99,
                "residency_generation": residency["generation"],
                "resident_state": "live",
            }
        ],
    )
    still_lost = await store.thread_sandbox(thread_id=job["thread_id"])
    assert still_lost is not None
    assert still_lost["resident_state"] == "lost"
    assert still_lost["native_state"] == "lost"


async def test_expired_shared_finalization_preserves_context_and_snapshot_progress(
    store: AgentJobStore,
):
    """A settled prompt is never replayed and still enters the group snapshot."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-settled",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    child = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue from the settled shared context",
    )
    assert child is not None and child["state"] == "waiting"
    await _expire_attempt(store, claim["attempt_id"])

    actions = await store.reap_expired(max_attempts=3)

    assert actions == [{"job_id": job["id"], "attempt_id": claim["attempt_id"], "action": "failed"}]
    durable = await store.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["native_state"] == "ready"
    assert durable["context_status"] == "live"
    assert durable["native_session_id"] == "native-shared-settled"
    assert durable["last_settled_job_id"] == job["id"]
    assert durable["active_attempt_id"] is None
    assert durable["admitted_job_id"] is None
    assert durable["idle_deadline"] is not None
    assert (await store.get_job(child["id"]))["state"] == "queued"
    capture = await store.claim_due_sandbox_snapshot()
    assert capture is not None
    assert capture["anchor_job_id"] == job["id"]


async def test_expired_job_finalization_preserves_an_already_settled_context(
    store: AgentJobStore,
):
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert (
        await store.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )
    assert await store.record_continuity_mode(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        mode=None,
        context_status="new",
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert (
        await store.settle_thread_sandbox(
            **fence,
            job_id=job["id"],
            context_status="new",
            native_session_id="claude-session-one",
            idle_timeout_seconds=1800,
        )
        is not None
    )
    child = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue from the already-settled context",
    )
    assert child is not None and child["state"] == "waiting"
    await _expire_attempt(store, claim["attempt_id"])

    actions = await store.reap_expired(max_attempts=3)

    assert actions == [{"job_id": job["id"], "attempt_id": claim["attempt_id"], "action": "failed"}]
    durable = await store.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["context_status"] == "new"
    assert durable["last_settled_job_id"] == job["id"]
    assert durable["admitted_job_id"] is None
    assert durable["idle_deadline"] is not None
    assert (await store.job_turn_telemetry(job_id=job["id"]))["context_status"] == "new"
    failed = await store.get_job(job["id"])
    assert failed["state"] == "failed"
    assert "prompt was not replayed" in failed["detail"]
    assert (await store.get_job(child["id"]))["state"] == "queued"


async def test_expired_retained_admission_fails_closed_instead_of_replaying(
    store: AgentJobStore,
):
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert (
        await store.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )
    assert await store.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    child = await store.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="must not run after an expired admitted turn",
    )
    assert child is not None and child["state"] == "waiting"
    await _expire_attempt(store, claim["attempt_id"])

    actions = await store.reap_expired(max_attempts=3)

    assert actions == [{"job_id": job["id"], "attempt_id": claim["attempt_id"], "action": "failed"}]
    assert (await store.get_job(job["id"]))["state"] == "failed"
    durable = await store.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "stopped_dirty"
    assert durable["context_status"] == "lost"
    assert durable["active_attempt_id"] is None
    assert durable["admitted_job_id"] is None
    assert (await store.job_turn_telemetry(job_id=job["id"]))["context_status"] == "lost"
    failed_child = await store.get_job(child["id"])
    assert failed_child["state"] == "failed"
    assert "lost the retained sandbox context" in failed_child["detail"]


async def test_reap_fails_job_after_max_attempts(store: AgentJobStore):
    """Exhausting the attempt budget fails the job instead of requeueing."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await _expire_attempt(store, claim["attempt_id"])

    actions = await store.reap_expired(max_attempts=1)
    assert actions[0]["action"] == "failed"
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert "exhausted" in fetched["detail"]
    assert await store.list_terminal_resumes_pending() == [job["id"]]
    assert await store.mark_terminal_resume_complete(job_id=job["id"]) is True
    assert await store.list_terminal_resumes_pending() == []


async def test_a_publish_claim_carries_the_branch_the_job_was_started_from(store: AgentJobStore):
    """The publisher targets its PR at the job's own base, so it must see it.

    ``base_ref`` is not a column — the column holds the resolved ``base_sha``,
    because a branch moves — so the branch the owner picked rides along in
    ``metadata``. Leaving ``metadata`` out of this claim is what made every
    draft PR target the deployment default no matter what the job was pinned
    from.
    """
    job = await _create_job(store, metadata={"_agent_base_ref": "release/x"})
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    taken = await store.claim_for_publish()

    assert taken is not None
    assert taken["metadata"] == {"_agent_base_ref": "release/x"}


async def test_publish_is_one_shot(store: AgentJobStore):
    """A finished job is published at most once, by the platform publisher.

    Driven through the path that actually runs. The worker-side transitions
    this used to exercise were removed: nothing called them, and they let a
    sandbox runner's token write the terminal publish record from an arbitrary
    string.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    taken = await store.claim_for_publish()
    assert taken is not None and taken["job_id"] == job["id"]
    # A second publisher finds nothing: the claim moved it out of reach.
    assert await store.claim_for_publish() is None

    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/1") is True
    # And the URL is written exactly once, so a racing publisher cannot
    # overwrite it with a second PR.
    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/2") is False

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "succeeded"
    assert fetched["published_pr_url"] == "https://x/pr/1"


async def test_a_job_left_publishing_is_failed_not_republished(store: AgentJobStore):
    """A publish that never completes ends as failed, never as a second push.

    The branch or PR may already exist by the time anyone notices, so the rule
    is the same wherever this is detected: fail it for a human, do not retry.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await store.claim_for_publish()

    assert await store.reap_stalled_publishes(stall_seconds=0) == [job["id"]]

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert fetched["published_pr_url"] is None


async def test_cancel_queued_and_running(store: AgentJobStore):
    """Queued jobs cancel immediately; running jobs cancel via the worker."""
    queued = await _create_job(store)
    assert await store.request_cancel(job_id=queued["id"]) == "cancelled"
    assert (await store.get_job(queued["id"]))["state"] == "cancelled"
    assert await store.list_terminal_resumes_pending() == [queued["id"]]
    assert await store.mark_terminal_resume_complete(job_id=queued["id"]) is True

    # Owner scoping: the wrong user cannot cancel.
    running = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert await store.request_cancel(job_id=running["id"], user_id="someone-else") is None

    assert await store.request_cancel(job_id=running["id"], user_id="user-1") == "running"
    beat = await store.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        lease_ttl_seconds=60,
    )
    assert beat["ok"] is True
    assert beat["cancel_requested"] is True

    # The worker performs the fenced terminal transition.
    assert (
        await store.transition(
            job_id=running["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="cancelled",
        )
        is True
    )
    assert (await store.get_job(running["id"]))["state"] == "cancelled"

    # Cancelling a terminal job is a no-op that reports the state.
    assert await store.request_cancel(job_id=running["id"]) == "cancelled"


async def test_artifact_upsert_and_get(store: AgentJobStore):
    """Artifacts upsert per (job, attempt, kind) and read back the latest."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }

    first = await store.save_artifact(**fence, kind="patch", content="diff-v1")
    assert first is not None
    second = await store.save_artifact(**fence, kind="patch", content="diff-v2")
    assert second == first  # replaced in place, not duplicated

    artifact = await store.get_artifact(job_id=job["id"], kind="patch")
    assert artifact is not None
    assert artifact["content"] == "diff-v2"
    assert await store.get_artifact(job_id=job["id"], kind="missing") is None


async def test_model_credential_follows_the_fence(store: AgentJobStore):
    """A worker token buys inference only while its attempt owns the job.

    This is the revocation mechanism for the sandbox's model access: there is
    no key to revoke, so the checks that matter are that a live fence resolves
    and that every way of losing the fence stops resolving.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }

    identity = await store.resolve_model_credential(**fence)
    assert identity is not None
    assert identity["user_id"] == "user-1"
    # The budget assertion that sat here went with the column: a grant scopes
    # what may be called, and the per-user daily quota governs how much. Every
    # fence assertion below — the subject of this test — is unchanged.

    # A different generation never resolves.
    assert (
        await store.resolve_model_credential(
            job_id=job["id"], attempt_id=claim["attempt_id"], lease_generation=99
        )
        is None
    )

    # Lease expiry alone stops it, even before the reaper runs.
    await _expire_attempt(store, claim["attempt_id"])
    assert await store.resolve_model_credential(**fence) is None

    # After the reap the new attempt resolves and the old one stays dead.
    await store.reap_expired(max_attempts=3)
    retry = await store.claim_job(worker_id="w2", lease_ttl_seconds=60)
    assert await store.resolve_model_credential(**fence) is None
    assert (
        await store.resolve_model_credential(
            job_id=job["id"],
            attempt_id=retry["attempt_id"],
            lease_generation=retry["lease_generation"],
        )
        is not None
    )


async def test_model_credential_dies_with_a_terminal_job(store: AgentJobStore):
    """Finishing the job stops its token from buying more inference."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await store.resolve_model_credential(**fence) is not None

    await store.transition(**fence, from_states=("running",), to_state="succeeded")
    assert await store.resolve_model_credential(**fence) is None


async def test_model_credential_carries_the_owner_role_and_dies_with_the_account(
    store: AgentJobStore,
):
    """Model calls run at the owner's role, and stop when the account does.

    The role decides which models the sandbox can call — it must be the
    owner's, or the composer offers models whose first call 404s. The status
    check is the other half: a suspended owner's running job must stop buying
    inference without waiting for the reaper.
    """
    async with store._pool.acquire() as conn:
        # Mirror the production schema's constrained columns; everything else
        # is nullable or defaulted. IF NOT EXISTS keeps this compatible with a
        # test database where the full auth schema already exists.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'free',
                status TEXT DEFAULT 'active'
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO users (id, email, password_hash, role, status)
            VALUES ('user-1', 'agent-owner-role@test.invalid', 'x', 'internal', 'active')
            ON CONFLICT (id) DO UPDATE SET role = 'internal', status = 'active'
            """
        )
    try:
        job = await _create_job(store)
        claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
        fence = {
            "job_id": job["id"],
            "attempt_id": claim["attempt_id"],
            "lease_generation": claim["lease_generation"],
        }

        identity = await store.resolve_model_credential(**fence)
        assert identity is not None
        assert identity["role"] == "internal"

        async with store._pool.acquire() as conn:
            await conn.execute("UPDATE users SET status = 'suspended' WHERE id = 'user-1'")
        assert await store.resolve_model_credential(**fence) is None
    finally:
        async with store._pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = 'user-1'")


async def test_publish_claim_is_exactly_once(store: AgentJobStore):
    """Two publishers cannot both take the same finished job.

    The publish step has an externally visible side effect, so 'at most once'
    has to hold at the database level rather than by convention.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await store.save_artifact(**fence, kind="patch", content="diff --git a/x b/x\n")
    await store.transition(
        job_id=job["id"], **fence, from_states=("running",), to_state="succeeded"
    )

    first = await store.claim_for_publish()
    assert first is not None
    assert first["job_id"] == job["id"]
    assert first["patch"].startswith("diff --git")

    # Already claimed (now `publishing`), so a second publisher finds nothing.
    assert await store.claim_for_publish() is None

    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/1") is True
    # Recording twice is refused, so a retry cannot open a second PR.
    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/2") is False

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "succeeded"
    assert fetched["published_pr_url"] == "https://x/pr/1"


async def test_jobs_without_a_patch_are_not_published(store: AgentJobStore):
    """A job that changed nothing must never produce an empty PR."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert await store.claim_for_publish() is None


async def test_failed_publish_surfaces_the_reason(store: AgentJobStore):
    """A rejected patch fails the job with a reason the owner can read."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await store.save_artifact(**fence, kind="patch", content="diff --git a/x b/x\n")
    await store.transition(
        job_id=job["id"], **fence, from_states=("running",), to_state="succeeded"
    )
    await store.claim_for_publish()

    await store.fail_publish(job_id=job["id"], detail="patch rejected: modifies .github/")
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert ".github/" in fetched["detail"]

    events = await store.list_events_after(job_id=job["id"], after_id=0)
    assert events[-1]["event_type"] == "error"
    assert events[-1]["payload"]["phase"] == "publish_rejected"


async def test_an_expired_lease_is_dead_before_the_reaper_runs(store: AgentJobStore):
    """Expiry must take effect immediately, not when the reaper next passes.

    Previously every fenced write checked only generation and status, so a
    stalled worker kept full authority for up to a reaper interval after its
    lease ran out — it could renew, write events, store artifacts and finish
    the job. The reaper is a cleanup mechanism, not the thing that revokes.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    # Everything works while the lease is live.
    assert await store.append_event(**fence, event_type="message", payload={}) is not None

    await _expire_attempt(store, claim["attempt_id"])
    # Deliberately do NOT run the reaper.

    assert (await store.heartbeat(**fence, lease_ttl_seconds=60))["ok"] is False
    assert await store.append_event(**fence, event_type="message", payload={}) is None
    assert await store.save_artifact(**fence, kind="patch", content="x") is None
    assert (
        await store.transition(
            job_id=job["id"], **fence, from_states=("running",), to_state="succeeded"
        )
        is False
    )
    # And the job cannot be moved into publishing either.
    assert (
        await store.transition(
            job_id=job["id"], **fence, from_states=("running",), to_state="publishing"
        )
        is False
    )

    # And the job is untouched: still running, no artifact, no terminal state.
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "running"
    assert await store.get_artifact(job_id=job["id"], kind="patch") is None


async def test_a_released_claim_costs_no_retry(store: AgentJobStore):
    """A claim the platform gave up on must not spend the job's retry budget.

    The regression this pins: the budget was read off ``attempt_no``, which
    numbers every claim. A GitHub outage lasting across `max_attempts` claim
    cycles would therefore fail every queued private-repo job outright, without
    an agent ever having started — while the whole point of releasing the claim
    is that the job never got its turn.
    """
    job = await _create_job(store)

    # Three claims the platform abandons before the agent starts.
    for _ in range(3):
        claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
        assert claim is not None, "a released job must be claimable again"
        assert await store.release_claim(job_id=job["id"], attempt_id=claim["attempt_id"])
        assert (await store.get_job(job["id"]))["state"] == "queued"

    # The budget is untouched: a real attempt still gets to run and be reaped.
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await _expire_attempt(store, claim["attempt_id"])
    actions = await store.reap_expired(max_attempts=3)

    assert actions[0]["action"] == "queued", "aborted claims must not count as attempts"
    assert (await store.get_job(job["id"]))["state"] == "queued"


async def test_releasing_records_why_rather_than_erasing_the_attempt(store: AgentJobStore):
    """History stays append-only: the abandoned attempt is marked, not deleted."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    await store.release_claim(job_id=job["id"], attempt_id=claim["attempt_id"])

    events = await store.list_events_after(job_id=job["id"])
    aborted = [e for e in events if e["event_type"] == "attempt_aborted"]
    assert aborted, "an operator must be able to see the platform dropped this one"
    assert aborted[0]["attempt_id"] == claim["attempt_id"]


async def test_releasing_a_job_that_moved_on_is_a_no_op(store: AgentJobStore):
    """A late release must never drag a running job back to the queue."""
    job = await _create_job(store)
    first = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.release_claim(job_id=job["id"], attempt_id=first["attempt_id"])
    second = await store.claim_job(worker_id="w2", lease_ttl_seconds=60)

    # The first attempt is already finished, so releasing it again changes nothing.
    assert await store.release_claim(job_id=job["id"], attempt_id=first["attempt_id"]) is False

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "running"
    assert fetched["current_attempt_id"] == second["attempt_id"]


async def test_a_publish_the_publisher_abandoned_is_swept(store: AgentJobStore):
    """A job stuck in `publishing` must not stay there forever.

    `claim_for_publish` moves the job in its own transaction; if the publisher
    then dies, nothing else touches that row. The attempt reaper cannot help —
    it scans *running attempts*, and this job's attempt finished before
    publishing began — so without this sweep the job is invisible to its owner
    and to the publish queue indefinitely.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(from_states=("running",), to_state="succeeded", **fence)

    taken = await store.claim_for_publish()
    assert taken is not None
    assert (await store.get_job(job["id"]))["state"] == "publishing"

    # Nothing has stalled yet, so a sweep must leave it alone.
    assert await store.reap_stalled_publishes(stall_seconds=3600) == []
    assert (await store.get_job(job["id"]))["state"] == "publishing"

    # Past the deadline it is failed, not retried: the branch or PR may already
    # exist and a second automatic publish must never happen.
    assert await store.reap_stalled_publishes(stall_seconds=0) == [job["id"]]
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert "manual review" in fetched["detail"]


async def test_the_sweep_leaves_a_published_job_alone(store: AgentJobStore):
    """Only jobs with no PR recorded are swept."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await store.claim_for_publish()
    await store.record_publish(job_id=job["id"], pr_url="https://github.com/o/n/pull/1")

    assert await store.reap_stalled_publishes(stall_seconds=0) == []
    assert (await store.get_job(job["id"]))["state"] == "succeeded"


async def test_releasing_with_the_wrong_generation_is_refused(store: AgentJobStore):
    """The weakest fence in the store was this one; it now matches the others.

    Releasing a claim returns the job to the queue, so an unfenced release is a
    way to yank a job out from under the attempt that legitimately holds it.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    assert (
        await store.release_claim(
            job_id=job["id"], attempt_id=claim["attempt_id"], lease_generation=999
        )
        is False
    )
    assert (await store.get_job(job["id"]))["state"] == "running"

    assert (
        await store.release_claim(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is True
    )


async def test_a_release_writes_its_event_into_the_right_job(store: AgentJobStore):
    """agent_job_events.job_id has no foreign key, so a mismatched pair would land
    a control event in another job's stream — possibly another tenant's."""
    victim = await _create_job(store)
    target = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    # Deliberately name the wrong job alongside the real attempt id.
    wrong = victim["id"] if claim["id"] != victim["id"] else target["id"]
    await store.release_claim(job_id=wrong, attempt_id=claim["attempt_id"])

    stray = await store.list_events_after(job_id=wrong)
    assert not [e for e in stray if e["event_type"] == "attempt_aborted"], (
        "the event must follow the attempt's real job, not the caller's claim"
    )
    owned = await store.list_events_after(job_id=claim["id"])
    assert [e for e in owned if e["event_type"] == "attempt_aborted"]


async def test_a_cancelled_job_released_from_a_claim_ends_cancelled(store: AgentJobStore):
    """Requeueing a cancelled job put it somewhere nothing could ever reach.

    `claim_job` skips queued rows with `cancel_requested`, and the reaper only
    reaches jobs that still have a *running* attempt — which a released one
    does not. So the job sat in `queued` permanently, invisible to its owner's
    cancellation and to every worker.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert await store.request_cancel(job_id=job["id"]) == "running"

    await store.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "cancelled", "a cancelled job must not be requeued"
    assert await store.list_terminal_resumes_pending() == [job["id"]]


async def test_an_uncancelled_job_still_returns_to_the_queue(store: AgentJobStore):
    """The ordinary release path is unchanged."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    await store.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    assert (await store.get_job(job["id"]))["state"] == "queued"
    assert await store.claim_job(worker_id="w2", lease_ttl_seconds=60) is not None


# ── Runner host pool ───────────────────────────────────────────────────


async def test_a_polling_host_joins_the_pool(store: AgentJobStore):
    await store.touch_runner_host(host="runner-a", worker_id="runner-1")

    hosts = await store.list_runner_hosts()

    assert [h["host"] for h in hosts] == ["runner-a"]
    assert hosts[0]["is_active"] is False
    assert hosts[0]["last_worker_id"] == "runner-1"


async def test_claims_are_unrestricted_until_a_host_is_pinned(store: AgentJobStore):
    await _create_job(store)

    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claim is not None


async def test_a_pinned_host_is_the_only_one_that_claims(store: AgentJobStore):
    await _create_job(store)
    await store.touch_runner_host(host="runner-b", worker_id="w2")
    assert await store.set_active_runner_host(host="runner-b")

    assert await store.claim_job(worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None
    # ...and a runner predating host reporting is refused with them.
    assert await store.claim_job(worker_id="w-old", lease_ttl_seconds=60) is None
    assert await store.claim_job(worker_id="w2", lease_ttl_seconds=60, host="runner-b") is not None


async def test_a_refused_host_still_enters_the_pool(store: AgentJobStore):
    """You cannot switch to a host you cannot see."""
    await store.touch_runner_host(host="runner-a", worker_id="w1")
    await store.set_active_runner_host(host="runner-a")
    await _create_job(store)

    assert await store.claim_job(worker_id="w2", lease_ttl_seconds=60, host="runner-b") is None

    assert "runner-b" in {h["host"] for h in await store.list_runner_hosts()}


async def test_pinning_an_unknown_host_leaves_the_current_pin_alone(store: AgentJobStore):
    """Regression: clearing first meant a typo silently unpinned the deployment.

    The failed switch reported itself correctly and then handed every machine
    the queue back — the one outcome worse than refusing the change.
    """
    await store.touch_runner_host(host="runner-b", worker_id="w1")
    await store.set_active_runner_host(host="runner-b")

    assert await store.set_active_runner_host(host="runner-b-typo") is False

    hosts = {h["host"]: h["is_active"] for h in await store.list_runner_hosts()}
    assert hosts == {"runner-b": True}


async def test_only_one_host_can_be_active(store: AgentJobStore):
    await store.touch_runner_host(host="runner-a", worker_id="w1")
    await store.touch_runner_host(host="runner-b", worker_id="w2")

    await store.set_active_runner_host(host="runner-a")
    await store.set_active_runner_host(host="runner-b")

    active = [h["host"] for h in await store.list_runner_hosts() if h["is_active"]]
    assert active == ["runner-b"]


async def test_unpinning_restores_open_claiming(store: AgentJobStore):
    await _create_job(store)
    await store.touch_runner_host(host="runner-b", worker_id="w2")
    await store.set_active_runner_host(host="runner-b")

    assert await store.set_active_runner_host(host=None) is True

    assert await store.claim_job(worker_id="w1", lease_ttl_seconds=60, host="runner-a") is not None


async def test_forgetting_a_host_removes_it_until_it_polls_again(store: AgentJobStore):
    await store.touch_runner_host(host="old-box", worker_id="w1")

    assert await store.forget_runner_host(host="old-box") == "deleted"
    assert await store.list_runner_hosts() == []
    assert await store.forget_runner_host(host="old-box") == "unknown"

    await store.touch_runner_host(host="old-box", worker_id="w1")
    assert [h["host"] for h in await store.list_runner_hosts()] == ["old-box"]


async def test_a_switch_in_flight_makes_a_claim_wait_rather_than_race_it(store: AgentJobStore):
    """Regression: the claim used to read the policy on its own connection.

    Old host reads "still me", the admin switches, the old host then dequeues —
    and the machine the operator just moved away from takes one more job while
    the page says the switch succeeded. The check now runs inside the claiming
    transaction and takes the policy row shared, so a switch that has not
    committed blocks the claim instead of being overtaken by it.
    """
    await store.touch_runner_host(host="runner-a", worker_id="w1")
    await store.touch_runner_host(host="runner-b", worker_id="w2")
    await store.set_active_runner_host(host="runner-a")
    await _create_job(store)

    async with store._pool.acquire() as conn, conn.transaction():
        # An admin switch that has taken the row but not yet committed.
        await conn.execute("UPDATE agent_runner_policy SET active_host = 'runner-b' WHERE id")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                store.claim_job(worker_id="w1", lease_ttl_seconds=60, host="runner-a"),
                timeout=1.0,
            )

    # Once it commits, the old host is out and the new one takes the job.
    assert await store.claim_job(worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None
    assert await store.claim_job(worker_id="w2", lease_ttl_seconds=60, host="runner-b") is not None


async def test_the_policy_row_is_a_singleton(store: AgentJobStore):
    """One fact in one place — the property the claim's lock depends on."""
    from asyncpg.exceptions import CheckViolationError

    async with store._pool.acquire() as conn:
        with pytest.raises(CheckViolationError):
            await conn.execute(
                "INSERT INTO agent_runner_policy (id, active_host) VALUES (FALSE, 'x')"
            )
        assert await conn.fetchval("SELECT count(*) FROM agent_runner_policy") == 1


async def test_removing_a_host_cannot_outrun_a_switch_onto_it(store: AgentJobStore):
    """Regression: the refusal used to be a read the switch could outrun.

    The endpoint saw the host idle, an admin pinned it, and the delete then
    removed the row the gate was pointing at — silently unpinning the
    deployment and handing every machine the queue back.
    """
    await store.touch_runner_host(host="runner-a", worker_id="w1")
    await store.touch_runner_host(host="runner-b", worker_id="w2")
    await store.set_active_runner_host(host="runner-a")

    async with store._pool.acquire() as conn, conn.transaction():
        # A switch onto runner-b that has taken the policy row, uncommitted.
        await conn.execute("UPDATE agent_runner_policy SET active_host = 'runner-b' WHERE id")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(store.forget_runner_host(host="runner-b"), timeout=1.0)

    # Once it commits, runner-b is the active host and removal is refused.
    assert await store.forget_runner_host(host="runner-b") == "active"
    assert "runner-b" in {h["host"] for h in await store.list_runner_hosts()}


async def test_pinning_a_host_cannot_outrun_its_removal(store: AgentJobStore):
    """The mirror case: pinning must not land on a row that is being deleted.

    A bare existence check lets the removal commit first, leaving the queue
    pinned to a machine that is gone — every job queues forever.
    """
    await store.touch_runner_host(host="runner-b", worker_id="w2")

    async with store._pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM agent_runner_hosts WHERE host = 'runner-b'")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(store.set_active_runner_host(host="runner-b"), timeout=1.0)

    assert await store.set_active_runner_host(host="runner-b") is False
    assert await store.active_runner_host() is None


async def test_forget_reports_which_of_the_three_things_happened(store: AgentJobStore):
    await store.touch_runner_host(host="runner-a", worker_id="w1")
    await store.touch_runner_host(host="old-box", worker_id="w3")
    await store.set_active_runner_host(host="runner-a")

    assert await store.forget_runner_host(host="runner-a") == "active"
    assert await store.forget_runner_host(host="old-box") == "deleted"
    assert await store.forget_runner_host(host="old-box") == "unknown"


async def test_the_pool_and_the_pinned_host_come_from_one_read(store: AgentJobStore):
    """The list and the header cannot name different hosts."""
    await store.touch_runner_host(host="runner-a", worker_id="w1")
    await store.touch_runner_host(host="runner-b", worker_id="w2")
    await store.set_active_runner_host(host="runner-b")

    hosts, active, _ = await store.runner_pool()

    assert active == "runner-b"
    assert [h["host"] for h in hosts if h["is_active"]] == ["runner-b"]


async def test_a_replayed_event_seq_is_answered_with_the_stored_row(store: AgentJobStore):
    """A retry after a dropped response duplicates neither event nor message."""
    await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    first = await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "the answer"},
        event_seq=1,
    )
    replay = await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "the answer"},
        event_seq=1,
    )
    second = await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="tool_use",
        payload={"name": "ls"},
        event_seq=2,
    )

    assert replay == first
    assert second is not None and second != first
    async with store._pool.acquire() as conn:
        events = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_job_events WHERE attempt_id = $1", claim["attempt_id"]
        )
        messages = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_thread_messages WHERE job_id = $1 AND role = 'assistant'",
            claim["id"],
        )
    assert events == 2
    assert messages == 1


async def test_a_resource_rollup_upserts_rather_than_accumulating(store: AgentJobStore):
    """A retried post is the same window said twice, not a second window.

    Idempotence here is a statement about ``ON CONFLICT``, which is exactly
    what a fake keyed by attempt cannot test: any dict-backed stand-in is
    idempotent by construction, so the assertion passes without the SQL being
    right.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    rollup = {
        "cpu_usec": 1_000,
        "mem_peak_bytes": 2_048,
        "net_rx_bytes": 4_096,
        "sample_count": 5,
        "coverage": 1.0,
        "sampler_version": "1",
    }

    assert await store.record_attempt_resources(
        attempt_id=claim["attempt_id"], lease_generation=claim["lease_generation"], rollup=rollup
    )
    assert await store.record_attempt_resources(
        attempt_id=claim["attempt_id"], lease_generation=claim["lease_generation"], rollup=rollup
    )

    telemetry = await store.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 1_000, "a retried post accumulated instead of replacing"
    assert telemetry["net_rx_bytes"] == 4_096


async def test_a_superseded_attempt_cannot_overwrite_the_live_one_s_numbers(
    store: AgentJobStore,
):
    """Fenced by the same predicate as every other worker write."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    await store.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"cpu_usec": 1_000, "sample_count": 5, "coverage": 1.0, "sampler_version": "1"},
    )

    stale = await store.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        rollup={"cpu_usec": 999_999, "sample_count": 1, "coverage": 1.0, "sampler_version": "1"},
    )

    assert stale is False
    telemetry = await store.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 1_000


async def test_an_unmeasured_window_is_stored_as_such_not_as_zero(store: AgentJobStore):
    """Null counters with coverage 0 — the row that says "not measured".

    Postgres is what makes this meaningful: the columns are nullable, so a
    turn nobody could measure reads back as NULL rather than as a zero the
    reader would have no way to question.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert await store.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"sample_count": 0, "coverage": 0.0, "sampler_version": "1"},
    )

    telemetry = await store.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] is None
    assert telemetry["coverage"] == 0.0
    assert telemetry["sampler_version"] == "1"


async def test_thread_events_span_every_turn_and_page(store: AgentJobStore):
    """A thread's complete log stays ordered and pageable across follow-ups."""
    first = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    for kind in ("tool_use", "tool_result", "message"):
        await store.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type=kind,
            payload={"marker": kind},
        )
    await store.transition(
        job_id=first["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    second = await store.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="and the docs"
    )
    assert second is not None

    events = await store.list_thread_events_after(job_id=second["id"], user_id="user-1")
    assert events is not None
    assert [event["event_type"] for event in events] == ["tool_use", "tool_result", "message"]
    assert all(event["turn_no"] == 1 for event in events)
    assert events[0]["payload"] == {"marker": "tool_use"}

    page = await store.list_thread_events_after(job_id=second["id"], user_id="user-1", limit=2)
    assert page is not None and len(page) == 2
    rest = await store.list_thread_events_after(
        job_id=second["id"], user_id="user-1", after_id=page[-1]["id"]
    )
    assert rest is not None and [event["event_type"] for event in rest] == ["message"]


async def test_thread_events_stop_at_the_owner(store: AgentJobStore):
    mine = await _create_job(store)

    assert await store.list_thread_events_after(job_id=mine["id"], user_id="user-2") is None
    assert await store.list_thread_events_after(job_id=mine["id"], user_id="user-1") == []
