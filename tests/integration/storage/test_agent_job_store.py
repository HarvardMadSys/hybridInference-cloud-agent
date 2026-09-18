"""Integration tests for Storage (Postgres): fencing, reaper, snapshots.

These pin the concurrency-ownership contract from issue #1041: attempts +
lease_generation fencing (zombie workers can never write after a reap),
append-only events with a global cursor, retained-group publication, and
fail-closed reaper semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from cloud_agent.mcp.registry import binding_from_row
from cloud_agent.schemas import WorkerEventRequest
from cloud_agent.storage.assembly import Storage
from cloud_agent.thread_stats import summarize_thread
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_SHARED_HOST_CAPABILITIES = [
    SHARED_SANDBOX_CAPABILITY,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    shared_deployment_capability("test-deployment"),
]


@pytest_asyncio.fixture
async def pool():
    """The connection pool these tests arrange and assert over directly.

    Separate from ``store`` because it is a separate thing: the assembly owns
    its pool and does not hand it out, so a test that needs raw SQL for its
    arrange or its assertion asks for the pool it created, rather than
    reaching past a repository into one.
    """
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

    # Schema and backfills come from the baseline migration now, not from
    # the storage layer. This is E1's acceptance: `alembic upgrade head` on a
    # fresh database has to leave every one of these tests passing.
    apply_migrations(db_config)

    async with pool.acquire() as conn:
        await reset_agent_tables(conn)

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        await pool.close()


@pytest_asyncio.fixture
async def store(pool):
    """The whole storage layer, assembled the way the service assembles it."""
    return Storage(pool)


async def _create_job(store: Storage, **overrides) -> dict:
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
    return await store.conversation_commands.create_job(**params)


async def test_thread_stats_keep_output_presence_when_omitting_long_transcripts(
    store: Storage,
):
    """Exercise the SQL projection that previously bypassed false-zero detection."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    for text in [" \n\t", "long answer " * 1000]:
        assert await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={
                "text": text,
                "usage_id": "msg_answer",
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        )
    inputs = await store.execution_queries.thread_stats_inputs(
        job_id=job["id"], user_id=job["user_id"]
    )
    assert inputs is not None
    assert [event["payload"]["has_text"] for event in inputs["events"]] == [False, True]
    assert all("text" not in event["payload"] for event in inputs["events"])
    stats = summarize_thread(
        job_ids=inputs["job_ids"], attempts=inputs["attempts"], events=inputs["events"]
    )
    assert stats.input_tokens == 100
    assert stats.output_tokens is None
    assert stats.output_tokens_per_second is None


async def test_bounded_event_stays_bounded_in_postgres_and_marks_the_assistant_message(
    pool,
    store: Storage,
):
    """Both event readers and the plain thread-message path see bounded content."""
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    event = WorkerEventRequest(event_type="message", payload={"text": '界😀\n"\\' * 100_000})
    event_id = await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type=event.event_type,
        payload=event.payload,
    )
    async with pool.acquire() as conn:
        size = await conn.fetchval(
            "SELECT octet_length(payload::text) FROM agent_job_events WHERE id = $1", event_id
        )
        content = await conn.fetchval(
            "SELECT content FROM agent_thread_messages WHERE source_event_id = $1", event_id
        )
    assert size <= 256 * 1024
    assert content.endswith("[Content truncated: event payload exceeded 256 KiB.]")


async def _settled(pool, store: Storage, job_id: str) -> None:
    """Stand in for the runner's settlement: a success carries its native ordinal.

    The runner settles the shared workspace before it reports success and the
    settlement assigns ``native_turn_no``; the schema refuses a succeeded row
    without one. Tests that skip the sandbox protocol and finish a job
    directly prepare that fact here, instead of the store guessing it.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET native_turn_no = turn_no "
            "WHERE id = $1 AND native_turn_no IS NULL",
            job_id,
        )


async def _expire_attempt(pool, store: Storage, attempt_id: int) -> None:
    """Force an attempt's lease into the past."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            attempt_id,
        )


def _shared_claim_attestation() -> dict[str, object]:
    """Return the exact process-local fence required for shared work."""
    return {
        "sandbox_image_digest": "sha256:shared-image",
        "shared_sandbox_protocol": SHARED_SANDBOX_PROTOCOL,
        "runner_capabilities": _SHARED_HOST_CAPABILITIES,
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
    }


_DEFAULT_CLAIM_VALUE = object()


async def _claim(
    store: Storage,
    *,
    worker_id: str,
    lease_ttl_seconds: float,
    host: str = "runner-a",
    host_credential_hash: str | object = _DEFAULT_CLAIM_VALUE,
    claim_attestation: dict[str, object] | object = _DEFAULT_CLAIM_VALUE,
) -> dict | None:
    """Claim through the enrolled shared-session contract used in production."""
    credential = (
        f"{host}-credential-hash"
        if host_credential_hash is _DEFAULT_CLAIM_VALUE
        else host_credential_hash
    )
    attestation = (
        _shared_claim_attestation()
        if claim_attestation is _DEFAULT_CLAIM_VALUE
        else claim_attestation
    )
    assert isinstance(credential, str)
    assert isinstance(attestation, dict)
    if await store.host_pool.get_host(host=host) is None:
        await store.host_pool.register_host(
            host=host,
            credential_hash=credential,
            capabilities=_SHARED_HOST_CAPABILITIES,
            slots_total=128,
        )
        await store.host_pool.record_host_heartbeat(
            host=host,
            capabilities=_SHARED_HOST_CAPABILITIES,
            sandbox_image_digest="sha256:shared-image",
        )
    return await store.attempt_commands.claim_job(
        worker_id=worker_id,
        lease_ttl_seconds=lease_ttl_seconds,
        host=host,
        host_credential_hash=credential,
        claim_attestation=attestation,
    )


async def _reserve_retained_chat(
    store: Storage,
) -> tuple[dict, dict, dict]:
    """Create a single-member shared group under a real database fence."""
    credential_hash = "runner-a-credential-hash"
    await store.host_pool.register_host(
        host="runner-a",
        credential_hash=credential_hash,
        capabilities=_SHARED_HOST_CAPABILITIES,
        slots_total=4,
    )
    online = await store.host_pool.record_host_heartbeat(
        host="runner-a",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )
    assert online is not None and online["status"] == "online"
    job = await _create_job(store)
    claim = await _claim(
        store,
        worker_id="w1",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=credential_hash,
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None
    sandbox = await store.sandbox.reserve_thread_sandbox(
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
    assert sandbox is not None and sandbox["shared_live"] is True
    return job, claim, sandbox


async def _reserve_shared_chat(
    store: Storage,
    *,
    metadata: dict | None = None,
    task_prompt: str = "fix the flaky test",
) -> tuple[dict, dict, dict]:
    """Create, claim, and reserve one capability-gated shared-live chat."""
    await store.host_pool.register_host(
        host="shared-runner",
        credential_hash="test-credential-hash",
        capabilities=_SHARED_HOST_CAPABILITIES,
        slots_total=4,
    )
    online = await store.host_pool.record_host_heartbeat(
        host="shared-runner",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )
    assert online is not None and online["status"] == "online"
    job = await _create_job(store, base_sha="a" * 40, metadata=metadata, task_prompt=task_prompt)
    claim = await _claim(
        store,
        worker_id="shared-worker",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None and claim["id"] == job["id"]
    sandbox = await store.sandbox.reserve_thread_sandbox(
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
    )
    assert sandbox is not None and sandbox["shared_live"] is True
    return job, claim, sandbox


async def test_shared_workspace_snapshot_publishes_without_rewriting_turn_state(
    pool,
    store: Storage,
):
    """A shared turn, snapshot, publication, and base import form one lineage."""
    job, claim, sandbox = await _reserve_shared_chat(
        store,
        metadata={"_agent_base_ref": "release/x"},
    )
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    running = await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    assert running is not None and running["shared_live"] is True
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    settled = await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-one",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert settled is not None
    assert settled["workspace_publication_state"] == "pending"
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    assert capture["sandbox_id"] == sandbox["sandbox_id"]
    assert capture["anchor_job_id"] == job["id"]
    assert capture["base_sha"] == job["base_sha"]
    patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    digest = hashlib.sha256(patch.encode()).hexdigest()
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
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

    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    assert publication["publication_kind"] == "shared_snapshot"
    assert publication["snapshot_id"] == capture["snapshot_id"]
    assert publication["included_job_ids"] == [job["id"]]
    assert publication["branch_id"] == job["thread_id"]
    assert publication["patch"] == patch
    assert publication["metadata"] == {"_agent_base_ref": "release/x"}
    # Publication is group bookkeeping, not another state of the model turn.
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "succeeded"

    commit_sha = "b" * 40
    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        pr_url="https://example.test/pull/1",
        commit_sha=commit_sha,
    )
    published_job = await store.conversation_queries.get_job(job["id"])
    assert published_job is not None
    assert published_job["state"] == "succeeded"
    assert published_job["published_commit_sha"] == commit_sha

    pending_import = await store.snapshot.pending_snapshot_base_import()
    assert pending_import is not None
    assert pending_import["snapshot_id"] == capture["snapshot_id"]
    assert pending_import["published_commit_sha"] == commit_sha
    assert await store.snapshot.complete_snapshot_base_import(
        snapshot_id=capture["snapshot_id"],
        commit_sha=commit_sha,
    )
    final_sandbox = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert final_sandbox is not None
    assert final_sandbox["workspace_publication_state"] == "published"
    async with pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT published_base_sha, snapshot_state FROM agent_sandboxes WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert group is not None
    assert group["published_base_sha"] == commit_sha
    assert group["snapshot_state"] == "quiescent"
    assert await store.snapshot.pending_snapshot_base_import() is None


async def test_idle_stopped_shared_workspace_snapshot_remains_claimable(
    pool,
    store: Storage,
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
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-stopped-snapshot",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-stopped-snapshot",
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
            sandbox["sandbox_id"],
        )

    capture = await store.snapshot.claim_due_sandbox_snapshot()

    assert capture is not None
    assert capture["sandbox_id"] == sandbox["sandbox_id"]
    async with pool.acquire() as conn:
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
        await store.snapshot.record_sandbox_snapshot_capture(
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
    durable = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["workspace_publication_state"] == "published"
    assert durable["idle_deadline"] is not None


async def test_stale_shared_snapshot_receipt_cannot_mutate_a_replacement_container(
    pool,
    store: Storage,
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
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-before-replacement",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-before-replacement",
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
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None

    async with pool.acquire() as conn:
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
        await store.snapshot.record_sandbox_snapshot_capture(
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
    assert not await store.snapshot.defer_sandbox_snapshot_capture(
        snapshot_id=capture["snapshot_id"],
        sandbox_id=capture["sandbox_id"],
        sandbox_generation=capture["generation"],
        container_id=capture["container_id"],
        detail="receipt lost its lifecycle fence",
        dirty=True,
    )
    async with pool.acquire() as conn:
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
    pool,
    store: Storage,
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
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-failure",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-failure",
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
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None

    if failure_mode == "capture_dirty":
        assert await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            detail="unsafe writer census",
            dirty=True,
        )
    elif failure_mode == "capture_stalled":
        assert await store.snapshot.reap_stalled_sandbox_snapshot_captures(stall_seconds=0) == [
            capture["snapshot_id"]
        ]
    else:
        patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
        assert await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        publication = await store.snapshot.claim_for_publish()
        assert publication is not None
        if failure_mode == "publish_failed":
            await store.snapshot.fail_snapshot_publish(
                snapshot_id=capture["snapshot_id"],
                detail="publisher rejected the immutable patch",
            )
        else:
            assert await store.snapshot.reap_stalled_snapshot_publishes(stall_seconds=0) == [
                capture["snapshot_id"]
            ]

    async with pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT snapshot_state, idle_deadline FROM agent_sandboxes WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert group is not None
    assert group["snapshot_state"] == expected_snapshot_state
    assert group["idle_deadline"] is not None


@pytest.mark.parametrize("next_action", ["follow_up", "fork", "capture", "publish", "base_import"])
async def test_reaped_fork_does_not_poison_the_surviving_member_manifest(
    pool, store: Storage, next_action: str
):
    """A failed hidden child remains history but cannot occupy the host manifest."""
    from cloud_agent_host.runner import _shared_group_manifest
    from cloud_agent_host.workspace_broker import SharedSandboxMemberRequest

    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-fork-recovery", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-source",
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
    operation = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"], user_id=job["user_id"], idempotency_key="stalled-fork"
    )
    assert operation is not None and operation.get("error") is None
    actions = await store.session_fork.reap_stalled_session_forks(stall_seconds=0)
    assert [action["id"] for action in actions] == [operation["id"]]
    async with pool.acquire() as conn:
        tombstone = await conn.fetchrow(
            "SELECT t.deleted_at, m.resident_slot_id FROM agent_threads t "
            "JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id WHERE t.id = $1",
            operation["target_thread_id"],
        )
    assert tombstone is not None and tombstone["deleted_at"] is not None
    assert tombstone["resident_slot_id"] is None

    if next_action == "fork":
        observed = await store.session_fork.prepare_session_fork(
            source_job_id=job["id"], user_id=job["user_id"], idempotency_key="replacement-fork"
        )
        assert observed is not None and observed.get("error") is None
        assert observed["target_resident_slot_id"] == operation["target_resident_slot_id"]
        expected_members = {job["thread_id"], observed["target_thread_id"]}
    elif next_action == "follow_up":
        follow = await store.conversation_commands.create_follow_up(
            parent_job_id=job["id"], user_id=job["user_id"], prompt="continue after failed Fork"
        )
        assert follow is not None and follow.get("error") is None
        follow_claim = await _claim(
            store,
            worker_id="follow-worker",
            lease_ttl_seconds=60,
            host="shared-runner",
            host_credential_hash="test-credential-hash",
        )
        assert follow_claim is not None and follow_claim["id"] == follow["id"]
        observed = await store.sandbox.reserve_thread_sandbox(
            thread_id=job["thread_id"],
            attempt_id=follow_claim["attempt_id"],
            lease_generation=follow_claim["lease_generation"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_name=sandbox["container_name"],
            runtime_id=sandbox["runtime_id"],
            runtime_version=sandbox["runtime_version"],
            sandbox_image_digest=sandbox["sandbox_image_digest"],
            execution_profile=sandbox["execution_profile"],
        )
        assert observed is not None
        expected_members = {job["thread_id"]}
    else:
        observed = await store.snapshot.claim_due_sandbox_snapshot()
        assert observed is not None
        if next_action in {"publish", "base_import"}:
            patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
            assert await store.snapshot.record_sandbox_snapshot_capture(
                snapshot_id=observed["snapshot_id"],
                sandbox_id=observed["sandbox_id"],
                sandbox_generation=observed["generation"],
                container_id=observed["container_id"],
                workspace_epoch=observed["workspace_epoch"],
                base_sha=observed["base_sha"],
                patch=patch,
                content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
            )
            observed = await store.snapshot.claim_for_publish()
            assert observed is not None
        if next_action == "base_import":
            assert await store.snapshot.record_snapshot_publish(
                snapshot_id=observed["snapshot_id"],
                pr_url="https://example.invalid/pull/1",
                commit_sha="b" * 40,
            )
            observed = await store.snapshot.pending_snapshot_base_import()
            assert observed is not None
        expected_members = {job["thread_id"]}
    # Exercise the strict runner projection and the host HTTP schema. Both
    # reject the NULL slot that an unfiltered tombstone used to contribute.
    manifest = (
        observed["members"]
        if next_action in {"capture", "publish", "base_import"}
        else _shared_group_manifest(observed, thread_id=job["thread_id"])
    )
    wire_members = [SharedSandboxMemberRequest.model_validate(member) for member in manifest]
    assert {member.thread_id for member in wire_members} == expected_members
    assert all(member.resident_slot_id is not None for member in wire_members)


async def test_native_fork_finalizes_two_concurrent_members_in_one_sandbox(
    pool,
    store: Storage,
):
    """The durable saga creates a second session alias, never a Docker clone."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    saved_skills = [
        {
            "id": 42,
            "name": "Review",
            "description": "Review changes",
            "revision": 3,
            "instructions": "Retain this exact version.",
        }
    ]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET skills = $2::jsonb WHERE id = $1",
            job["id"],
            json.dumps(saved_skills),
        )
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-source",
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

    # A newly deployed host may still retain a container created from the v1
    # image. Ordinary follow-ups remain valid, but its old helper must never
    # receive an at-anchor request it would silently interpret as "latest".
    before_upgrade = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert before_upgrade is not None and before_upgrade["native_fork_capable"] is True
    await store.host_pool.record_host_heartbeat(
        host="shared-runner",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:new-host-image",
    )
    after_upgrade = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert after_upgrade is not None and after_upgrade["native_fork_capable"] is False
    refused = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"],
        user_id=job["user_id"],
        idempotency_key="native-fork-old-container",
    )
    assert refused == {"error": "native_fork_unavailable"}
    await store.host_pool.record_host_heartbeat(
        host="shared-runner",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )

    operation = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"],
        user_id=job["user_id"],
        idempotency_key="native-fork-one",
    )
    assert operation is not None and operation.get("error") is None
    assert operation["source_native_turn_no"] == 1
    assert operation["state"] == "preparing"
    assert operation["sandbox_id"] == sandbox["sandbox_id"]
    assert operation["container_id"] == "container-shared-one"
    assert operation["target_thread_id"] != job["thread_id"]
    await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id=job["user_id"], archived=True
    )
    assert (await store.conversation_queries.list_sessions(user_id=job["user_id"], archived=True))[
        "sessions"
    ][0]["delete_block_reason"] == "fork_in_progress"
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[job["thread_id"]], user_id=job["user_id"]
    ) == {"error": "fork_in_progress"}
    await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id=job["user_id"], archived=False
    )
    # The child exists from preparation — its membership already owns a
    # resident slot and can carry a residency — but it has no turns, so the
    # owner cannot see it anywhere until finalization copies the history in.
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_jobs WHERE thread_id = $1",
                operation["target_thread_id"],
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT fork_operation_id FROM agent_thread_sandbox_memberships "
                "WHERE thread_id = $1",
                operation["target_thread_id"],
            )
            == operation["id"]
        )
    visible = await store.conversation_queries.list_conversations(
        user_id=job["user_id"], include_archived=True
    )
    assert operation["target_thread_id"] not in {
        item["job"]["thread_id"] for item in visible["conversations"]
    }

    # The fork transaction cleared the durable group idle deadline. A
    # heartbeat sampled just before that transaction may still report the
    # supervisor as locally quiescent; it must not re-arm a stop while the
    # native mutation is in flight.
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            thread_id=job["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-shared-one",
            sandbox_generation=sandbox["generation"],
            running=True,
            activity_active=False,
            idle_remaining_seconds=0,
            members=[],
        )
        == "running"
    )
    async with pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT active_operation_count, idle_deadline FROM agent_sandboxes WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert group is not None
    assert group["active_operation_count"] == 1
    assert group["idle_deadline"] is None
    assert (
        await store.sandbox.due_thread_sandbox_stop(
            thread_id=job["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-shared-one",
            sandbox_generation=sandbox["generation"],
        )
        is None
    )

    coordinator = "coordinator-one"
    assert await store.session_fork.claim_session_fork_materialization(
        operation_id=operation["id"],
        coordinator_token=coordinator,
        lease_seconds=210,
    )
    assert not await store.session_fork.claim_session_fork_materialization(
        operation_id=operation["id"],
        coordinator_token="competing-coordinator",
        lease_seconds=210,
    )
    residencies = await store.session_fork.establish_session_fork_residencies(
        operation_id=operation["id"],
        coordinator_token=coordinator,
        ttl_seconds=120,
    )
    assert residencies is not None
    assert await store.grants.link_residency_grant(
        thread_id=residencies["source"]["thread_id"],
        generation=residencies["source"]["generation"],
        grant_id="grant-source-first",
    )
    assert await store.grants.link_residency_grant(
        thread_id=residencies["target"]["thread_id"],
        generation=residencies["target"]["generation"],
        grant_id="grant-target-first",
    )
    # A heartbeat can have sampled STOPPED immediately before this fork
    # rotated the source residency. Its release must share the durable
    # operation fence or it can revoke the brand-new source process grant.
    assert (
        await store.residency.release_shared_residency_if_quiescent(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            process_generation=1,
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
        )
        is None
    )
    assert (
        await store.residency.heartbeat_residency(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            host="shared-runner",
            ttl_seconds=120,
        )
        == "grant-source-first"
    )
    retried_residencies = await store.session_fork.establish_session_fork_residencies(
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
    materialized = await store.session_fork.record_session_fork_materialized(
        operation_id=operation["id"],
        coordinator_token=coordinator,
        receipt=receipt,
    )
    assert materialized is not None and materialized["state"] == "materialized"
    # Queue finalization before an idempotent owner retry while holding the
    # operation lock. A retry that takes thread -> operation would deadlock
    # against finalization's operation -> thread once this lock is released.
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchrow(
            "SELECT id FROM agent_session_forks WHERE id = $1 FOR UPDATE", operation["id"]
        )
        finalization = asyncio.create_task(
            store.session_fork.finalize_session_fork(operation_id=operation["id"])
        )
        await asyncio.sleep(0.03)
        retry = asyncio.create_task(
            store.session_fork.prepare_session_fork(
                source_job_id=job["id"],
                user_id=job["user_id"],
                idempotency_key="native-fork-one",
            )
        )
        await asyncio.sleep(0.03)
        assert not finalization.done() and not retry.done()
    child, replayed = await asyncio.wait_for(asyncio.gather(finalization, retry), timeout=3)
    assert replayed is not None and replayed["state"] == "ready"
    assert child is not None
    assert child["thread_id"] == operation["target_thread_id"]
    assert child["fork_source_job_id"] == job["id"]
    assert child["title_source"] == "inherited"
    assert child["title"] == f"{job['title']} (fork)"
    assert child["execution_profile"] == job["execution_profile"]
    assert child["skills"] == [
        {key: value for key, value in skill.items() if key != "instructions"}
        for skill in saved_skills
    ]
    async with pool.acquire() as conn:
        assert (
            json.loads(
                await conn.fetchval("SELECT skills FROM agent_jobs WHERE id = $1", child["id"])
            )
            == saved_skills
        )
    child_sandbox = await store.sandbox.thread_sandbox(thread_id=child["thread_id"])
    source_sandbox = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert child_sandbox is not None and source_sandbox is not None
    assert child_sandbox["sandbox_id"] == source_sandbox["sandbox_id"] == sandbox["sandbox_id"]
    assert child_sandbox["container_id"] == source_sandbox["container_id"]
    assert child_sandbox["native_session_id"] == "native-child"
    assert source_sandbox["native_session_id"] == "native-source"
    assert child["native_turn_no"] == 1
    assert len(child_sandbox["group_members"]) == 2
    assert await store.session_fork.finalize_session_fork(operation_id=operation["id"]) == child

    # A STOPPED heartbeat can be sampled after the fork registered its new
    # residency generation but before the source process was restarted. By the
    # time it is committed, Fork is READY and the durable process generation is
    # newer. Neither reconciliation nor grant release may roll that process
    # back to stopped after the operation fence itself has cleared.
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            thread_id=job["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-shared-one",
            sandbox_generation=sandbox["generation"],
            running=False,
            activity_active=False,
            idle_remaining_seconds=None,
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
    source_after_stale_stop = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert source_after_stale_stop is not None
    assert source_after_stale_stop["process_generation"] == 2
    assert source_after_stale_stop["resident_state"] == "live"
    assert (
        await store.residency.release_shared_residency_if_quiescent(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            process_generation=1,
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
        )
        is None
    )
    assert (
        await store.residency.heartbeat_residency(
            thread_id=residencies["source"]["thread_id"],
            generation=residencies["source"]["generation"],
            host="shared-runner",
            ttl_seconds=120,
        )
        == ""
    )
    same_request = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"],
        user_id=job["user_id"],
        idempotency_key="native-fork-one",
    )
    assert same_request is not None and same_request["state"] == "ready"

    source_follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue source",
    )
    child_follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=child["id"],
        user_id=child["user_id"],
        prompt="continue child",
    )
    assert source_follow_up is not None and source_follow_up["state"] == "queued"
    assert source_follow_up["skills"] == child_follow_up["skills"] == saved_skills
    assert child_follow_up is not None and child_follow_up["state"] == "queued"
    first_claim = await _claim(
        store,
        worker_id="shared-worker-a",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )
    second_claim = await _claim(
        store,
        worker_id="shared-worker-b",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )
    assert first_claim is not None and second_claim is not None
    assert {first_claim["id"], second_claim["id"]} == {
        source_follow_up["id"],
        child_follow_up["id"],
    }


async def test_native_fork_ordinal_counts_settlement_even_if_job_finalization_failed(
    store: Storage,
):
    """Provider history and platform terminal state cannot drift by one turn."""
    first, first_claim, sandbox = await _reserve_shared_chat(store)
    first_fence = {
        "thread_id": first["thread_id"],
        "attempt_id": first_claim["attempt_id"],
        "lease_generation": first_claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **first_fence,
        container_id="container-native-ordinal",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**first_fence, job_id=first["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **first_fence,
        job_id=first["id"],
        context_status="live",
        native_session_id="native-ordinal-source",
        idle_timeout_seconds=1800,
        base_sha=first["base_sha"],
    )
    second = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"],
        user_id=first["user_id"],
        prompt="continue after a lost final response",
    )
    assert second is not None and second["state"] == "queued"
    # This is the finalization-only failure case: the native exchange is
    # already durable, even though the platform outcome is failed.
    assert await store.attempt_commands.transition(
        job_id=first["id"],
        attempt_id=first_claim["attempt_id"],
        lease_generation=first_claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="job finalization failed after native settlement",
    )
    assert (await store.conversation_queries.get_job(first["id"]))["native_turn_no"] == 1
    assert (await store.conversation_queries.get_job(second["id"]))["state"] == "queued"

    second_claim = await _claim(
        store,
        worker_id="shared-worker-second",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )
    assert second_claim is not None and second_claim["id"] == second["id"]
    activated = await store.sandbox.reserve_thread_sandbox(
        thread_id=first["thread_id"],
        attempt_id=second_claim["attempt_id"],
        lease_generation=second_claim["lease_generation"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        container_name=sandbox["container_name"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        sandbox_image_digest=sandbox["sandbox_image_digest"],
        execution_profile=sandbox["execution_profile"],
    )
    assert activated is not None
    second_fence = {
        "thread_id": first["thread_id"],
        "attempt_id": second_claim["attempt_id"],
        "lease_generation": second_claim["lease_generation"],
        "sandbox_generation": activated["generation"],
        "activation_generation": activated["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **second_fence,
        container_id="container-native-ordinal",
        context_status="restored",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**second_fence, job_id=second["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **second_fence,
        job_id=second["id"],
        context_status="restored",
        native_session_id="native-ordinal-source",
        idle_timeout_seconds=1800,
        base_sha=first["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=second["id"],
        attempt_id=second_claim["attempt_id"],
        lease_generation=second_claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (await store.conversation_queries.get_job(second["id"]))["native_turn_no"] == 2

    operation = await store.session_fork.prepare_session_fork(
        source_job_id=second["id"],
        user_id=second["user_id"],
        idempotency_key="native-fork-after-finalization-failure",
    )
    assert operation is not None and operation.get("error") is None
    assert operation["source_native_turn_no"] == 2


async def test_create_get_list_round_trip(store: Storage):
    """create -> get -> list returns the queued job with its fields."""
    job = await _create_job(store, metadata={"origin": "test"})
    assert job["state"] == "queued"
    assert job["id"].startswith("ajob_")

    fetched = await store.conversation_queries.get_job(job["id"])
    assert fetched is not None
    assert fetched["task_prompt"] == "fix the flaky test"
    assert fetched["metadata"] == {"origin": "test"}

    listed = await store.conversation_queries.list_jobs(user_id="user-1")
    assert [item["id"] for item in listed] == [job["id"]]
    assert await store.conversation_queries.get_job("ajob_missing") is None


async def test_execution_profile_persists_and_is_inherited_by_follow_up(pool, store, monkeypatch):
    trusted_default = await _create_job(store, task_prompt="trusted default")
    assert trusted_default["execution_profile"] == "trusted-v1"

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
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'cancelled' WHERE id = $1", source["id"])

    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=source["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert follow_up["execution_profile"] == "untrusted-kata-v1"
    assert (await store.conversation_queries.get_job(source["id"]))[
        "execution_profile"
    ] == "untrusted-kata-v1"


async def test_conversations_page_all_history_with_pins_and_equal_activity(pool, store: Storage):
    """Keyset pages cross the pinned boundary without missing tied threads."""
    jobs = [await _create_job(store, task_prompt=f"Historical task {i}") for i in range(61)]
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET created_at = $1, updated_at = $1", same_time)
        await conn.execute("UPDATE agent_threads SET created_at = $1, updated_at = $1", same_time)
        await conn.execute(
            "UPDATE agent_threads SET pinned_at = $1 WHERE id = ANY($2::text[])",
            same_time,
            [job["thread_id"] for job in jobs[:3]],
        )
    first = await store.conversation_queries.list_conversations(user_id="user-1")
    assert len(first["conversations"]) == 50
    assert first["next_cursor"]
    assert all(item["job"]["pinned_at"] for item in first["conversations"][:3])
    second = await store.conversation_queries.list_conversations(
        user_id="user-1", cursor=first["next_cursor"]
    )
    assert len(second["conversations"]) == 11
    assert second["next_cursor"] is None
    assert {item["job"]["id"] for item in first["conversations"] + second["conversations"]} == {
        job["id"] for job in jobs
    }
    seen = []
    cursor = None
    while True:
        page = await store.conversation_queries.list_conversations(
            user_id="user-1", limit=2, cursor=cursor
        )
        seen.extend(item["job"]["thread_id"] for item in page["conversations"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    expected = sorted((job["thread_id"] for job in jobs[:3]), reverse=True) + sorted(
        (job["thread_id"] for job in jobs[3:]), reverse=True
    )
    assert seen == expected


@pytest.mark.parametrize("pinned", [True, False, None])
async def test_conversations_filter_pins_before_paginating_owned_active_threads(
    store: Storage, pinned: bool | None
):
    """Each sidebar section pages its own rows without duplicates or omissions."""
    jobs = [await _create_job(store, task_prompt=f"Sidebar task {i}") for i in range(7)]
    for job in jobs[:3]:
        await store.conversation_commands.set_thread_pinned(
            job_id=job["id"], user_id="user-1", pinned=True
        )
    archived = await _create_job(store, task_prompt="Sidebar archived")
    await store.conversation_commands.set_thread_pinned(
        job_id=archived["id"], user_id="user-1", pinned=True
    )
    await store.conversation_commands.set_thread_archived(
        job_id=archived["id"], user_id="user-1", archived=True
    )
    foreign = await _create_job(store, user_id="user-2", task_prompt="Sidebar foreign")
    await store.conversation_commands.set_thread_pinned(
        job_id=foreign["id"], user_id="user-2", pinned=True
    )

    seen = []
    cursor = None
    while True:
        page = await store.conversation_queries.list_conversations(
            user_id="user-1", pinned=pinned, q="Sidebar", limit=2, cursor=cursor
        )
        seen.extend(item["job"]["id"] for item in page["conversations"])
        if pinned is not None:
            assert all(
                (item["job"]["pinned_at"] is not None) is pinned for item in page["conversations"]
            )
        cursor = page["next_cursor"]
        if cursor is None:
            break
    expected = jobs[:3] if pinned is True else jobs[3:] if pinned is False else jobs
    assert len(seen) == len(expected)
    assert set(seen) == {job["id"] for job in expected}

    with_archive = await store.conversation_queries.list_conversations(
        user_id="user-1", pinned=pinned, include_archived=True
    )
    expected_ids = {job["id"] for job in expected}
    if pinned is not False:
        expected_ids.add(archived["id"])
    assert {item["job"]["id"] for item in with_archive["conversations"]} == expected_ids


async def test_conversations_search_canonical_title_and_repo_with_literal_unicode(
    store: Storage,
):
    """Later turns and prompt body text cannot replace the durable task title."""
    original = await _create_job(
        store, task_prompt="修复 Café 100%_\\Bug\nprivate body-only keyword"
    )
    tip = original
    for _ in range(52):
        tip = await store.conversation_commands.create_follow_up(
            parent_job_id=tip["id"], user_id="user-1", prompt="later-turn-only keyword"
        )
        assert tip is not None and "error" not in tip
    await _create_job(store, task_prompt="Unrelated task")
    for query in ["修复", "CAFÉ", "100%_\\bug", "%", "_", "\\", "  修复  "]:
        result = await store.conversation_queries.list_conversations(user_id="user-1", q=query)
        assert len(result["conversations"]) == 1, query
        item = result["conversations"][0]
        assert item["title"] == "修复 Café 100%_\\Bug"
        assert item["job"]["id"] == tip["id"]
        assert item["job"]["turn_no"] == 53
    for query in ["private body-only", "later-turn-only", "missing"]:
        assert (await store.conversation_queries.list_conversations(user_id="user-1", q=query))[
            "conversations"
        ] == []
    by_repo = await store.conversation_queries.list_conversations(
        user_id="user-1", q="EXAMPLE-ORG/EXAMPLE"
    )
    assert len(by_repo["conversations"]) == 2
    assert (
        len(
            (await store.conversation_queries.list_conversations(user_id="user-1", q="  "))[
                "conversations"
            ]
        )
        == 2
    )


async def test_conversations_archive_repo_and_tenant_filters_are_independent(pool, store: Storage):
    """Including archives widens state visibility without crossing ownership."""
    active = await _create_job(store, task_prompt="shared search title")
    archived = await _create_job(store, task_prompt="shared search title")
    elsewhere = await _create_job(store, repo="other/project", task_prompt="shared search title")
    foreign = await _create_job(store, user_id="user-2", task_prompt="shared search title")
    await store.conversation_commands.set_thread_archived(
        job_id=archived["id"], user_id="user-1", archived=True
    )
    await store.conversation_commands.set_thread_archived(
        job_id=foreign["id"], user_id="user-2", archived=True
    )
    normal = await store.conversation_queries.list_conversations(user_id="user-1", q="shared")
    assert {item["job"]["id"] for item in normal["conversations"]} == {
        active["id"],
        elsewhere["id"],
    }
    both = await store.conversation_queries.list_conversations(
        user_id="user-1", q="shared", repo=active["repo"], include_archived=True
    )
    assert {item["job"]["id"]: item["archived"] for item in both["conversations"]} == {
        active["id"]: False,
        archived["id"]: True,
    }
    assert (
        await store.conversation_queries.list_conversations(user_id="nobody", include_archived=True)
    ) == {
        "conversations": [],
        "next_cursor": None,
    }
    assert (
        await store.conversation_queries.list_conversations(user_id="user-1", repo="missing/repo")
    )["conversations"] == []
    # A job cannot be re-owned apart from its thread, nor a thread apart from
    # its jobs: the composite owner keys refuse the malformed association the
    # list read used to have to defend against.
    import asyncpg

    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE agent_jobs SET user_id = 'user-2' WHERE id = $1", active["id"]
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE agent_threads SET user_id = 'user-2' WHERE id = $1",
                elsewhere["thread_id"],
            )
    everything = await store.conversation_queries.list_conversations(
        user_id="user-1", include_archived=True
    )
    assert {item["job"]["id"] for item in everything["conversations"]} == {
        active["id"],
        archived["id"],
        elsewhere["id"],
    }


async def test_conversations_cannot_hold_a_foreign_turn(pool, store: Storage):
    """A turn's owner is its thread's owner; the schema refuses any other row."""
    parent = await _create_job(store)
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="turn body"
    )
    assert child is not None
    import asyncpg

    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE agent_jobs SET user_id = 'user-2' WHERE id = $1", child["id"]
            )
    page = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in page["conversations"]] == [child["id"]]
    assert (await store.conversation_queries.list_conversations(user_id="user-2"))[
        "conversations"
    ] == []


@pytest.mark.parametrize("pinned", [None, True, False])
async def test_conversations_cursor_rejects_malformed_and_changed_search_scope(
    store: Storage,
    pinned: bool | None,
):
    """Cursor data never broadens owner, repo, archive, or text search scope."""
    import base64
    import json

    for _ in range(2):
        job = await _create_job(store)
        if pinned:
            await store.conversation_commands.set_thread_pinned(
                job_id=job["id"], user_id="user-1", pinned=True
            )
    page = await store.conversation_queries.list_conversations(
        user_id="user-1", limit=1, pinned=pinned
    )
    cursor = page["next_cursor"]
    assert cursor
    for changed in [
        {"user_id": "user-2"},
        {"q": "different"},
        {"repo": "different/repo"},
        {"include_archived": True},
    ] + [{"pinned": value} for value in [None, True, False] if value is not pinned]:
        with pytest.raises(ValueError, match="Invalid conversation cursor"):
            await store.conversation_queries.list_conversations(
                **{"user_id": "user-1", "cursor": cursor, "pinned": pinned, **changed}
            )
    data = json.loads(base64.urlsafe_b64decode(cursor))
    malformed = ["", "not-base64", "x" * 2049, base64.urlsafe_b64encode(b"[]").decode()]
    for field, value in [("v", 2), ("activity", "2026-01-01"), ("pin", 17), ("thread", "\x00")]:
        malformed.append(
            base64.urlsafe_b64encode(json.dumps({**data, field: value}).encode()).decode()
        )
    for invalid in malformed:
        with pytest.raises(ValueError, match="Invalid conversation cursor"):
            await store.conversation_queries.list_conversations(
                user_id="user-1", cursor=invalid, pinned=pinned
            )
    with pytest.raises(ValueError, match="Invalid conversation search"):
        await store.conversation_queries.list_conversations(user_id="user-1", q="\x00")


async def test_conversation_activity_counts_messages_and_transitions_without_lease_noise(
    pool,
    store: Storage,
):
    """Running work rises on meaningful changes without every heartbeat reordering it."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="activity-worker", lease_ttl_seconds=60)
    assert claim is not None
    before = (await store.conversation_queries.list_conversations(user_id="user-1"))[
        "conversations"
    ][0]
    assert await store.attempt_commands.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        lease_ttl_seconds=60,
    )
    heartbeat = (await store.conversation_queries.list_conversations(user_id="user-1"))[
        "conversations"
    ][0]
    assert heartbeat["last_activity_at"] == before["last_activity_at"]
    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "meaningful assistant reply"},
    )
    after = (await store.conversation_queries.list_conversations(user_id="user-1"))[
        "conversations"
    ][0]
    assert after["last_activity_at"] > before["last_activity_at"]
    assert after["job"]["id"] == job["id"]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', updated_at = NOW() WHERE id = $1", job["id"]
        )
    final = (await store.conversation_queries.list_conversations(user_id="user-1"))[
        "conversations"
    ][0]
    assert final["job"]["state"] == "failed"
    assert final["last_activity_at"] > after["last_activity_at"]


async def test_projects_sort_by_message_and_job_activity_preserving_pins(pool, store: Storage):
    """An older project's new reply or completion changes its recency."""
    older = await _create_job(store, repo="owner/older")
    newer = await _create_job(store, repo="owner/newer")
    dates = [datetime(2026, 1, day, tzinfo=timezone.utc) for day in range(1, 5)]
    async with pool.acquire() as conn:
        for job, timestamp in zip((older, newer), dates[:2], strict=True):
            await conn.execute(
                "UPDATE agent_jobs SET created_at = $2, updated_at = $2 WHERE id = $1",
                job["id"],
                timestamp,
            )
            await conn.execute(
                "UPDATE agent_threads SET created_at = $2, updated_at = $2 WHERE id = $1",
                job["thread_id"],
                timestamp,
            )
    assert [
        p["repo"] for p in await store.conversation_queries.list_projects(user_id="user-1")
    ] == [
        "owner/newer",
        "owner/older",
    ]
    # Assistant messages advance the thread timestamp, without a new job.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET updated_at = $2 WHERE id = $1", older["thread_id"], dates[2]
        )
    projects = await store.conversation_queries.list_projects(user_id="user-1")
    assert [p["repo"] for p in projects] == ["owner/older", "owner/newer"]
    assert projects[0]["last_activity_at"] == dates[2]
    # A completion advances the job timestamp, without another user turn.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET updated_at = $2, state = 'succeeded', native_turn_no = 1 "
            "WHERE id = $1",
            newer["id"],
            dates[3],
        )
    projects = await store.conversation_queries.list_projects(user_id="user-1")
    assert [p["repo"] for p in projects] == ["owner/newer", "owner/older"]
    conversations = await store.conversation_queries.list_conversations(user_id="user-1")
    assert (
        projects[0]["last_activity_at"]
        == conversations["conversations"][0]["last_activity_at"]
        == dates[3]
    )
    await store.conversation_commands.set_thread_pinned(
        job_id=older["id"], user_id="user-1", pinned=True
    )
    assert [
        p["repo"] for p in await store.conversation_queries.list_projects(user_id="user-1")
    ] == [
        "owner/older",
        "owner/newer",
    ]


async def test_projects_summarize_a_user_s_repos_and_page_one_at_a_time(store: Storage):
    """The sidebar's task tree: a folder per repo, and a page inside one."""
    first = await _create_job(store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="and the docs"
    )
    assert follow_up is not None
    other = await _create_job(store, repo="example-org/other-repo")
    await _create_job(store, user_id="user-2", repo="example-org/not-mine")

    projects = await store.conversation_queries.list_projects(user_id="user-1")

    by_repo = {project["repo"]: project for project in projects}
    assert set(by_repo) == {"example-org/example-repo", "example-org/other-repo"}
    # Two turns of one conversation are one task, and both are still queued.
    assert by_repo["example-org/example-repo"]["task_count"] == 1
    assert by_repo["example-org/example-repo"]["active_count"] == 2
    # Most recently active first.
    assert projects[0]["repo"] == "example-org/other-repo"

    paged = await store.conversation_queries.list_jobs(
        user_id="user-1", repo="example-org/other-repo"
    )
    assert [job["id"] for job in paged] == [other["id"]]
    # No filter still means every project.
    assert len(await store.conversation_queries.list_jobs(user_id="user-1")) == 3


async def test_archived_threads_leave_the_active_project_tree(store: Storage):
    """A repo whose only thread is archived is not an active folder."""
    job = await _create_job(store)
    await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id="user-1", archived=True
    )

    assert await store.conversation_queries.list_projects(user_id="user-1") == []
    archived = await store.conversation_queries.list_projects(user_id="user-1", archived=True)
    assert [project["repo"] for project in archived] == ["example-org/example-repo"]


async def test_archive_and_restore_filter_owned_threads_without_cancelling(
    store: Storage,
):
    """One owned job id archives every turn without changing run state."""
    parent = await _create_job(store)
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="also update the docs"
    )
    assert child is not None
    foreign = await _create_job(
        store,
        user_id="user-2",
        repo="example-org/other-repo",
        task_prompt="unrelated task",
    )
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    assert claim["id"] == parent["id"]

    archived = await store.conversation_commands.set_thread_archived(
        job_id=child["id"], user_id="user-1", archived=True
    )

    assert archived is not None
    assert archived["thread_id"] == parent["thread_id"]
    assert archived["archived_at"] is not None
    archived_again = await store.conversation_commands.set_thread_archived(
        job_id=parent["id"], user_id="user-1", archived=True
    )
    assert archived_again == archived
    archived_thread = await store.conversation_queries.get_thread_for_job(
        job_id=child["id"], user_id="user-1"
    )
    assert archived_thread["archived_at"] == archived["archived_at"]
    assert await store.conversation_queries.list_jobs(user_id="user-1") == []
    assert {
        job["id"]
        for job in await store.conversation_queries.list_jobs(user_id="user-1", archived=True)
    } == {
        parent["id"],
        child["id"],
    }
    live_parent = await store.conversation_queries.get_job(parent["id"])
    assert live_parent is not None
    assert live_parent["state"] == "running"
    assert live_parent["cancel_requested"] is False

    assert (
        await store.conversation_commands.set_thread_archived(
            job_id=foreign["id"], user_id="user-1", archived=True
        )
        is None
    )
    assert (
        await store.conversation_commands.set_thread_archived(
            job_id="ajob_missing", user_id="user-1", archived=True
        )
        is None
    )
    assert [job["id"] for job in await store.conversation_queries.list_jobs(user_id="user-2")] == [
        foreign["id"]
    ]

    restored = await store.conversation_commands.set_thread_archived(
        job_id=parent["id"], user_id="user-1", archived=False
    )

    assert restored == {"thread_id": parent["thread_id"], "archived_at": None}
    restored_thread = await store.conversation_queries.get_thread_for_job(
        job_id=parent["id"], user_id="user-1"
    )
    assert restored_thread["archived_at"] is None
    assert await store.conversation_queries.list_jobs(user_id="user-1", archived=True) == []
    assert {job["id"] for job in await store.conversation_queries.list_jobs(user_id="user-1")} == {
        parent["id"],
        child["id"],
    }


async def test_rename_persists_across_turns_and_search_without_changing_prompts(
    pool,
    store: Storage,
):
    """The canonical thread title survives reads, follow-ups, and owner isolation."""
    parent = await _create_job(store)
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="also update the docs"
    )
    foreign = await _create_job(store, user_id="user-2")
    title = "Renamed CSV conversation"
    result = await store.conversation_commands.set_thread_title(
        job_id=child["id"], user_id="user-1", title=title
    )
    assert result == {"thread_id": parent["thread_id"], "title": title}
    for original in (parent, child):
        job = await store.conversation_queries.get_job(original["id"])
        assert job["title"] == title
        assert job["task_prompt"] == original["task_prompt"]
        thread = await store.conversation_queries.get_thread_for_job(
            job_id=original["id"], user_id="user-1"
        )
        assert thread["title"] == title
        assert all(turn["title"] == title for turn in thread["jobs"])
    assert all(
        job["title"] == title
        for job in await store.conversation_queries.list_jobs(user_id="user-1")
    )
    conversations = await store.conversation_queries.list_conversations(
        user_id="user-1", q="Renamed CSV"
    )
    assert [item["title"] for item in conversations["conversations"]] == [title]
    next_turn = await store.conversation_commands.create_follow_up(
        parent_job_id=child["id"], user_id="user-1", prompt="one more change"
    )
    assert next_turn["title"] == title
    for job_id in (foreign["id"], "ajob_missing"):
        assert (
            await store.conversation_commands.set_thread_title(
                job_id=job_id, user_id="user-1", title="no"
            )
            is None
        )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", parent["thread_id"]
        )
    assert (
        await store.conversation_commands.set_thread_title(
            job_id=parent["id"], user_id="user-1", title="no"
        )
        is None
    )


async def test_pin_and_unpin_owned_threads_are_idempotent_and_reorder_lists(
    pool,
    store: Storage,
):
    """A pin covers every turn and its project, while foreign jobs remain private."""
    parent = await _create_job(store)
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="also update the docs"
    )
    assert child is not None
    newer = await _create_job(store, repo="example-org/newer-repo")
    foreign = await _create_job(store, user_id="user-2", repo="example-org/foreign")
    async with pool.acquire() as conn:
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

    pinned = await store.conversation_commands.set_thread_pinned(
        job_id=child["id"], user_id="user-1", pinned=True
    )

    assert pinned is not None
    assert pinned["thread_id"] == parent["thread_id"]
    assert pinned["pinned_at"] is not None
    assert (
        await store.conversation_commands.set_thread_pinned(
            job_id=parent["id"], user_id="user-1", pinned=True
        )
        == pinned
    )
    jobs = await store.conversation_queries.list_jobs(user_id="user-1")
    assert [job["id"] for job in jobs] == [child["id"], parent["id"], newer["id"]]
    assert jobs[0]["pinned_at"] == jobs[1]["pinned_at"] == pinned["pinned_at"]
    projects = await store.conversation_queries.list_projects(user_id="user-1")
    assert [project["repo"] for project in projects] == [
        "example-org/example-repo",
        "example-org/newer-repo",
    ]
    assert projects[0]["pinned_count"] == 1
    assert projects[0]["pinned_at"] == pinned["pinned_at"]
    assert (
        await store.conversation_commands.set_thread_pinned(
            job_id=foreign["id"], user_id="user-1", pinned=True
        )
        is None
    )
    assert (
        await store.conversation_commands.set_thread_pinned(
            job_id="ajob_missing", user_id="user-1", pinned=True
        )
        is None
    )

    unpinned = await store.conversation_commands.set_thread_pinned(
        job_id=parent["id"], user_id="user-1", pinned=False
    )

    assert unpinned == {"thread_id": parent["thread_id"], "pinned_at": None}
    assert [job["id"] for job in await store.conversation_queries.list_jobs(user_id="user-1")] == [
        newer["id"],
        child["id"],
        parent["id"],
    ]
    assert [
        project["repo"]
        for project in await store.conversation_queries.list_projects(user_id="user-1")
    ] == [
        "example-org/newer-repo",
        "example-org/example-repo",
    ]


async def test_concurrent_follow_ups_form_one_linear_thread(store: Storage):
    """The thread lock serializes simultaneous sends instead of forking."""
    parent = await _create_job(store)
    children = await asyncio.gather(
        store.conversation_commands.create_follow_up(
            parent_job_id=parent["id"], user_id="user-1", prompt="first concurrent turn"
        ),
        store.conversation_commands.create_follow_up(
            parent_job_id=parent["id"], user_id="user-1", prompt="second concurrent turn"
        ),
    )
    ordered = sorted(
        (child for child in children if child is not None), key=lambda job: job["turn_no"]
    )
    assert [job["turn_no"] for job in ordered] == [2, 3]
    assert ordered[0]["parent_job_id"] == parent["id"]
    assert ordered[1]["parent_job_id"] == ordered[0]["id"]
    assert [job["state"] for job in ordered] == ["queued", "queued"]


async def test_shared_queued_follow_up_runs_before_snapshot_publish(store: Storage):
    """A queued live turn never waits for the asynchronous group publisher."""
    parent, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": parent["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-waiting",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=parent["id"])
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id=parent["user_id"], prompt="already waiting"
    )
    assert child is not None and child["state"] == "queued"
    assert (
        await _claim(
            store,
            worker_id="other-worker",
            lease_ttl_seconds=60,
            host="shared-runner",
            host_credential_hash="test-credential-hash",
        )
        is None
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=parent["id"],
        context_status="live",
        native_session_id="native-shared-waiting",
        idle_timeout_seconds=1800,
        base_sha=parent["base_sha"],
    )
    # Settlement releases native activation, but the earlier job still owns
    # its attempt until its terminal transition commits.
    assert (
        await _claim(
            store,
            worker_id="other-worker",
            lease_ttl_seconds=60,
            host="shared-runner",
            host_credential_hash="test-credential-hash",
        )
        is None
    )
    assert await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    released = await store.conversation_queries.get_job(child["id"])
    assert released is not None
    assert released["state"] == "queued"
    next_claim = await _claim(
        store,
        worker_id="other-worker",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
    )
    assert next_claim is not None and next_claim["id"] == child["id"]


async def test_concurrent_claims_keep_queued_turns_in_order(store: Storage):
    """SKIP LOCKED must never turn the next row in one chat into parallel work."""
    parent = await _create_job(store)
    first = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="second turn"
    )
    second = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="third turn"
    )
    other = await _create_job(store, task_prompt="an independent chat")
    assert first is not None and second is not None
    # Enroll before racing claims, so host registration is not the race under test.
    await store.host_pool.register_host(
        host="runner-a",
        credential_hash="runner-a-credential-hash",
        slots_total=128,
        capabilities=_SHARED_HOST_CAPABILITIES,
    )
    await store.host_pool.record_host_heartbeat(
        host="runner-a",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )

    async def race_claims() -> list[dict]:
        results = await asyncio.gather(
            *(
                _claim(store, worker_id=f"worker-{number}", lease_ttl_seconds=60)
                for number in range(8)
            )
        )
        return [result for result in results if result is not None]

    claims = await race_claims()
    assert {claim["id"] for claim in claims} == {parent["id"], other["id"]}
    assert len(claims) == 2
    assert (await store.host_pool.get_host(host="runner-a"))["slots_used"] == 2
    parent_claim = next(claim for claim in claims if claim["id"] == parent["id"])
    assert await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="failed before sandbox admission",
    )
    claims = await race_claims()
    assert len(claims) == 1 and claims[0]["id"] == first["id"]
    assert await store.attempt_commands.transition(
        job_id=first["id"],
        attempt_id=claims[0]["attempt_id"],
        lease_generation=claims[0]["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="failed before sandbox admission",
    )
    claims = await race_claims()
    assert len(claims) == 1 and claims[0]["id"] == second["id"]
    assert (await store.host_pool.get_host(host="runner-a"))["slots_used"] == 2


@pytest.mark.parametrize("parent_already_running", [False, True])
async def test_cancelled_intermediate_turns_do_not_bypass_unfinished_ancestor(
    store: Storage,
    parent_already_running: bool,
):
    """A terminal direct parent does not imply all previous turns are finished."""
    parent = await _create_job(store)
    parent_claim = (
        await _claim(store, worker_id="parent-worker", lease_ttl_seconds=60)
        if parent_already_running
        else None
    )
    children = []
    for number in range(3):
        child = await store.conversation_commands.create_follow_up(
            parent_job_id=parent["id"], user_id="user-1", prompt=f"follow-up {number}"
        )
        assert child is not None and child["state"] == "queued"
        children.append(child)
    for child in children[:2]:
        assert (
            await store.attempt_commands.request_cancel(job_id=child["id"], user_id="user-1")
            == "cancelled"
        )
    other = await _create_job(store, task_prompt="runnable behind blocked follow-ups")
    if parent_claim is None:
        parent_claim = await _claim(store, worker_id="parent-worker", lease_ttl_seconds=60)
    assert parent_claim is not None and parent_claim["id"] == parent["id"]
    other_claim = await _claim(store, worker_id="other-worker", lease_ttl_seconds=60)
    assert other_claim is not None and other_claim["id"] == other["id"]
    assert await _claim(store, worker_id="third-worker", lease_ttl_seconds=60) is None
    assert (await store.conversation_queries.get_job(children[-1]["id"]))["state"] == "queued"

    assert await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="failed before sandbox admission",
    )
    next_claim = await _claim(store, worker_id="third-worker", lease_ttl_seconds=60)
    assert next_claim is not None and next_claim["id"] == children[-1]["id"]


async def test_settlement_and_concurrent_follow_up_use_one_lock_order(pool, store: Storage):
    parent, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": parent["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-lock-order",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=parent["id"])

    async with pool.acquire() as conn:
        await conn.execute(
            "DROP TRIGGER IF EXISTS test_delay_membership_update "
            "ON agent_thread_sandbox_memberships"
        )
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION test_delay_membership_update()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_sleep(0.25);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER test_delay_membership_update
            BEFORE UPDATE ON agent_thread_sandbox_memberships
            FOR EACH ROW EXECUTE FUNCTION test_delay_membership_update()
            """
        )
    try:
        settlement = asyncio.create_task(
            store.sandbox.settle_thread_sandbox(
                **fence,
                job_id=parent["id"],
                context_status="live",
                native_session_id="native-lock-order",
                idle_timeout_seconds=1800,
                base_sha=parent["base_sha"],
            )
        )
        await asyncio.sleep(0.05)
        follow_up = asyncio.create_task(
            store.conversation_commands.create_follow_up(
                parent_job_id=parent["id"],
                user_id=parent["user_id"],
                prompt="send while settlement holds its locks",
            )
        )
        settled, child = await asyncio.wait_for(
            asyncio.gather(settlement, follow_up),
            timeout=3,
        )
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS test_delay_membership_update "
                "ON agent_thread_sandbox_memberships"
            )
            await conn.execute("DROP FUNCTION IF EXISTS test_delay_membership_update()")

    assert settled is not None
    assert child is not None and child["state"] == "queued"


async def test_settlement_and_host_reconciliation_use_one_lock_order(pool, store: Storage):
    parent, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": parent["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-reconcile-lock-order",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=parent["id"])

    async with pool.acquire() as conn:
        await conn.execute(
            "DROP TRIGGER IF EXISTS test_delay_membership_update "
            "ON agent_thread_sandbox_memberships"
        )
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION test_delay_membership_update()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_sleep(0.25);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER test_delay_membership_update
            BEFORE UPDATE ON agent_thread_sandbox_memberships
            FOR EACH ROW EXECUTE FUNCTION test_delay_membership_update()
            """
        )
    try:
        settlement = asyncio.create_task(
            store.sandbox.settle_thread_sandbox(
                **fence,
                job_id=parent["id"],
                context_status="live",
                native_session_id="native-reconcile-lock-order",
                idle_timeout_seconds=1800,
                base_sha=parent["base_sha"],
            )
        )
        await asyncio.sleep(0.05)
        reconciliation = asyncio.create_task(
            store.sandbox.reconcile_thread_sandbox_observation(
                thread_id=parent["thread_id"],
                host="shared-runner",
                sandbox_id=sandbox["sandbox_id"],
                container_id="container-reconcile-lock-order",
                sandbox_generation=sandbox["generation"],
                running=True,
                activity_active=True,
                members=[
                    {
                        "thread_id": parent["thread_id"],
                        "activation_generation": sandbox["activation_generation"],
                        "process_generation": 1,
                        "residency_generation": 0,
                        "resident_state": "live",
                    }
                ],
            )
        )
        settled, reconciled = await asyncio.wait_for(
            asyncio.gather(settlement, reconciliation),
            timeout=3,
        )
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS test_delay_membership_update "
                "ON agent_thread_sandbox_memberships"
            )
            await conn.execute("DROP FUNCTION IF EXISTS test_delay_membership_update()")

    assert settled is not None
    assert reconciled == "running"


async def test_stopped_reconciliation_locks_even_unreported_members_first(
    pool,
    store: Storage,
):
    parent, _, sandbox = await _reserve_shared_chat(store)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET active_attempt_id = NULL, admitted_job_id = NULL, admitted_at = NULL
            WHERE thread_id = $1
            """,
            parent["thread_id"],
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopping' WHERE id = $1",
            sandbox["sandbox_id"],
        )
        await conn.execute(
            "DROP TRIGGER IF EXISTS test_delay_membership_update "
            "ON agent_thread_sandbox_memberships"
        )
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION test_delay_membership_update()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_sleep(0.25);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER test_delay_membership_update
            BEFORE UPDATE ON agent_thread_sandbox_memberships
            FOR EACH ROW EXECUTE FUNCTION test_delay_membership_update()
            """
        )

    async def membership_then_group() -> None:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_thread_sandbox_memberships "
                "SET updated_at = NOW() WHERE thread_id = $1",
                parent["thread_id"],
            )
            await conn.execute(
                "UPDATE agent_sandboxes SET updated_at = NOW() WHERE id = $1",
                sandbox["sandbox_id"],
            )

    try:
        lifecycle = asyncio.create_task(membership_then_group())
        await asyncio.sleep(0.05)
        reconciliation = asyncio.create_task(
            store.sandbox.reconcile_thread_sandbox_observation(
                thread_id=parent["thread_id"],
                host="shared-runner",
                sandbox_id=sandbox["sandbox_id"],
                container_id="container-stopped-lock-order",
                sandbox_generation=sandbox["generation"],
                running=False,
                members=[],
            )
        )
        _, reconciled = await asyncio.wait_for(
            asyncio.gather(lifecycle, reconciliation),
            timeout=3,
        )
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS test_delay_membership_update "
                "ON agent_thread_sandbox_memberships"
            )
            await conn.execute("DROP FUNCTION IF EXISTS test_delay_membership_update()")

    assert reconciled == "stopped"


async def test_claim_creates_fenced_attempt(store: Storage):
    """Claiming moves the job to running with attempt_no = generation = 1."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    assert claim["id"] == job["id"]
    assert claim["state"] == "running"
    assert claim["attempt_no"] == 1
    assert claim["lease_generation"] == 1

    # The job is no longer queued, so a second claim finds nothing.
    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60) is None


async def test_heartbeat_extends_and_fences(store: Storage):
    """Heartbeat succeeds with the right generation and fences the wrong one."""
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    beat = await store.attempt_commands.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        lease_ttl_seconds=60,
    )
    assert beat["ok"] is True
    assert beat["cancel_requested"] is False

    stale = await store.attempt_commands.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        lease_ttl_seconds=60,
    )
    assert stale["ok"] is False


async def test_event_seq_and_global_cursor(store: Storage):
    """Events get per-attempt seq 1..n and a strictly increasing global id."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    ids = []
    for index in range(3):
        event_id = await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"index": index},
        )
        assert event_id is not None
        ids.append(event_id)
    assert ids == sorted(ids)

    events = await store.execution_queries.list_events_after(job_id=job["id"], after_id=0)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["payload"]["index"] for event in events] == [0, 1, 2]

    tail = await store.execution_queries.list_events_after(job_id=job["id"], after_id=ids[1])
    assert [event["id"] for event in tail] == [ids[2]]


async def test_retained_workspace_readiness_is_a_durable_thread_wide_fence(
    store: Storage,
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

    assert await store.sandbox.thread_workspace_ready(thread_id=job["thread_id"]) is False
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-retained-one",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.thread_workspace_ready(thread_id=job["thread_id"]) is False

    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="lifecycle",
        payload={"phase": "workspace_ready"},
    )
    assert await store.sandbox.thread_workspace_ready(thread_id=job["thread_id"]) is True
    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="lifecycle",
        payload={"phase": "workspace_finalizing"},
    )
    assert await store.sandbox.thread_workspace_ready(thread_id=job["thread_id"]) is False

    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="new",
        native_session_id="native-session-one",
        idle_timeout_seconds=1800,
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert await store.sandbox.thread_workspace_ready(thread_id=job["thread_id"]) is True


async def test_zombie_worker_is_fenced_out_after_reap(pool, store: Storage):
    """After a reap, every write path of the old attempt is rejected."""
    job = await _create_job(store)
    old = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert (
        await store.attempt_commands.append_event(
            attempt_id=old["attempt_id"],
            lease_generation=old["lease_generation"],
            event_type="message",
            payload={"n": 1},
        )
        is not None
    )

    await _expire_attempt(pool, store, old["attempt_id"])
    actions = await store.attempt_commands.reap_expired(max_attempts=3)
    assert actions == [{"job_id": job["id"], "attempt_id": old["attempt_id"], "action": "queued"}]

    # The supersede is announced as an append-only control event.
    events = await store.execution_queries.list_events_after(job_id=job["id"], after_id=0)
    assert events[-1]["event_type"] == "attempt_superseded"
    assert events[-1]["payload"]["reason"] == "lease_expired"

    # A new worker claims attempt 2 / generation 2.
    new = await _claim(store, worker_id="w2", lease_ttl_seconds=60)
    assert new is not None
    assert new["attempt_no"] == 2
    assert new["lease_generation"] == 2

    # The zombie's event, heartbeat, and terminal writes are fenced out.
    zombie_kwargs = {
        "attempt_id": old["attempt_id"],
        "lease_generation": old["lease_generation"],
    }
    assert (
        await store.attempt_commands.append_event(**zombie_kwargs, event_type="message", payload={})
        is None
    )
    assert (await store.attempt_commands.heartbeat(**zombie_kwargs, lease_ttl_seconds=60))[
        "ok"
    ] is False
    assert (
        await store.attempt_commands.transition(
            job_id=job["id"],
            **zombie_kwargs,
            from_states=("running",),
            to_state="succeeded",
        )
        is False
    )
    # The live attempt keeps working.
    assert (
        await store.attempt_commands.append_event(
            attempt_id=new["attempt_id"],
            lease_generation=new["lease_generation"],
            event_type="message",
            payload={"n": 2},
        )
        is not None
    )


async def test_settlement_ack_replay_preserves_snapshot_epoch_and_rejects_stale_fences(
    pool,
    store: Storage,
):
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-retained-one",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    request = dict(
        **fence,
        job_id=job["id"],
        context_status="new",
        native_session_id="claude-session-one",
        idle_timeout_seconds=1800,
    )
    original = await store.sandbox.settle_thread_sandbox(**request)
    assert original is not None

    async def snapshot_state():
        async with pool.acquire() as conn:
            return (
                await conn.fetchrow(
                    "SELECT workspace_epoch, idle_deadline FROM agent_sandboxes WHERE id = $1",
                    sandbox["sandbox_id"],
                ),
                await conn.fetch(
                    "SELECT * FROM agent_sandbox_snapshots WHERE sandbox_id = $1 ORDER BY id",
                    sandbox["sandbox_id"],
                ),
                await conn.fetchval(
                    "SELECT native_turn_no FROM agent_jobs WHERE id = $1", job["id"]
                ),
            )

    before = await snapshot_state()
    replay = await store.sandbox.settle_thread_sandbox(**request)
    assert replay is not None
    assert replay["native_session_id"] == original["native_session_id"]
    assert await snapshot_state() == before
    # A retry that cannot restate the locator still replays the recorded one;
    # the attempt, lease and activation fences already pin the boundary.
    locator_free = await store.sandbox.settle_thread_sandbox(
        **(request | {"native_session_id": None})
    )
    assert locator_free is not None
    assert locator_free["native_session_id"] == original["native_session_id"]
    assert await snapshot_state() == before
    for change in [
        {"activation_generation": fence["activation_generation"] + 1},
        {"lease_generation": fence["lease_generation"] + 1},
        {"native_session_id": "a-different-session"},
        {"context_status": "restored"},
    ]:
        assert await store.sandbox.settle_thread_sandbox(**(request | change)) is None
    await _expire_attempt(pool, store, claim["attempt_id"])
    assert await store.sandbox.settle_thread_sandbox(**request) is None
    assert await snapshot_state() == before


async def test_retained_admission_settlement_and_job_finish_share_one_boundary(
    pool,
    store: Storage,
):
    job, claim, sandbox = await _reserve_retained_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    running = await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-retained-one",
        context_status="new",
    )
    assert running is not None
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    settled = await store.sandbox.settle_thread_sandbox(
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
        "running": True,
    }
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=True,
    )
    assert (await store.sandbox.thread_sandbox(thread_id=job["thread_id"]))["idle_deadline"] is None
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=False,
        idle_remaining_seconds=120,
    )
    rearmed = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert rearmed["idle_deadline"] > datetime.now(timezone.utc)

    # A broker can restart between turns, reconstruct the immutable container
    # identity from Docker, and know no activation generation. A terminal that
    # wakes the stopped container must still promote the durable row and own
    # its idle clock, but only while no worker attempt owns it.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET idle_deadline = NOW() - INTERVAL '1 second' WHERE id = $1",
            sandbox["sandbox_id"],
        )
    observation["running"] = False
    assert await store.sandbox.reconcile_thread_sandbox_observation(**observation) == "stopped"
    observation["running"] = True
    promoted = await store.sandbox.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=True,
    )
    assert promoted == "running"
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=False,
        idle_remaining_seconds=120,
    )
    rearmed_after_restart = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert rearmed_after_restart["state"] == "running"
    assert rearmed_after_restart["idle_deadline"] > datetime.now(timezone.utc)

    # When a replacement broker has neither activity tokens nor a monotonic
    # deadline, it reports activity as unknown. The control plane grants one
    # recovery window only; repeated healthy heartbeats must not keep an idle
    # container alive forever by moving that deadline forward.
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=True,
    )
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=None,
        idle_remaining_seconds=None,
    )
    first_recovery = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert first_recovery["idle_deadline"] is not None
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation,
        activity_active=None,
        idle_remaining_seconds=None,
    )
    second_recovery = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert second_recovery["idle_deadline"] == first_recovery["idle_deadline"]

    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    durable = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["active_attempt_id"] is None
    assert durable["admitted_job_id"] is None
    assert durable["last_settled_job_id"] == job["id"]
    assert durable["native_session_id"] == "claude-session-one"


async def test_retained_container_identity_cannot_be_replaced_or_reimaged(
    store: Storage,
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
        await store.sandbox.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )
    assert (
        await store.sandbox.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-replacement",
            context_status="new",
        )
        is None
    )
    with pytest.raises(ValueError, match="immutable identity"):
        await store.sandbox.reserve_thread_sandbox(
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


async def test_retained_follow_up_keeps_owning_host_image_and_cli_pin_after_pool_switch(
    store: Storage,
):
    job = await _create_job(store)
    original = await _claim(
        store,
        worker_id="w1",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation={
            **_shared_claim_attestation(),
            "sandbox_runtime": "runc",
            "sandbox_image_digest": "sha256:original-image",
            "runtime_versions_available": {"claude-code": ["2.1.220"]},
        },
    )
    assert original is not None
    sandbox = await store.sandbox.reserve_thread_sandbox(
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
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-pinned",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="new",
        native_session_id="session-pinned",
        idle_timeout_seconds=1800,
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=original["attempt_id"],
        lease_generation=original["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    # Enrol the destination while the queue is empty, then move new-chat
    # placement there. The existing group cannot move with that policy bit: its
    # only workspace and native state are still in runner-a's Docker layer.
    assert (
        await _claim(
            store,
            worker_id="enrol-runner-b",
            lease_ttl_seconds=60,
            host="runner-b",
        )
        is None
    )
    assert await store.host_pool.set_active_runner_host(host="runner-b")
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue",
    )
    assert child is not None and child["state"] == "queued"

    _, active_host, _, retained_hosts = await store.host_pool.deployment_runner_pool()
    assert active_host == "runner-b"
    assert "runner-a" in retained_hosts

    assert (
        await _claim(
            store,
            worker_id="wrong-host",
            lease_ttl_seconds=60,
            host="runner-b",
        )
        is None
    )

    follow_up = await _claim(
        store,
        worker_id="w2",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation={
            **_shared_claim_attestation(),
            # v2 added historical Fork only. An old owner may continue the
            # exact retained group it already holds after placement moves.
            "shared_sandbox_protocol": "v1",
            "sandbox_runtime": "runc",
            "sandbox_image_digest": "sha256:new-runner-image",
            "runtime_versions_available": {"claude-code": ["2.1.220", "2.1.999"]},
        },
    )

    assert follow_up is not None
    assert follow_up["runtime_version"] == "2.1.220"
    assert follow_up["sandbox_image_digest"] == "sha256:original-image"
    assert follow_up["thread_sandbox"]["container_id"] == "container-pinned"

    fresh = await _create_job(store, task_prompt="new chat after the host switch")
    assert (
        await _claim(
            store,
            worker_id="old-host-new-chat",
            lease_ttl_seconds=60,
            host="runner-a",
        )
        is None
    )
    new_claim = await _claim(
        store,
        worker_id="selected-host-new-chat",
        lease_ttl_seconds=60,
        host="runner-b",
    )
    assert new_claim is not None and new_claim["id"] == fresh["id"]


async def test_compatible_v1_runner_cannot_materialize_a_new_group(store: Storage):
    fresh = await _create_job(store)
    v1_attestation = {
        **_shared_claim_attestation(),
        "shared_sandbox_protocol": "v1",
    }

    claim = await _claim(
        store,
        worker_id="v1-runner",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=v1_attestation,
    )

    assert claim is None
    assert (await store.conversation_queries.get_job(fresh["id"]))["state"] == "queued"


async def test_failed_preadmission_activation_requests_an_immediate_stop(
    store: Storage,
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
        await store.sandbox.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )

    abandoned = await store.sandbox.abandon_thread_sandbox(
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
    store: Storage,
):
    job, claim, sandbox = await _reserve_retained_chat(store)

    abandoned = await store.sandbox.abandon_thread_sandbox(
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
    store: Storage,
):
    """A missing native locator fails closed before any new prompt is sent."""
    job, claim, sandbox = await _reserve_retained_chat(store)
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="do not leave this turn waiting forever",
    )
    assert child is not None and child["state"] == "queued"

    abandoned = await store.sandbox.abandon_thread_sandbox(
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
    assert abandoned["state"] == "lost"
    assert abandoned["context_status"] == "lost"
    queued = await store.conversation_queries.get_job(child["id"])
    assert queued["state"] == "failed"
    assert "lost the retained sandbox context" in queued["detail"]
    assert await store.conversation_commands.create_follow_up(
        parent_job_id=child["id"],
        user_id=job["user_id"],
        prompt="still must not be accepted",
    ) == {"error": "sandbox_context_lost"}


async def test_failed_admitted_turn_fails_queued_follow_up(
    store: Storage,
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
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-retained-one",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="this cannot run after an ambiguous turn",
    )
    assert child is not None and child["state"] == "queued"

    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="agent exited after prompt admission",
    )

    durable = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert durable["state"] == "stopping"
    assert durable["context_status"] == "lost"
    assert durable["idle_deadline"] is None
    blocked = await store.conversation_queries.get_job(child["id"])
    assert blocked["state"] == "failed"
    assert "lost the retained sandbox context" in blocked["detail"]


async def test_failed_preadmission_late_container_observation_is_stopped(
    store: Storage,
):
    """A Docker create that outran /running cannot become an immortal orphan."""
    job, claim, sandbox = await _reserve_retained_chat(store)
    abandoned = await store.sandbox.abandon_thread_sandbox(
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

    recovered = await store.sandbox.reconcile_thread_sandbox_observation(
        thread_id=job["thread_id"],
        host="runner-a",
        sandbox_id="asbx_test",
        container_id="container-created-before-response-loss",
        sandbox_generation=sandbox["generation"],
        running=True,
        # Model the supervisor lease whose acquire response never reached the
        # runner. Durable ownership is already gone, so it must not block stop.
        activity_active=True,
    )

    assert recovered == "stopping"
    durable = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["container_id"] == "container-created-before-response-loss"
    assert durable["state"] == "stopping"
    due = await store.sandbox.due_thread_sandbox_stop(
        thread_id=job["thread_id"],
        host="runner-a",
        sandbox_id="asbx_test",
        container_id="container-created-before-response-loss",
        sandbox_generation=sandbox["generation"],
    )
    assert due is not None
    assert due["reason"] == "orphaned_attempt"
    assert due["activation_generation"] == 0


async def test_expired_shared_admission_loses_only_that_member_and_residency(
    pool,
    store: Storage,
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
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    residency = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        runtime_id="claude-code",
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
    )
    assert residency is not None
    assert await store.sandbox.record_shared_thread_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_id=sandbox["sandbox_id"],
        residency_generation=residency["generation"],
    )
    assert await store.grants.link_residency_grant(
        thread_id=job["thread_id"],
        generation=residency["generation"],
        grant_id="grant-shared-expired",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="must not run after an ambiguous shared turn",
    )
    assert child is not None and child["state"] == "queued"

    sibling = await _create_job(store, task_prompt="independent fork sibling")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_thread_sandbox_memberships (
                thread_id, user_id, sandbox_id, native_session_id, native_state,
                context_status, process_generation, resident_state,
                resident_slot_id
            ) VALUES ($1, (SELECT user_id FROM agent_threads WHERE id = $1), $2, 'native-sibling', 'ready', 'live', 7, 'live', 1)
            """,
            sibling["thread_id"],
            sandbox["sandbox_id"],
        )
    await _expire_attempt(pool, store, claim["attempt_id"])

    actions = await store.attempt_commands.reap_expired(max_attempts=3)

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
    failed = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    surviving = await store.sandbox.thread_sandbox(thread_id=sibling["thread_id"])
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
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "failed"
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT residency_status FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
                job["thread_id"],
            )
            == "none"
        )

    # A late host census can report the orphan process, but LOST is durable
    # and may not be promoted back to LIVE by observation alone.
    await store.sandbox.reconcile_thread_sandbox_observation(
        thread_id=job["thread_id"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        container_id="container-shared-one",
        sandbox_generation=sandbox["generation"],
        running=True,
        activity_active=False,
        idle_remaining_seconds=120,
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
    still_lost = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert still_lost is not None
    assert still_lost["resident_state"] == "lost"
    assert still_lost["native_state"] == "lost"


async def test_expired_shared_finalization_preserves_context_and_snapshot_progress(
    pool,
    store: Storage,
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
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-shared-one",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-shared-settled",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue from the settled shared context",
    )
    assert child is not None and child["state"] == "queued"
    await _expire_attempt(pool, store, claim["attempt_id"])

    actions = await store.attempt_commands.reap_expired(max_attempts=3)

    assert actions == [{"job_id": job["id"], "attempt_id": claim["attempt_id"], "action": "failed"}]
    durable = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["native_state"] == "ready"
    assert durable["context_status"] == "live"
    assert durable["native_session_id"] == "native-shared-settled"
    assert durable["last_settled_job_id"] == job["id"]
    assert durable["active_attempt_id"] is None
    assert durable["admitted_job_id"] is None
    assert durable["idle_deadline"] is not None
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    assert capture["anchor_job_id"] == job["id"]


async def test_expired_job_finalization_preserves_an_already_settled_context(
    pool,
    store: Storage,
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
        await store.sandbox.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )
    assert await store.attempt_commands.record_continuity_mode(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        mode=None,
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert (
        await store.sandbox.settle_thread_sandbox(
            **fence,
            job_id=job["id"],
            context_status="new",
            native_session_id="claude-session-one",
            idle_timeout_seconds=1800,
        )
        is not None
    )
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="continue from the already-settled context",
    )
    assert child is not None and child["state"] == "queued"
    await _expire_attempt(pool, store, claim["attempt_id"])

    actions = await store.attempt_commands.reap_expired(max_attempts=3)

    assert actions == [{"job_id": job["id"], "attempt_id": claim["attempt_id"], "action": "failed"}]
    durable = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["context_status"] == "new"
    assert durable["last_settled_job_id"] == job["id"]
    assert durable["admitted_job_id"] is None
    assert durable["idle_deadline"] is not None
    assert (await store.execution_queries.job_turn_telemetry(job_id=job["id"]))[
        "context_status"
    ] == "new"
    failed = await store.conversation_queries.get_job(job["id"])
    assert failed["state"] == "failed"
    assert "prompt was not replayed" in failed["detail"]
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"


async def test_expired_retained_admission_fails_closed_instead_of_replaying(
    pool,
    store: Storage,
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
        await store.sandbox.mark_thread_sandbox_running(
            **fence,
            container_id="container-retained-one",
            context_status="new",
        )
        is not None
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="must not run after an expired admitted turn",
    )
    assert child is not None and child["state"] == "queued"
    await _expire_attempt(pool, store, claim["attempt_id"])

    actions = await store.attempt_commands.reap_expired(max_attempts=3)

    assert actions == [{"job_id": job["id"], "attempt_id": claim["attempt_id"], "action": "failed"}]
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "failed"
    durable = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert durable is not None
    assert durable["state"] == "stopping"
    assert durable["context_status"] == "lost"
    assert durable["active_attempt_id"] is None
    assert durable["admitted_job_id"] is None
    assert durable["idle_deadline"] is None
    assert (await store.execution_queries.job_turn_telemetry(job_id=job["id"]))[
        "context_status"
    ] == "lost"
    failed_child = await store.conversation_queries.get_job(child["id"])
    assert failed_child["state"] == "failed"
    assert "lost the retained sandbox context" in failed_child["detail"]


async def test_reap_fails_job_after_max_attempts(pool, store: Storage):
    """Exhausting the attempt budget fails the job instead of requeueing."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    await _expire_attempt(pool, store, claim["attempt_id"])

    actions = await store.attempt_commands.reap_expired(max_attempts=1)
    assert actions[0]["action"] == "failed"
    fetched = await store.conversation_queries.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert "exhausted" in fetched["detail"]
    assert await store.sandbox.list_terminal_resumes_pending() == [job["id"]]
    assert await store.sandbox.mark_terminal_resume_complete(job_id=job["id"]) is True
    assert await store.sandbox.list_terminal_resumes_pending() == []


async def test_cancel_queued_and_running(store: Storage):
    """Queued jobs cancel immediately; running jobs cancel via the worker."""
    queued = await _create_job(store)
    assert await store.attempt_commands.request_cancel(job_id=queued["id"]) == "cancelled"
    assert (await store.conversation_queries.get_job(queued["id"]))["state"] == "cancelled"
    assert await store.sandbox.list_terminal_resumes_pending() == [queued["id"]]
    assert await store.sandbox.mark_terminal_resume_complete(job_id=queued["id"]) is True

    # Owner scoping: the wrong user cannot cancel.
    running = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert (
        await store.attempt_commands.request_cancel(job_id=running["id"], user_id="someone-else")
        is None
    )

    assert (
        await store.attempt_commands.request_cancel(job_id=running["id"], user_id="user-1")
        == "running"
    )
    beat = await store.attempt_commands.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        lease_ttl_seconds=60,
    )
    assert beat["ok"] is True
    assert beat["cancel_requested"] is True

    # The worker performs the fenced terminal transition.
    assert (
        await store.attempt_commands.transition(
            job_id=running["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="cancelled",
        )
        is True
    )
    assert (await store.conversation_queries.get_job(running["id"]))["state"] == "cancelled"

    # Cancelling a terminal job is a no-op that reports the state.
    assert await store.attempt_commands.request_cancel(job_id=running["id"]) == "cancelled"


async def test_model_credential_follows_the_fence(pool, store: Storage):
    """A worker token buys inference only while its attempt owns the job.

    This is the revocation mechanism for the sandbox's model access: there is
    no key to revoke, so the checks that matter are that a live fence resolves
    and that every way of losing the fence stops resolving.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }

    identity = await store.credential_queries.resolve_model_credential(**fence)
    assert identity is not None
    assert identity["user_id"] == "user-1"
    # The budget assertion that sat here went with the column: a grant scopes
    # what may be called, and the per-user daily quota governs how much. Every
    # fence assertion below — the subject of this test — is unchanged.

    # A different generation never resolves.
    assert (
        await store.credential_queries.resolve_model_credential(
            job_id=job["id"], attempt_id=claim["attempt_id"], lease_generation=99
        )
        is None
    )

    # Lease expiry alone stops it, even before the reaper runs.
    await _expire_attempt(pool, store, claim["attempt_id"])
    assert await store.credential_queries.resolve_model_credential(**fence) is None

    # After the reap the new attempt resolves and the old one stays dead.
    await store.attempt_commands.reap_expired(max_attempts=3)
    retry = await _claim(store, worker_id="w2", lease_ttl_seconds=60)
    assert await store.credential_queries.resolve_model_credential(**fence) is None
    assert (
        await store.credential_queries.resolve_model_credential(
            job_id=job["id"],
            attempt_id=retry["attempt_id"],
            lease_generation=retry["lease_generation"],
        )
        is not None
    )


async def test_model_credential_dies_with_a_terminal_job(pool, store: Storage):
    """Finishing the job stops its token from buying more inference."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await store.credential_queries.resolve_model_credential(**fence) is not None

    await _settled(pool, store, fence["job_id"])
    await store.attempt_commands.transition(**fence, from_states=("running",), to_state="succeeded")
    assert await store.credential_queries.resolve_model_credential(**fence) is None


@pytest.mark.parametrize("users_surface", ["absent", "unrelated", "unreadable", "other_profile"])
async def test_model_credential_uses_only_agent_owned_metadata(
    pool, store: Storage, users_surface: str
):
    """Local objects named users cannot change or break fenced MCP resolution."""
    connection = await store.mcp.create_mcp_connection(
        user_id="user-1",
        label="Docs",
        transport="streamable-http",
        location="remote",
        url="https://mcp.example.com/mcp",
        launch_spec=None,
        auth_kind="none",
        auth_config={},
        template="docs",
    )
    job = await _create_job(
        store,
        mcp_bindings=[
            {
                "connection_id": int(connection["id"]),
                "alias": "docs",
                "enabled": True,
                "config_revision": 1,
                "authorization_epoch": 1,
                "capability_revision": 1,
                "rules": {},
            }
        ],
    )
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            # Expose the real Agent tables through temporary views. A private
            # search path excludes any pre-existing public.users without
            # creating, dropping, or changing another service's auth table.
            await conn.execute(
                "CREATE TEMP VIEW agent_attempts AS SELECT * FROM public.agent_attempts"
            )
            await conn.execute("CREATE TEMP VIEW agent_jobs AS SELECT * FROM public.agent_jobs")
            # The thread carries the tool servers now; still agent-owned.
            await conn.execute(
                "CREATE TEMP VIEW agent_threads AS SELECT * FROM public.agent_threads"
            )
            # The bindings and their connections are joined on every fenced
            # resolution, so they are exposed the same way rather than read
            # through a second code path this test would not be checking.
            await conn.execute(
                "CREATE TEMP VIEW agent_mcp_thread_bindings "
                "AS SELECT * FROM public.agent_mcp_thread_bindings"
            )
            await conn.execute(
                "CREATE TEMP VIEW agent_mcp_connections "
                "AS SELECT * FROM public.agent_mcp_connections"
            )
            await conn.execute("SET LOCAL search_path = pg_temp, pg_catalog")
            if users_surface == "unrelated":
                await conn.execute("CREATE TEMP TABLE users (account_name TEXT)")
            elif users_surface == "unreadable":
                # A real PostgreSQL permission error, contained entirely in
                # temporary objects and independent of the test user's role.
                await conn.execute(
                    """
                    CREATE FUNCTION pg_temp.deny_users_lookup() RETURNS TEXT
                    LANGUAGE plpgsql AS $$
                    BEGIN
                        RAISE insufficient_privilege USING MESSAGE = 'users is not accessible';
                    END;
                    $$;
                    CREATE TEMP VIEW users AS
                    SELECT 'user-1'::text AS id, pg_temp.deny_users_lookup() AS role,
                           'active'::text AS status
                    """
                )
            elif users_surface == "other_profile":
                await conn.execute(
                    """
                    CREATE TEMP TABLE users (id TEXT, role TEXT, status TEXT);
                    INSERT INTO users VALUES ('user-1', 'unrelated-role', 'suspended')
                    """
                )
            else:
                assert await conn.fetchval("SELECT to_regclass('users')") is None

            @asynccontextmanager
            async def borrow_connection():
                yield conn

            scoped_store = Storage(SimpleNamespace(acquire=borrow_connection))
            identity = await scoped_store.credential_queries.resolve_model_credential(**fence)
            # The owner, the model and the session's bindings all still resolve,
            # and nothing about them comes from a ``users`` table this platform
            # does not own.
            assert identity["user_id"] == job["user_id"]
            assert identity["job_id"] == job["id"]
            assert identity["model"] == job["model"]
            assert identity["thread_id"] == job["thread_id"]
            assert [entry["alias"] for entry in identity["mcp_bindings"]] == ["docs"]
            # The relay reads these rows through ``binding_from_row`` on every
            # forwarded call, so the query has to hand back every field that
            # reader needs. It did not: ``thread_id`` was selected for
            # ``list_mcp_bindings`` and omitted from the fence, and the fakes
            # supplied it, so nothing failed until the merged host endpoint
            # drove the real store. Asserted here, on the real row.
            binding = binding_from_row(identity["mcp_bindings"][0])
            assert binding.thread_id == job["thread_id"]
            assert binding.alias == "docs"
            assert binding.connection_id == str(connection["id"])
            assert await conn.fetchval("SELECT 1") == 1
        finally:
            # Roll back even a passing test, including all temporary objects
            # and session settings. The shared pool must remain unchanged.
            await transaction.rollback()


async def test_an_expired_lease_is_dead_before_the_reaper_runs(pool, store: Storage):
    """Expiry must take effect immediately, not when the reaper next passes.

    Previously every fenced write checked only generation and status, so a
    stalled worker kept full authority for up to a reaper interval after its
    lease ran out — it could renew, write events, or finish the job. The
    reaper is a cleanup mechanism, not the thing that revokes.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    # Everything works while the lease is live.
    assert (
        await store.attempt_commands.append_event(**fence, event_type="message", payload={})
        is not None
    )

    await _expire_attempt(pool, store, claim["attempt_id"])
    # Deliberately do NOT run the reaper.

    assert (await store.attempt_commands.heartbeat(**fence, lease_ttl_seconds=60))["ok"] is False
    assert (
        await store.attempt_commands.append_event(**fence, event_type="message", payload={}) is None
    )
    assert (
        await store.attempt_commands.transition(
            job_id=job["id"], **fence, from_states=("running",), to_state="succeeded"
        )
        is False
    )
    # And the job is untouched: still running with no terminal state.
    fetched = await store.conversation_queries.get_job(job["id"])
    assert fetched["state"] == "running"


async def test_a_released_claim_costs_no_retry(pool, store: Storage):
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
        claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
        assert claim is not None, "a released job must be claimable again"
        assert await store.attempt_commands.release_claim(
            job_id=job["id"], attempt_id=claim["attempt_id"]
        )
        assert (await store.conversation_queries.get_job(job["id"]))["state"] == "queued"

    # The budget is untouched: a real attempt still gets to run and be reaped.
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    await _expire_attempt(pool, store, claim["attempt_id"])
    actions = await store.attempt_commands.reap_expired(max_attempts=3)

    assert actions[0]["action"] == "queued", "aborted claims must not count as attempts"
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "queued"


async def test_an_admitted_turn_is_never_handed_back_to_the_queue(store: Storage):
    """The guard that keeps a user's instruction from running twice.

    A host returns a turn it could not start, and the only thing standing
    between "could not start" and "already running" is this check — which is
    the database's, not the caller's, precisely because a host that is wrong
    about what its child did must not be able to cause a replay. Once the
    prompt has been admitted to the CLI, the lease and the retained-session
    rules decide, and they may end the conversation rather than repeat it.
    """
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="release-guard-container", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])

    released = await store.attempt_commands.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        reason="launch_failed",
    )

    assert released is False
    # Nothing moved: the attempt is still the live one and the job still running.
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "running"


async def test_a_turn_reserved_but_not_admitted_can_still_be_handed_back(store: Storage):
    """The window the release exists for, and the line it stops at.

    A sandbox reserved for a turn is not the turn having started — the prompt
    has not reached the CLI — so this is exactly the case a host that failed to
    start its child is in, and requeueing costs the user nothing.
    """
    job, claim, _sandbox = await _reserve_shared_chat(store)

    released = await store.attempt_commands.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        reason="launch_failed",
    )

    assert released is True
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "queued"


async def test_a_release_records_the_reason_it_was_given(store: Storage):
    """'This host cannot start turns' is what the trail is read for."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    await store.attempt_commands.release_claim(
        job_id=job["id"], attempt_id=claim["attempt_id"], reason="launch_failed"
    )

    events = await store.execution_queries.list_events_after(job_id=job["id"])
    aborted = [e for e in events if e["event_type"] == "attempt_aborted"]
    assert aborted and aborted[0]["payload"]["reason"] == "launch_failed"


async def test_releasing_records_why_rather_than_erasing_the_attempt(store: Storage):
    """History stays append-only: the abandoned attempt is marked, not deleted."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    await store.attempt_commands.release_claim(job_id=job["id"], attempt_id=claim["attempt_id"])

    events = await store.execution_queries.list_events_after(job_id=job["id"])
    aborted = [e for e in events if e["event_type"] == "attempt_aborted"]
    assert aborted, "an operator must be able to see the platform dropped this one"
    assert aborted[0]["attempt_id"] == claim["attempt_id"]


async def test_releasing_a_job_that_moved_on_is_a_no_op(store: Storage):
    """A late release must never drag a running job back to the queue."""
    job = await _create_job(store)
    first = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    await store.attempt_commands.release_claim(job_id=job["id"], attempt_id=first["attempt_id"])
    second = await _claim(store, worker_id="w2", lease_ttl_seconds=60)

    # The first attempt is already finished, so releasing it again changes nothing.
    assert (
        await store.attempt_commands.release_claim(job_id=job["id"], attempt_id=first["attempt_id"])
        is False
    )

    fetched = await store.conversation_queries.get_job(job["id"])
    assert fetched["state"] == "running"
    assert fetched["current_attempt_id"] == second["attempt_id"]


async def test_releasing_with_the_wrong_generation_is_refused(store: Storage):
    """The weakest fence in the store was this one; it now matches the others.

    Releasing a claim returns the job to the queue, so an unfenced release is a
    way to yank a job out from under the attempt that legitimately holds it.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    assert (
        await store.attempt_commands.release_claim(
            job_id=job["id"], attempt_id=claim["attempt_id"], lease_generation=999
        )
        is False
    )
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "running"

    assert (
        await store.attempt_commands.release_claim(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is True
    )


async def test_a_release_writes_its_event_into_the_right_job(store: Storage):
    """agent_job_events.job_id has no foreign key, so a mismatched pair would land
    a control event in another job's stream — possibly another tenant's."""
    victim = await _create_job(store)
    target = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    # Deliberately name the wrong job alongside the real attempt id.
    wrong = victim["id"] if claim["id"] != victim["id"] else target["id"]
    await store.attempt_commands.release_claim(job_id=wrong, attempt_id=claim["attempt_id"])

    stray = await store.execution_queries.list_events_after(job_id=wrong)
    assert not [e for e in stray if e["event_type"] == "attempt_aborted"], (
        "the event must follow the attempt's real job, not the caller's claim"
    )
    owned = await store.execution_queries.list_events_after(job_id=claim["id"])
    assert [e for e in owned if e["event_type"] == "attempt_aborted"]


async def test_a_cancelled_job_released_from_a_claim_ends_cancelled(store: Storage):
    """Requeueing a cancelled job put it somewhere nothing could ever reach.

    `claim_job` skips queued rows with `cancel_requested`, and the reaper only
    reaches jobs that still have a *running* attempt — which a released one
    does not. So the job sat in `queued` permanently, invisible to its owner's
    cancellation and to every worker.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert await store.attempt_commands.request_cancel(job_id=job["id"]) == "running"

    await store.attempt_commands.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    fetched = await store.conversation_queries.get_job(job["id"])
    assert fetched["state"] == "cancelled", "a cancelled job must not be requeued"
    assert await store.sandbox.list_terminal_resumes_pending() == [job["id"]]


async def test_an_uncancelled_job_still_returns_to_the_queue(store: Storage):
    """The ordinary release path is unchanged."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    await store.attempt_commands.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "queued"
    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60) is not None


async def test_a_replayed_event_seq_is_answered_with_the_stored_row(pool, store: Storage):
    """A retry after a dropped response duplicates neither event nor message."""
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)

    first = await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "the answer"},
        event_seq=1,
    )
    replay = await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "the answer"},
        event_seq=1,
    )
    second = await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="tool_use",
        payload={"name": "ls"},
        event_seq=2,
    )

    assert replay == first
    assert second is not None and second != first
    async with pool.acquire() as conn:
        events = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_job_events WHERE attempt_id = $1", claim["attempt_id"]
        )
        messages = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_thread_messages WHERE job_id = $1 AND role = 'assistant'",
            claim["id"],
        )
    assert events == 2
    assert messages == 1


async def test_a_resource_rollup_upserts_rather_than_accumulating(store: Storage):
    """A retried post is the same window said twice, not a second window.

    Idempotence here is a statement about ``ON CONFLICT``, which is exactly
    what a fake keyed by attempt cannot test: any dict-backed stand-in is
    idempotent by construction, so the assertion passes without the SQL being
    right.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    rollup = {
        "cpu_usec": 1_000,
        "mem_peak_bytes": 2_048,
        "net_rx_bytes": 4_096,
        "sample_count": 5,
        "coverage": 1.0,
        "sampler_version": "1",
    }

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"], lease_generation=claim["lease_generation"], rollup=rollup
    )
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"], lease_generation=claim["lease_generation"], rollup=rollup
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["attempt_id"] == claim["attempt_id"]
    assert telemetry["cpu_usec"] == 1_000, "a retried post accumulated instead of replacing"
    assert telemetry["net_rx_bytes"] == 4_096


async def test_retry_telemetry_is_read_back_per_attempt(pool, store: Storage):
    """The owner UI can pair each attempt's events with the same window."""
    job = await _create_job(store)
    first = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert first is not None
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        rollup={
            "cpu_usec": 1_000,
            "sample_count": 2,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Read")],
            "processes": [_retry_process("first")],
        },
    )
    await _expire_attempt(pool, store, first["attempt_id"])
    await store.attempt_commands.reap_expired(max_attempts=3)

    second = await _claim(store, worker_id="w2", lease_ttl_seconds=60)
    assert second is not None
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=second["attempt_id"],
        lease_generation=second["lease_generation"],
        rollup={
            "cpu_usec": 7_000,
            "sample_count": 4,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Bash")],
            "processes": [_retry_process("second")],
        },
    )

    attempts = await store.execution_queries.job_attempts_telemetry(job_id=job["id"])

    assert [attempt["attempt_id"] for attempt in attempts] == [
        first["attempt_id"],
        second["attempt_id"],
    ]
    assert [attempt["attempt_no"] for attempt in attempts] == [1, 2]
    assert all("tool_calls" not in attempt and "processes" not in attempt for attempt in attempts)
    details = [
        await store.execution_queries.job_turn_telemetry(
            job_id=job["id"], attempt_id=attempt["attempt_id"]
        )
        for attempt in attempts
    ]
    assert [attempt["cpu_usec"] for attempt in details] == [1_000, 7_000]
    assert [attempt["tool_calls"][0]["tool_name"] for attempt in details] == ["Read", "Bash"]
    assert [attempt["processes"][0]["comm"] for attempt in details] == ["first", "second"]
    other_job = await _create_job(store)
    assert (
        await store.execution_queries.job_turn_telemetry(
            job_id=other_job["id"], attempt_id=first["attempt_id"]
        )
        is None
    )


def _retry_process(comm: str) -> dict[str, object]:
    return {
        "process_seq": 1,
        "uid": 10001,
        "pid": 42,
        "tgid": 42,
        "ppid": 1,
        "comm": comm,
        "executable": "/bin/test",
        "started_offset_ms": 0,
        "duration_ms": 1,
        "exit_code": 0,
        "observed_fork": True,
        "observed_exec": True,
        "observed_exit": True,
        "tool_call_seqs": [1],
    }


async def test_a_superseded_attempt_cannot_overwrite_the_live_one_s_numbers(
    store: Storage,
):
    """Fenced by the same predicate as every other worker write."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"cpu_usec": 1_000, "sample_count": 5, "coverage": 1.0, "sampler_version": "1"},
    )

    stale = await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        rollup={"cpu_usec": 999_999, "sample_count": 1, "coverage": 1.0, "sampler_version": "1"},
    )

    assert stale is False
    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 1_000


async def test_an_unmeasured_window_is_stored_as_such_not_as_zero(store: Storage):
    """Null counters with coverage 0 — the row that says "not measured".

    Postgres is what makes this meaningful: the columns are nullable, so a
    turn nobody could measure reads back as NULL rather than as a zero the
    reader would have no way to question.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"sample_count": 0, "coverage": 0.0, "sampler_version": "1"},
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] is None
    assert telemetry["coverage"] == 0.0
    assert telemetry["sampler_version"] == "1"
    assert telemetry["tool_calls"] == []
    assert telemetry["tool_calls_truncated"] is False
    assert telemetry["tool_time_ms"] is None


MEASUREMENT_ID = "00000000-0000-4000-8000-000000000001"


def _turn_snapshot(
    *,
    state: str = "final",
    measurement_id: str = MEASUREMENT_ID,
    cpu_usec: int = 80_000_000,
    peak_bytes: int = 1_288_490_189,
    duration_ms: int = 130_000,
    observed_through_ms: int = 130_000,
) -> dict[str, object]:
    """One cumulative snapshot in the canonical snake_case stored shape."""
    return {
        "schema_version": 1,
        "measurement_id": measurement_id,
        "report_seq": 1,
        "state": state,
        "scope": "member_execution_window",
        "source": "kata-guest-member-window-v1",
        "window": {
            "duration_ms": duration_ms,
            "observed_through_ms": observed_through_ms,
            "open_scan_ms": 12,
            "close_scan_ms": 15,
        },
        "cpu": {
            "status": "available",
            "method": "live_and_exit_task_counters",
            "usec": cpu_usec,
            "reason_codes": [],
        },
        "memory": {
            "status": "available",
            "method": "sampled_member_pss",
            "peak_bytes": peak_bytes,
            "sample_interval_ms": 1000,
            "valid_samples": 131,
            "expected_samples": 131,
            "max_gap_ms": 1000,
            "reason_codes": [],
        },
        "storage_io": {
            "status": "unavailable",
            "method": "task_io_accounting",
            "read_bytes": None,
            "write_bytes": None,
            "reason_codes": ["filesystem_attribution_unverified"],
        },
    }


async def _record_turn_summary(
    store: Storage,
    claim: dict,
    *,
    report_seq: int,
    summary: dict[str, object],
    state: str | None = None,
    measurement_id: str | None = None,
    lease_generation: int | None = None,
) -> str:
    """Post one checkpoint through the arguments the route passes."""
    return await store.attempt_commands.record_attempt_turn_summary(
        attempt_id=claim["attempt_id"],
        lease_generation=(
            claim["lease_generation"] if lease_generation is None else lease_generation
        ),
        measurement_id=(
            str(summary["measurement_id"]) if measurement_id is None else measurement_id
        ),
        report_seq=report_seq,
        state=str(summary["state"]) if state is None else state,
        summary={**summary, "report_seq": report_seq},
    )


async def test_a_turn_summary_is_stored_and_its_retry_is_not_a_second_write(
    store: Storage,
):
    """A cumulative snapshot resent unchanged is the same snapshot, not a delta.

    This is the property a fake keyed by attempt cannot check: ``duplicate``
    has to come from the stored sequence and the stored payload matching, and
    the row must still hold the first value afterwards.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    snapshot = _turn_snapshot()

    assert await _record_turn_summary(store, claim, report_seq=1, summary=snapshot) == "stored"
    assert await _record_turn_summary(store, claim, report_seq=1, summary=snapshot) == "duplicate"

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000
    assert telemetry["turn_summary"]["state"] == "final"
    assert telemetry["turn_summary_received_at"] is not None


async def test_an_older_checkpoint_cannot_roll_a_stored_window_backwards(
    store: Storage,
):
    """Out-of-order delivery must not replace a newer cumulative snapshot."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(
            store, claim, report_seq=7, summary=_turn_snapshot(cpu_usec=50_000_000)
        )
        == "stored"
    )
    assert (
        await _record_turn_summary(
            store, claim, report_seq=3, summary=_turn_snapshot(cpu_usec=1_000)
        )
        == "conflict"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["cpu"]["usec"] == 50_000_000


async def test_two_payloads_at_one_sequence_are_a_protocol_conflict(
    store: Storage,
):
    """Last-write-wins would let whichever delivery arrived second decide."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=4, summary=_turn_snapshot()) == "stored"
    )
    different = _turn_snapshot(cpu_usec=1)
    assert await _record_turn_summary(store, claim, report_seq=4, summary=different) == "conflict"

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000


async def test_a_final_snapshot_cannot_be_overwritten_by_a_collecting_one(
    store: Storage,
):
    """A newer sequence does not outrank ``final``. Nothing un-finishes a turn."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(
            store, claim, report_seq=9, summary=_turn_snapshot(state="final")
        )
        == "stored"
    )
    assert (
        await _record_turn_summary(
            store,
            claim,
            report_seq=10,
            summary=_turn_snapshot(state="collecting", observed_through_ms=4_000),
        )
        == "conflict"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["state"] == "final"
    assert telemetry["turn_summary"]["window"]["observed_through_ms"] == 130_000


async def test_a_second_measurement_cannot_take_over_a_fixed_window(
    store: Storage,
):
    """The first accepted report fixes the attempt's ``measurement_id``."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )
    other = "00000000-0000-4000-8000-000000000002"
    assert (
        await _record_turn_summary(
            store,
            claim,
            report_seq=2,
            summary=_turn_snapshot(measurement_id=other, cpu_usec=7),
        )
        == "conflict"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["measurement_id"] == MEASUREMENT_ID


async def test_a_superseded_or_expired_lease_cannot_write_a_turn_summary(pool, store: Storage):
    """Fenced like every other worker write, against the database clock."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(
            store,
            claim,
            report_seq=1,
            summary=_turn_snapshot(),
            lease_generation=claim["lease_generation"] + 1,
        )
        == "lease_lost"
    )
    await _expire_attempt(pool, store, claim["attempt_id"])
    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot())
        == "lease_lost"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"] is None


async def test_the_legacy_writer_does_not_clobber_the_turn_summary(
    store: Storage,
):
    """Two independent reports about one attempt, in both write orders."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"cpu_usec": 1_000, "sample_count": 5, "coverage": 1.0, "sampler_version": "1"},
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 1_000, "the legacy rollup must have landed"
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000, (
        "the legacy writer erased the turn summary"
    )


async def test_the_turn_writer_keeps_the_legacy_row_and_its_child_rows(
    store: Storage,
):
    """The other direction: a snapshot must not erase independent evidence."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={
            "cpu_usec": 9_000,
            "mem_peak_bytes": 4_096,
            "sample_count": 12,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Read")],
            "processes": [_retry_process("reader")],
        },
    )
    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 9_000
    assert telemetry["mem_peak_bytes"] == 4_096
    assert telemetry["sampler_version"] == "2"
    assert [call["tool_name"] for call in telemetry["tool_calls"]] == ["Read"]
    assert [process["comm"] for process in telemetry["processes"]] == ["reader"]
    assert telemetry["turn_summary"]["measurement_id"] == MEASUREMENT_ID


async def test_a_turn_summary_alone_creates_a_legacy_compatible_base_row(
    store: Storage,
):
    """The base is the honest "no container totals were attributed", not a zero.

    ``sampler_version`` is NOT NULL with no default, so the row the snapshot
    writer inserts has to name the shared-member convention itself. A reader
    must see coverage 0 and NULL counters rather than a fabricated measurement.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["sampler_version"] == "shared-member-v1"
    assert telemetry["sample_count"] == 0
    assert telemetry["coverage"] == 0.0
    assert telemetry["cpu_usec"] is None
    assert telemetry["mem_peak_bytes"] is None
    assert telemetry["net_rx_bytes"] is None
    assert telemetry["tool_calls"] == []
    assert telemetry["processes"] == []
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000


def _tool_call(seq: int, name: str, **overrides: object) -> dict[str, object]:
    """One per-call row in the shape the runner's attribution produces."""
    return {
        "call_seq": seq,
        "tool_use_id": f"toolu_{seq}",
        "tool_name": name,
        "timing_source": "message",
        "duration_ms": 20,
        "sample_count": 0,
        "overlap_n": 1,
        "measured_span_ms": None,
        "cpu_usec_exclusive": None,
        "cpu_usec_shared": None,
        "mem_peak_exclusive_bytes": None,
        "mem_peak_shared_bytes": None,
        "process_count": 0,
        **overrides,
    }


async def test_per_call_rows_are_stored_and_read_back_in_order(store: Storage):
    """The rows the footer's expanded table renders, over real SQL."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={
            "sample_count": 12,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls_truncated": False,
            "tool_time_ms": 9_543,
            "process_observer": "kata-guest-ebpf-v1",
            "process_observation_complete": True,
            "process_events_lost": 0,
            "processes_truncated": False,
            "tool_calls": [
                _tool_call(
                    1,
                    "Bash",
                    timing_source="exact",
                    duration_ms=9_523,
                    sample_count=9,
                    measured_span_ms=8_000,
                    cpu_usec_exclusive=4_400_000,
                    mem_peak_exclusive_bytes=270_000_000,
                    process_count=1,
                    process_cpu_usec=15_000,
                    process_mem_peak_bytes=2_228_224,
                    process_io_read_bytes=0,
                    process_io_write_bytes=8_388_608,
                    process_resources_partial=True,
                ),
                _tool_call(2, "Read"),
            ],
            "processes": [
                {
                    "process_seq": 1,
                    "uid": 11001,
                    "pid": 412,
                    "tgid": 412,
                    "ppid": 41,
                    "comm": "tiny",
                    "executable": "/bin/tiny",
                    "started_offset_ms": 12,
                    "duration_ms": 1,
                    "exit_code": 0,
                    "observed_fork": True,
                    "observed_exec": True,
                    "observed_exit": True,
                    "tool_call_seqs": [1],
                    "cpu_usec": 15_000,
                    "mem_peak_bytes": 2_228_224,
                    "io_read_bytes": 0,
                    "io_write_bytes": 8_388_608,
                    "io_rchar_bytes": 8_390_683,
                    "io_wchar_bytes": 8_388_701,
                }
            ],
        },
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["tool_calls_truncated"] is False
    assert telemetry["tool_time_ms"] == 9_543
    first, second = telemetry["tool_calls"]
    assert [first["tool_name"], second["tool_name"]] == ["Bash", "Read"]
    assert [first["timing_source"], second["timing_source"]] == ["exact", "message"]
    assert first["cpu_usec_exclusive"] == 4_400_000
    assert first["process_count"] == 1
    assert first["process_cpu_usec"] == 15_000
    assert first["process_mem_peak_bytes"] == 2_228_224
    assert first["process_io_read_bytes"] == 0
    assert first["process_io_write_bytes"] == 8_388_608
    assert first["process_resources_partial"] is True
    assert second["process_cpu_usec"] is None
    assert second["process_resources_partial"] is False
    assert telemetry["process_observer"] == "kata-guest-ebpf-v1"
    assert telemetry["process_observation_complete"] is True
    assert telemetry["process_events_lost"] == 0
    assert telemetry["processes_truncated"] is False
    (process,) = telemetry["processes"]
    assert process["uid"] == 11001
    assert process["executable"] == "/bin/tiny"
    assert process["duration_ms"] == 1
    assert process["tool_call_seqs"] == [1]
    assert process["cpu_usec"] == 15_000
    assert process["mem_peak_bytes"] == 2_228_224
    assert process["io_write_bytes"] == 8_388_608
    assert process["io_rchar_bytes"] == 8_390_683
    assert process["io_wchar_bytes"] == 8_388_701
    # Null rather than zero, in the database itself: this is the row that says
    # the call was too short for the sampler to have seen anything.
    assert second["cpu_usec_exclusive"] is None
    assert second["mem_peak_exclusive_bytes"] is None
    assert second["sample_count"] == 0


async def test_a_retried_post_replaces_the_call_list_rather_than_appending(
    store: Storage,
):
    """The list is one statement about the attempt, not a set to union with."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    base = {"sample_count": 5, "coverage": 1.0, "sampler_version": "2"}

    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={**base, "tool_calls": [_tool_call(1, "Bash"), _tool_call(2, "Read")]},
    )
    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={**base, "tool_calls": [_tool_call(1, "Bash")]},
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert len(telemetry["tool_calls"]) == 1, "a retried post left a row from the first"


async def test_a_superseded_attempt_cannot_write_per_call_rows_either(store: Storage):
    """The rows describe spans inside a window this attempt no longer owns."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={
            "sample_count": 5,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Bash")],
        },
    )

    stale = await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        rollup={
            "sample_count": 1,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Bash", cpu_usec_exclusive=999_999)],
        },
    )

    assert stale is False
    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    (row,) = telemetry["tool_calls"]
    assert row["cpu_usec_exclusive"] is None, "a fenced write reached the per-call rows"


async def test_a_row_cannot_claim_a_number_is_both_exclusive_and_shared(
    store: Storage,
):
    """The whole point of two columns is that a reader can tell them apart."""
    import asyncpg

    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await store.attempt_commands.record_attempt_resources(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            rollup={
                "sample_count": 5,
                "coverage": 1.0,
                "sampler_version": "2",
                "tool_calls": [_tool_call(1, "Bash", cpu_usec_exclusive=10, cpu_usec_shared=10)],
            },
        )
    assert job["id"]


async def test_thread_events_span_every_turn_and_page(store: Storage):
    """A thread's complete log stays ordered and pageable across follow-ups."""
    first = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    for kind in ("tool_use", "tool_result", "message"):
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type=kind,
            payload={"marker": kind},
        )
    await store.attempt_commands.transition(
        job_id=first["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="synthetic first-turn failure",
    )
    second = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="and the docs"
    )
    assert second is not None

    events = await store.execution_queries.list_thread_events_after(
        job_id=second["id"], user_id="user-1"
    )
    assert events is not None
    assert [event["event_type"] for event in events] == ["tool_use", "tool_result", "message"]
    assert all(event["turn_no"] == 1 for event in events)
    assert events[0]["payload"] == {"marker": "tool_use"}

    page = await store.execution_queries.list_thread_events_after(
        job_id=second["id"], user_id="user-1", limit=2
    )
    assert page is not None and len(page) == 2
    rest = await store.execution_queries.list_thread_events_after(
        job_id=second["id"], user_id="user-1", after_id=page[-1]["id"]
    )
    assert rest is not None and [event["event_type"] for event in rest] == ["message"]


async def test_thread_events_stop_at_the_owner(store: Storage):
    mine = await _create_job(store)

    assert (
        await store.execution_queries.list_thread_events_after(job_id=mine["id"], user_id="user-2")
        is None
    )
    assert (
        await store.execution_queries.list_thread_events_after(job_id=mine["id"], user_id="user-1")
        == []
    )


async def test_session_pages_count_threads_and_keep_long_conversation_titles(store: Storage):
    """A long thread cannot consume the history window or replace its title."""
    jobs = [await _create_job(store, task_prompt=f"History task {index}") for index in range(55)]
    long_thread = jobs[0]
    for index in range(60):
        child = await store.conversation_commands.create_follow_up(
            parent_job_id=long_thread["id"], user_id="user-1", prompt=f"Follow-up {index}"
        )
        assert child is not None and child["turn_no"] == index + 2
    await store.conversation_commands.set_sessions_archived(
        thread_ids=[job["thread_id"] for job in jobs], user_id="user-1", archived=True
    )
    first = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, limit=50
    )
    assert first["total"] == 55 and len(first["sessions"]) == 50
    assert first["next_cursor"] is not None
    # Removing a preceding row must not skip the next conversation, as an
    # offset cursor would. Counts reflect the current owner history.
    await store.conversation_commands.set_sessions_archived(
        thread_ids=[first["sessions"][0]["thread_id"]], user_id="user-1", archived=False
    )
    second = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, limit=50, cursor=first["next_cursor"]
    )
    assert second["total"] == 54 and len(second["sessions"]) == 5
    assert second["next_cursor"] is None
    all_sessions = first["sessions"] + second["sessions"]
    assert {session["thread_id"] for session in all_sessions} == {job["thread_id"] for job in jobs}
    long_session = next(row for row in all_sessions if row["thread_id"] == long_thread["thread_id"])
    assert long_session["title"] == "History task 0"
    assert long_session["job"]["turn_no"] == 61
    assert long_session["job"]["task_prompt"] == "Follow-up 59"
    assert long_session["job"]["archived_at"] == long_session["archived_at"]


async def test_session_search_state_and_project_facets_apply_before_pagination(
    pool,
    store: Storage,
):
    target = await _create_job(store, task_prompt="Repair 100%_ Coverage", repo="acme/api")
    other_project = await _create_job(store, task_prompt="Repair Dashboard", repo="acme/web")
    needs_review = await _create_job(store, task_prompt="Repair renderer", repo="acme/api")
    foreign = await _create_job(store, user_id="user-2", task_prompt="Repair 100%_ Coverage")
    for job in (target, other_project, needs_review, foreign):
        await store.conversation_commands.set_thread_archived(
            job_id=job["id"], user_id=job["user_id"], archived=True
        )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', "
            "native_turn_no = COALESCE(native_turn_no, turn_no)"
        )
        await conn.execute(
            "UPDATE agent_jobs SET published_pr_url = 'https://example.test/pr/1' "
            "WHERE id = ANY($1::text[])",
            [target["id"], other_project["id"]],
        )
    literal = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, search="100%_"
    )
    assert [row["thread_id"] for row in literal["sessions"]] == [target["thread_id"]]
    found = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, search="rEPAir", state="done", repo="acme/api", limit=1
    )
    assert found["total"] == 1
    assert found["projects"] == [{"repo": "acme/api", "count": 1}, {"repo": "acme/web", "count": 1}]
    assert found["sessions"][0]["thread_id"] == target["thread_id"]
    review = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, state="needs_review"
    )
    assert review["total"] == 1 and review["sessions"][0]["thread_id"] == needs_review["thread_id"]
    missing = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, repo="acme/missing"
    )
    assert missing["total"] == 0 and missing["sessions"] == []
    assert missing["projects"] == [
        {"repo": "acme/api", "count": 2},
        {"repo": "acme/web", "count": 1},
    ]


async def test_idle_sessions_filter_latest_turn_before_pagination_and_facets(pool, store: Storage):
    completed = await _create_job(store, task_prompt="Completed", repo="acme/api")
    published = await _create_job(store, task_prompt="Published", repo="acme/web")
    stopped = await _create_job(store, task_prompt="Stopped", repo="acme/api")
    older_completed = await _create_job(store, task_prompt="Continued", repo="acme/api")
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=older_completed["id"], user_id="user-1", prompt="Continue working"
    )
    assert follow_up is not None and follow_up["state"] == "queued"
    foreign = await _create_job(store, user_id="user-2", repo="acme/api")
    unarchived = await _create_job(store, repo="acme/api")
    excluded = [
        await _create_job(store, task_prompt=state) for state in ("failed", "queued", "running")
    ]
    states = [
        (completed, "succeeded"),
        (published, "succeeded"),
        (stopped, "cancelled"),
        (older_completed, "succeeded"),
        (foreign, "succeeded"),
        (unarchived, "succeeded"),
        *zip(excluded, ("failed", "queued", "running"), strict=True),
    ]
    for job, state in states:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_jobs SET state = $2, "
                "native_turn_no = CASE WHEN $2 = 'succeeded' THEN turn_no ELSE NULL END "
                "WHERE id = $1",
                job["id"],
                state,
            )
        if job is not unarchived:
            await store.conversation_commands.set_thread_archived(
                job_id=job["id"], user_id=job["user_id"], archived=True
            )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET published_pr_url = 'https://example.test/pr/1' WHERE id = $1",
            published["id"],
        )
    cursor = None
    found = []
    for _ in range(3):
        page = await store.conversation_queries.list_sessions(
            user_id="user-1", archived=True, state="idle", limit=1, cursor=cursor
        )
        assert page["total"] == 3
        assert page["projects"] == [
            {"repo": "acme/api", "count": 2},
            {"repo": "acme/web", "count": 1},
        ]
        found.extend(page["sessions"])
        cursor = page["next_cursor"]
    assert cursor is None
    assert {row["thread_id"] for row in found} == {
        completed["thread_id"],
        published["thread_id"],
        stopped["thread_id"],
    }
    assert {row["job"]["state"] for row in found} == {"succeeded", "cancelled"}

    project = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, state="idle", repo="acme/api"
    )
    assert project["total"] == 2
    assert {row["thread_id"] for row in project["sessions"]} == {
        completed["thread_id"],
        stopped["thread_id"],
    }
    assert project["projects"] == page["projects"]


async def test_conversation_state_keeps_active_work_visible_after_cancelling_queued_turn(
    pool,
    store: Storage,
):
    first = await _create_job(store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Queued follow-up"
    )
    assert follow_up is not None and follow_up["state"] == "queued"
    assert (await store.conversation_queries.get_job(first["id"]))["conversation_state"] == "queued"
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'running' WHERE id = $1", first["id"])
    assert (
        await store.attempt_commands.request_cancel(job_id=follow_up["id"], user_id="user-1")
        == "cancelled"
    )

    for job_id in (first["id"], follow_up["id"]):
        assert (await store.conversation_queries.get_job(job_id))["conversation_state"] == "running"
    conversation = (await store.conversation_queries.list_conversations(user_id="user-1"))[
        "conversations"
    ][0]["job"]
    assert conversation["id"] == follow_up["id"]
    assert conversation["state"] == "cancelled"
    assert conversation["conversation_state"] == "running"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="running"))[
        "total"
    ] == 1
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="idle"))[
        "total"
    ] == 0

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            first["id"],
        )
    assert (await store.conversation_queries.get_job(first["id"]))["conversation_state"] == "idle"
    assert (await store.conversation_queries.get_job(follow_up["id"]))[
        "conversation_state"
    ] == "idle"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="idle"))[
        "total"
    ] == 1

    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'failed' WHERE id = $1", follow_up["id"])
    historical = await store.conversation_queries.get_job(first["id"])
    assert historical["state"] == "succeeded"
    assert historical["conversation_state"] == "failed"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="failed"))[
        "total"
    ] == 1
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="idle"))[
        "total"
    ] == 0


async def test_session_cursor_is_scoped_to_owner_and_filters(store: Storage):
    for _ in range(2):
        job = await _create_job(store)
        await store.conversation_commands.set_thread_archived(
            job_id=job["id"], user_id="user-1", archived=True
        )
    page = await store.conversation_queries.list_sessions(user_id="user-1", archived=True, limit=1)
    assert page["next_cursor"] is not None
    for overrides in (
        {"user_id": "user-2"},
        {"archived": False},
        {"search": "another title"},
        {"repo": "different/repo"},
        {"state": "queued"},
        {"cursor": "bad cursor"},
    ):
        args = {"user_id": "user-1", "archived": True, "cursor": page["next_cursor"], **overrides}
        with pytest.raises(ValueError, match="Invalid session cursor"):
            await store.conversation_queries.list_sessions(**args)


async def test_session_pages_can_skip_summary_without_changing_rows_or_cursor(store: Storage):
    for repo in ("acme/api", "acme/api", "acme/web"):
        job = await _create_job(store, repo=repo)
        await store.conversation_commands.set_thread_archived(
            job_id=job["id"], user_id="user-1", archived=True
        )
    options = {"user_id": "user-1", "archived": True, "repo": "acme/api", "limit": 1}
    summary = await store.conversation_queries.list_sessions(**options)
    rows = await store.conversation_queries.list_sessions(**options, include_summary=False)
    assert rows == {**summary, "total": None, "projects": None}
    assert summary["projects"] == [
        {"repo": "acme/api", "count": 2},
        {"repo": "acme/web", "count": 1},
    ]
    assert rows["next_cursor"] is not None
    next_rows = await store.conversation_queries.list_sessions(
        **options, cursor=rows["next_cursor"], include_summary=False
    )
    assert next_rows["total"] is None and next_rows["projects"] is None
    assert next_rows["sessions"][0]["thread_id"] != rows["sessions"][0]["thread_id"]
    assert next_rows["sessions"][0]["job"]["repo"] == "acme/api"
    assert next_rows["next_cursor"] is None


async def test_session_mutations_are_atomic_owner_scoped_and_irreversible(pool, store: Storage):
    mine = await _create_job(store)
    busy = await _create_job(store)
    foreign = await _create_job(store, user_id="user-2")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'failed' WHERE id = $1", mine["id"])
    assert await store.conversation_commands.set_sessions_archived(
        thread_ids=[mine["thread_id"], foreign["thread_id"]], user_id="user-1", archived=True
    ) == {"error": "not_found"}
    assert (await store.conversation_queries.get_job(mine["id"]))["archived_at"] is None
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[mine["thread_id"]], user_id="user-1"
    ) == {"error": "not_archived"}
    selection = [mine["thread_id"], busy["thread_id"]]
    archived = await store.conversation_commands.set_sessions_archived(
        thread_ids=selection, user_id="user-1", archived=True
    )
    assert len(archived["sessions"]) == 2
    assert len({row["archived_at"] for row in archived["sessions"]}) == 1
    assert await store.conversation_commands.delete_sessions(
        thread_ids=selection, user_id="user-1"
    ) == {"error": "session_active"}
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[mine["thread_id"], foreign["thread_id"]], user_id="user-1"
    ) == {"error": "not_found"}
    assert (await store.conversation_queries.get_job(mine["id"]))["deleted_at"] is None
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[mine["thread_id"]], user_id="user-1"
    ) == {"thread_ids": [mine["thread_id"]]}
    retained = await store.conversation_queries.get_job(mine["id"])
    assert retained is not None and retained["deleted_at"] is not None
    assert retained["task_prompt"] == mine["task_prompt"]
    assert (
        await store.conversation_queries.get_thread_for_job(job_id=mine["id"], user_id="user-1")
        is None
    )
    assert (
        await store.execution_queries.list_thread_events_after(job_id=mine["id"], user_id="user-1")
        is None
    )
    assert (
        await store.execution_queries.thread_stats_inputs(job_id=mine["id"], user_id="user-1")
        is None
    )
    assert (
        await store.conversation_commands.set_thread_archived(
            job_id=mine["id"], user_id="user-1", archived=False
        )
        is None
    )
    assert (
        await store.conversation_commands.set_thread_pinned(
            job_id=mine["id"], user_id="user-1", pinned=True
        )
        is None
    )
    assert await store.conversation_commands.set_sessions_archived(
        thread_ids=selection, user_id="user-1", archived=False
    ) == {"error": "not_found"}
    assert (await store.conversation_queries.get_job(busy["id"]))["archived_at"] is not None
    assert (
        await store.conversation_commands.create_follow_up(
            parent_job_id=mine["id"], user_id="user-1", prompt="must not resurrect"
        )
        is None
    )
    assert (
        await store.session_fork.prepare_session_fork(
            source_job_id=mine["id"], user_id="user-1", idempotency_key="deleted-history"
        )
        is None
    )
    assert {
        job["id"]
        for job in await store.conversation_queries.list_jobs(user_id="user-1", archived=True)
    } == {busy["id"]}
    assert (await store.conversation_queries.list_sessions(user_id="user-1", archived=True))[
        "total"
    ] == 1
    assert {
        item["job"]["id"]
        for item in (
            await store.conversation_queries.list_conversations(
                user_id="user-1", include_archived=True
            )
        )["conversations"]
    } == {busy["id"]}
    assert (await store.conversation_queries.list_projects(user_id="user-1", archived=True))[0][
        "task_count"
    ] == 1


async def test_archiving_serializes_with_follow_up_creation(pool, store: Storage):
    job = await _create_job(store)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agent_threads SET archived_at = NOW() WHERE id = $1", job["thread_id"]
        )
        follow_up = asyncio.create_task(
            store.conversation_commands.create_follow_up(
                parent_job_id=job["id"], user_id="user-1", prompt="racing message"
            )
        )
        await asyncio.sleep(0.03)
        assert not follow_up.done()
    assert await asyncio.wait_for(follow_up, timeout=3) == {"error": "session_archived"}
    assert (
        len(
            (
                await store.conversation_queries.get_thread_for_job(
                    job_id=job["id"], user_id="user-1"
                )
            )["jobs"]
        )
        == 1
    )
    await store.conversation_commands.set_sessions_archived(
        thread_ids=[job["thread_id"]], user_id="user-1", archived=False
    )
    resumed = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="resume"
    )
    assert resumed is not None and resumed["turn_no"] == 2


async def test_deleting_an_idle_shared_session_keeps_workspace_sibling_and_billing(
    pool,
    store: Storage,
):
    """History deletion is independent of the shared container's lifetime."""
    source, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": source["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-history-delete", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=source["id"])
    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "Retained answer for the source session"},
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=source["id"],
        context_status="live",
        native_session_id="native-source-history",
        idle_timeout_seconds=1800,
        base_sha=source["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=source["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await store.conversation_commands.set_thread_archived(
        job_id=source["id"], user_id="user-1", archived=True
    )
    pending = await store.conversation_queries.list_sessions(user_id="user-1", archived=True)
    assert pending["sessions"][0]["delete_block_reason"] == "publication_pending"
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[source["thread_id"]], user_id="user-1"
    ) == {"error": "publication_pending"}
    sibling = await _create_job(store, task_prompt="forked sibling")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_snapshots SET state = 'published'")
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', "
            "native_turn_no = COALESCE(native_turn_no, turn_no), fork_source_job_id = $2 WHERE id = $1",
            sibling["id"],
            source["id"],
        )
        await conn.execute(
            """
            INSERT INTO agent_thread_sandbox_memberships (
                thread_id, user_id, sandbox_id, native_session_id, native_state, context_status,
                resident_state, last_settled_job_id, forked_from_thread_id, fork_anchor_job_id
            ) VALUES ($1, (SELECT user_id FROM agent_threads WHERE id = $1), $2, 'native-sibling-history', 'ready', 'live', 'live', $3, $4, $5)
            """,
            sibling["thread_id"],
            sandbox["sandbox_id"],
            sibling["id"],
            source["thread_id"],
            source["id"],
        )
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET residency_generation = 1, residency_status = 'hot',
                residency_lease_expires_at = NOW() + INTERVAL '1 hour',
                residency_grant_id = 'grant-history'
            WHERE thread_id = $1 AND sandbox_id = $2
            """,
            source["thread_id"],
            sandbox["sandbox_id"],
        )
        await conn.execute(
            """
            INSERT INTO agent_attempt_ledger
                (attempt_id, grant_id, residency_generation, binding_id, window_since, snapshot)
            VALUES ($1, 'grant-history', 1, 'binding-test', NOW(), '{"spent_usd": 0.42}')
            """,
            claim["attempt_id"],
        )
    sibling_turn = await store.conversation_commands.create_follow_up(
        parent_job_id=sibling["id"], user_id="user-1", prompt="independent work"
    )
    assert sibling_turn is not None and sibling_turn["state"] == "queued"
    before = await store.sandbox.thread_sandbox(thread_id=source["thread_id"])
    assert before["resident_state"] == "live"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", archived=True))[
        "sessions"
    ][0]["can_delete"]
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[source["thread_id"]], user_id="user-1"
    ) == {"thread_ids": [source["thread_id"]]}
    assert await store.sandbox.thread_sandbox(thread_id=source["thread_id"]) == before
    assert (await store.conversation_queries.get_job(sibling_turn["id"]))["state"] == "queued"
    assert (
        await store.conversation_queries.get_thread_for_job(job_id=sibling["id"], user_id="user-1")
        is not None
    )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT snapshot FROM agent_attempt_ledger WHERE attempt_id = $1",
                claim["attempt_id"],
            )
            == '{"spent_usd": 0.42}'
        )
        assert (
            await conn.fetchval(
                "SELECT residency_grant_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
                source["thread_id"],
            )
            == "grant-history"
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_job_events WHERE job_id = $1", source["id"]
            )
            > 0
        )
    assert (
        await store.residency.heartbeat_residency(
            thread_id=source["thread_id"], generation=1, host="shared-runner", ttl_seconds=120
        )
        == "grant-history"
    )


async def test_generated_title_is_canonical_and_stable_across_turns(store: Storage):
    """The prompt remains intact while each turn reads the same short summary."""
    prompt = "Please implement the CSV export for SPRUCE_8462 with detailed validation."
    job, claim, sandbox = await _reserve_shared_chat(store, task_prompt=prompt)
    assert len(job["title"]) <= 36
    assert job["title_source"] == "fallback"
    assert claim is not None and claim["title_generation_pending"]
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await store.conversation_commands.set_generated_thread_title(
        **fence, title="SPRUCE_8462 CSV export"
    )
    assert not await store.conversation_commands.set_generated_thread_title(
        **fence, title="Second summary"
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None
    assert saved["title"] == "SPRUCE_8462 CSV export"
    assert saved["title_source"] == "generated"
    assert saved["task_prompt"] == prompt
    sandbox_fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **sandbox_fence, container_id="title-container", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**sandbox_fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **sandbox_fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="title-session",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="Now add JSON too"
    )
    assert child is not None and child.get("error") is None
    assert child["title"] == saved["title"]
    assert child["title_source"] == "generated"
    followup_claim = await _claim(
        store,
        worker_id="title-worker",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
    )
    assert followup_claim is not None and not followup_claim["title_generation_pending"]
    assert not await store.conversation_commands.set_generated_thread_title(
        job_id=child["id"],
        attempt_id=followup_claim["attempt_id"],
        lease_generation=followup_claim["lease_generation"],
        title="JSON export",
    )


async def test_manual_same_title_wins_in_flight_generation(pool, store: Storage):
    """A manual save claims ownership even if the user keeps the fallback text."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None and claim["title_generation_pending"]
    # Hold the user edit uncommitted so generation really races the row lock.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agent_threads SET title_source = 'manual' WHERE id = $1",
            job["thread_id"],
        )
        generation = asyncio.create_task(
            store.conversation_commands.set_generated_thread_title(
                job_id=job["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                title="Unwanted automatic title",
            )
        )
        await asyncio.sleep(0.03)
        assert not generation.done()
    assert not await asyncio.wait_for(generation, timeout=3)
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title"] == job["title"]
    assert saved["title_source"] == "manual"


async def test_manual_same_title_sets_provenance(store: Storage):
    """Saving the visible fallback without edits still blocks later generation."""
    job = await _create_job(store)
    assert await store.conversation_commands.set_thread_title(
        job_id=job["id"], user_id=job["user_id"], title=job["title"]
    )
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None and not claim["title_generation_pending"]
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title_source"] == "manual"
    assert not await store.conversation_commands.set_generated_thread_title(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        title="Unwanted title",
    )


@pytest.mark.parametrize(
    "fence_break", ["generation", "job", "expired", "failed", "cancelled", "deleted"]
)
async def test_generated_title_rejects_stale_or_deleted_work(
    pool, store: Storage, fence_break: str
):
    """Expired/superseded work cannot mutate the canonical conversation title."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None
    generation = claim["lease_generation"]
    job_id = job["id"]
    if fence_break == "generation":
        generation += 1
    elif fence_break == "job":
        job_id = (await _create_job(store, user_id="other-owner"))["id"]
    elif fence_break in {"failed", "cancelled"}:
        assert await store.attempt_commands.transition(
            job_id=job_id,
            attempt_id=claim["attempt_id"],
            lease_generation=generation,
            from_states=("running",),
            to_state=fence_break,
        )
    else:
        async with pool.acquire() as conn:
            if fence_break == "expired":
                await conn.execute(
                    "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE id = $1",
                    claim["attempt_id"],
                )
            else:
                await conn.execute(
                    "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1",
                    job["thread_id"],
                )
    assert not await store.conversation_commands.set_generated_thread_title(
        job_id=job_id,
        attempt_id=claim["attempt_id"],
        lease_generation=generation,
        title="Stale result",
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title"] == job["title"]


async def test_title_generation_is_not_repeated_on_retry(store: Storage):
    """Failed first-claim generation keeps the short fallback on a later attempt."""
    job = await _create_job(store)
    first = await _claim(store, worker_id="first-worker", lease_ttl_seconds=60)
    assert first is not None and first["title_generation_pending"]
    assert await store.attempt_commands.release_claim(
        job_id=job["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
    )
    second = await _claim(store, worker_id="second-worker", lease_ttl_seconds=60)
    assert second is not None and not second["title_generation_pending"]
    for claim in [first, second]:
        assert not await store.conversation_commands.set_generated_thread_title(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            title="Late summary",
        )


async def test_successful_short_task_accepts_delayed_title(pool, store: Storage):
    """A fast task can finish before its independent title request returns."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await _settled(pool, store, fence["job_id"])
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )
    assert await store.conversation_commands.set_generated_thread_title(
        **fence, title="Fix flaky test"
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title"] == "Fix flaky test"
    assert saved["title_source"] == "generated"


async def test_a_cancelled_parent_released_by_the_platform_unblocks_its_queued_child(
    store: Storage,
):
    """A platform-side release that lands as ``cancelled`` is a turn boundary too.

    The child is already queued and becomes eligible as soon as the parent's
    terminal write commits, without waiting for a periodic sweep.
    """
    parent = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == parent["id"]
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="and then this"
    )
    assert child is not None and child["state"] == "queued"

    assert (
        await store.attempt_commands.request_cancel(job_id=parent["id"], user_id="user-1")
        == "running"
    )
    assert await store.attempt_commands.release_claim(
        job_id=parent["id"], attempt_id=claim["attempt_id"]
    )

    assert (await store.conversation_queries.get_job(parent["id"]))["state"] == "cancelled"
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"
    # And it is the next thing a runner is handed.
    next_claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert next_claim is not None and next_claim["id"] == child["id"]


async def test_queued_follow_up_needs_no_promotion_after_parent_finishes(
    pool,
    store: Storage,
):
    """Eligibility follows the parent state even if no terminal cleanup ran."""
    parent = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="and then this"
    )
    assert child is not None and child["state"] == "queued"
    # A terminal parent without the scoped continuity cleanup, as can exist
    # in legacy data. Claiming does not depend on a second state update.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', current_attempt_id = NULL WHERE id = $1",
            parent["id"],
        )
        await conn.execute(
            "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() WHERE id = $1",
            claim["attempt_id"],
        )
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"
    next_claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert next_claim is not None and next_claim["id"] == child["id"]


async def test_queued_cleanup_fails_tail_after_success_without_retained_context(
    pool,
    store: Storage,
):
    """Legacy success cannot cause a queued follow-up to start a fresh sandbox."""
    parent = await _create_job(store)
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="second turn"
    )
    grandchild = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="third turn"
    )
    assert child is not None and grandchild is not None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            parent["id"],
        )
    other = await _create_job(store, task_prompt="independent runnable turn")
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == other["id"]
    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60) is None
    await store.attempt_commands.sweep_queued_follow_ups()
    for pending in (child, grandchild):
        saved = await store.conversation_queries.get_job(pending["id"])
        assert saved["state"] == "failed"
        assert "predates retained-session continuity" in saved["detail"]


@pytest.mark.parametrize("lost_field", ["context_status", "native_state"])
async def test_queued_child_with_lost_membership_is_not_claimed_before_cleanup(
    pool,
    store: Storage,
    lost_field: str,
):
    """A running group does not prove its member's native context is usable."""
    parent, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": parent["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-lost-member",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=parent["id"])
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="second turn"
    )
    assert child is not None and child["state"] == "queued"
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=parent["id"],
        context_status="live",
        native_session_id="native-lost-member",
        idle_timeout_seconds=1800,
        base_sha=parent["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    # Model legacy membership loss without the same-transaction tail cleanup.
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE agent_thread_sandbox_memberships SET {lost_field} = 'lost' "
            "WHERE thread_id = $1",
            parent["thread_id"],
        )
    assert (
        await _claim(
            store,
            worker_id="w2",
            lease_ttl_seconds=60,
            host="shared-runner",
            host_credential_hash="test-credential-hash",
        )
        is None
    )
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"
    await store.attempt_commands.sweep_queued_follow_ups()
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "failed"


async def test_a_success_that_skipped_settlement_is_refused_loudly(pool, store: Storage):
    """The store does not guess a native ordinal; a protocol slip fails, visibly."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    with pytest.raises(ValueError, match="settle"):
        await store.attempt_commands.transition(
            **fence, from_states=("running",), to_state="succeeded"
        )
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "running"
    await _settled(pool, store, job["id"])
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )


async def test_conversations_activity_counts_an_earlier_turn_finishing_last(
    pool,
    store: Storage,
):
    """A cancelled follow-up does not hide the parent's later completion.

    The newest *turn* is the cancelled child; the newest *activity* is the
    parent succeeding after it. The list must order by the latter.
    """
    parent = await _create_job(store, task_prompt="first conversation")
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == parent["id"]
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="never mind"
    )
    assert child is not None and child["state"] == "queued"
    assert (
        await store.attempt_commands.request_cancel(job_id=child["id"], user_id="user-1")
        == "cancelled"
    )
    other = await _create_job(store, task_prompt="second conversation")
    before = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in before["conversations"]] == [other["id"], child["id"]]

    await _settled(pool, store, parent["id"])
    assert await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    after = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in after["conversations"]] == [child["id"], other["id"]]
    finished = await store.conversation_queries.get_job(parent["id"])
    assert after["conversations"][0]["last_activity_at"] == finished["updated_at"]


async def test_conversations_activity_counts_a_publication_of_an_earlier_turn(
    pool,
    store: Storage,
):
    """Publication stamps the turns it included, and that is activity too."""
    parent = await _create_job(store, task_prompt="first conversation")
    child = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="never mind"
    )
    assert child is not None
    assert (
        await store.attempt_commands.request_cancel(job_id=child["id"], user_id="user-1")
        == "cancelled"
    )
    other = await _create_job(store, task_prompt="second conversation")
    before = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in before["conversations"]] == [other["id"], child["id"]]
    # What ``record_snapshot_publish`` writes on every included, succeeded
    # turn: the pull request, the commit, and a fresh ``updated_at``.
    published_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1, "
            "published_pr_url = 'https://example.test/pr/1', updated_at = $2 WHERE id = $1",
            parent["id"],
            published_at,
        )

    after = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in after["conversations"]] == [child["id"], other["id"]]
    assert after["conversations"][0]["last_activity_at"] == published_at


async def test_side_chat_follow_up_http_receipt_matches_retained_owner_read(
    pool, store, monkeypatch
):
    """Pin the browser's receipt contract through the real SQL command and HTTP route.

    The INSERT command deliberately returns JOB_SELECT without membership
    columns. Serializing it directly used to commit a turn but leave every
    side-chat send looking like a rejected workspace identity on the client.
    """
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from cloud_agent.api import routes, turns
    from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client, get_storage

    job, _claim_row, sandbox = await _reserve_shared_chat(store)
    origin = await _create_job(store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET fork_anchor_job_id = $1, forked_from_thread_id = $3 WHERE thread_id = $2",
            origin["id"],
            job["thread_id"],
            origin["thread_id"],
        )
    command_rows = []
    create_follow_up = store.conversation_commands.create_follow_up

    async def capture_command_row(**kwargs):
        row = await create_follow_up(**kwargs)
        command_rows.append(row)
        return row

    monkeypatch.setattr(store.conversation_commands, "create_follow_up", capture_command_row)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_agent_owner] = lambda: {"user_id": job["user_id"]}
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[get_gateway_client] = lambda: None
    monkeypatch.setattr(turns, "require_entitled_repo", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/v1/agent/jobs/{job['id']}/follow-ups"
        headers = {"Idempotency-Key": "side-receipt-contract"}
        response = await client.post(path, json={"prompt": "side question"}, headers=headers)
        assert response.status_code == 201, response.text
        receipt = response.json()
        assert len(command_rows) == 1
        assert command_rows[0]["thread_sandbox"] is None
        replay = await client.post(path, json={"prompt": "side question"}, headers=headers)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == receipt["id"]
        for body in (receipt, replay.json()):
            assert body["sandbox_id"] == sandbox["sandbox_id"]
            assert body["fork_anchor_job_id"] == origin["id"]
            assert body["thread_id"] == job["thread_id"]
            assert body["workspace_shared"] is True
        # This fixture is consumed by SideChat.test.tsx as well. Normalize only
        # generated IDs, preserving the real HTTP membership projection.
        import json
        from pathlib import Path

        contract = json.loads(
            (Path(__file__).parents[2] / "fixtures/side_chat_job_receipt.json").read_text()
        )
        normalized_ids = {
            sandbox["sandbox_id"]: "shared",
            job["thread_id"]: "side",
            origin["id"]: "main-2",
        }
        assert {key: normalized_ids.get(receipt[key], receipt[key]) for key in contract} == contract
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_jobs WHERE thread_id = $1", job["thread_id"]
                )
                == 2
            )

    await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id=job["user_id"], archived=True
    )
    refused = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"],
        user_id=job["user_id"],
        idempotency_key="archived-side",
    )
    assert refused == {"error": "session_archived"}
