"""Real PostgreSQL lifecycle of installation snapshots: build, receipt, use and retirement."""

from __future__ import annotations

import asyncio

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
import secrets
from contextlib import suppress

import pytest
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _SHARED_HOST_CAPABILITIES,
    _claim as _claim_job,
    _expire_attempt,
    _shared_claim_attestation,
    pool,
    store,
)
from tests.integration.storage.test_environment_store import _job, _save
from tests.support.db import assert_pool_idle

from cloud_agent.storage.repositories import (
    environment_snapshots as snapshots_module,
    environments as environments_module,
)
from cloud_agent.workers.environment_maintenance import run_environment_maintenance
from cloud_agent_shared.environment import EnvironmentScripts
from cloud_agent_shared.environment_snapshots import owner_hash, snapshot_object_key
from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_CAPABILITY

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

IMAGE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
OTHER_IMAGE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "b" * 64
SOURCE = "c" * 40
LOCKFILES = {"package-lock.json": "d" * 40}
#: What the install script reads, say to write an ``.npmrc``.
SETUP_TOKEN = {"name": "NPM_TOKEN", "value": "npm-token-before", "secret": True, "phase": "setup"}
RUNTIME_TOKEN = {
    "name": "API_TOKEN",
    "value": "api-token-before",
    "secret": True,
    "phase": "runtime",
}


async def _enable_archives(store) -> None:
    configured = await store.host_pool.set_sandbox_archive_settings(
        expected_revision=0,
        settings={
            "enabled": True,
            "region": "us-east-1",
            "kms_key_arn": (
                "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
            ),
            "expected_bucket_owner": "123456789012",
            "max_archive_bytes": 2 * 1024**3,
            "multipart_threshold_bytes": 8 * 1024**2,
            "multipart_part_bytes": 8 * 1024**2,
        },
    )
    assert configured is not None


async def _host(store, slots: int = 4) -> None:
    capabilities = [*_SHARED_HOST_CAPABILITIES, "environment-v1", "environment-snapshot-v1"]
    await store.host_pool.register_host(
        host="environment-host",
        credential_hash="credential",
        capabilities=capabilities,
        slots_total=slots,
    )
    await store.host_pool.record_host_heartbeat(
        host="environment-host", capabilities=capabilities, sandbox_image_digest=IMAGE
    )


async def _binding(store, pool, env) -> str:
    job = await _job(store, env)
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT environment_binding_id FROM agent_threads WHERE id=$1", job["thread_id"]
        )


def _receipt(snapshot_id: str, **overrides):
    return {
        "snapshot_id": snapshot_id,
        "object_key": snapshot_object_key(owner_hash("alice"), snapshot_id),
        "version_id": "v1",
        "sha256": "e" * 64,
        "size_bytes": 1024,
        "new_layer_bytes": 900,
        "base_image": IMAGE,
        "source_sha": SOURCE,
        "lockfiles": LOCKFILES,
        "durations_ms": {"commit": 5},
        **overrides,
    }


async def _claim(store, *, capable: bool):
    return await store.environments.claim_environment_run(
        "environment-host", "credential", _shared_claim_attestation(), snapshot_capable=capable
    )


async def _ready_snapshot(store, pool, monkeypatch, env=None):
    """Queue, claim and complete one prebuild; return its snapshot id and binding."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _enable_archives(store)
    await _host(store)
    env = env or await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    snapshot_id = await store.environment_snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
    )
    run = await _claim(store, capable=True)
    target = await store.environment_snapshots.capture_target(run, "environment-host")
    assert target == {"snapshot_id": snapshot_id, "owner": owner_hash("alice")}
    assert await store.environment_snapshots.record_capture(
        run_id=run["id"],
        host="environment-host",
        generation=run["lease_generation"],
        receipt=_receipt(snapshot_id),
    )
    return snapshot_id, binding, run


async def _claim_task(store, pool, worker_id: str):
    """Claim the oldest queued task on ``runner-a``, which applied the archive settings."""
    async with pool.acquire() as conn:
        revision = await conn.fetchval(
            "SELECT sandbox_archive_revision FROM agent_runner_policy WHERE id"
        )
    capabilities = [*_SHARED_HOST_CAPABILITIES, SANDBOX_ARCHIVE_CAPABILITY]
    if await store.host_pool.get_host(host="runner-a") is None:
        await store.host_pool.register_host(
            host="runner-a",
            credential_hash="runner-a-credential-hash",
            capabilities=capabilities,
            slots_total=8,
        )
    await store.host_pool.record_host_heartbeat(
        host="runner-a",
        credential_hash="runner-a-credential-hash",
        capabilities=capabilities,
        sandbox_image_digest="sha256:shared-image",
        sandbox_archive_settings_applied_revision=revision,
        sandbox_archive_settings_revision_reported=True,
    )
    return await _claim_job(
        store,
        worker_id=worker_id,
        lease_ttl_seconds=60,
        claim_attestation={
            **_shared_claim_attestation(),
            "runner_capabilities": capabilities,
            "sandbox_archive_settings_applied_revision": revision,
        },
    )


async def _running(store, pool, binding):
    """Claim the task bound to ``binding``; return it and the fence its token names."""
    async with pool.acquire() as conn:
        job_id = await conn.fetchval(
            "SELECT j.id FROM agent_jobs j JOIN agent_threads t ON t.id=j.thread_id "
            "WHERE t.environment_binding_id=$1",
            binding,
        )
    claim = await _claim_task(store, pool, "w1")
    assert claim is not None and claim["id"] == job_id
    return job_id, {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }


async def test_environment_maintenance_worker_retires_expired_snapshots(store, pool, monkeypatch):
    """The periodic worker runs snapshot maintenance, not only environment-run cleanup."""
    snapshot_id, _binding, _run = await _ready_snapshot(store, pool, monkeypatch)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET ready_at=NOW()-INTERVAL '8 days' WHERE id=$1",
            snapshot_id,
        )

    task = asyncio.create_task(
        run_environment_maintenance(
            store.environments,
            store.environment_snapshots,
            interval_seconds=0.001,
            deadline_seconds=5,
        )
    )
    try:
        async with asyncio.timeout(3):
            while (
                await pool.fetchval(
                    "SELECT state FROM agent_environment_snapshots WHERE id=$1", snapshot_id
                )
                != "deleting"
            ):
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    await assert_pool_idle(pool)


@pytest.mark.parametrize(
    ("table", "query_pattern"),
    [
        ("agent_environment_runs", "%UPDATE agent_environment_runs SET state=%"),
        ("agent_environment_snapshots", "%UPDATE agent_environment_snapshots s SET state=%"),
    ],
)
async def test_cancelling_a_blocked_maintenance_pass_returns_its_connection(
    store, pool, table: str, query_pattern: str
):
    """Cancellation in either database pass returns the checked-out connection."""
    lock_connection = await pool.acquire()
    transaction = lock_connection.transaction()
    await transaction.start()
    lock_statement = {
        "agent_environment_runs": "LOCK TABLE agent_environment_runs IN ACCESS EXCLUSIVE MODE",
        "agent_environment_snapshots": (
            "LOCK TABLE agent_environment_snapshots IN ACCESS EXCLUSIVE MODE"
        ),
    }[table]
    await lock_connection.execute(lock_statement)
    task = asyncio.create_task(
        run_environment_maintenance(
            store.environments,
            store.environment_snapshots,
            interval_seconds=0.001,
            deadline_seconds=30,
        )
    )
    try:
        async with asyncio.timeout(3):
            while not await pool.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND state = 'active'
                      AND wait_event_type = 'Lock'
                      AND query LIKE $1
                )
                """,
                query_pattern,
            ):
                await asyncio.sleep(0.005)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with suppress(Exception):
            await transaction.rollback()
        await pool.release(lock_connection)

    await assert_pool_idle(pool)


async def test_a_prebuild_needs_a_capable_host_and_never_takes_the_owners_test(
    store, pool, monkeypatch
):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _enable_archives(store)
    await _host(store)
    env = await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    snapshots = store.environment_snapshots

    snapshot_id = await snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
    )
    assert snapshot_id is not None
    # One build of an installation at a time.
    assert (
        await snapshots.queue_prebuild(
            user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
        )
        is None
    )
    assert await _claim(store, capable=False) is None
    # A queued prebuild is not the owner's one test.
    test = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha=SOURCE,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key="test",
    )
    assert (await _claim(store, capable=True))["id"] == test["id"]
    prebuild = await _claim(store, capable=True)
    assert prebuild["purpose"] == "prebuild" and prebuild["snapshot_id"] == snapshot_id
    # A test never saves its container.
    assert await snapshots.capture_target({**test, "purpose": "test"}, "environment-host") is None
    assert await snapshots.capture_target(prebuild, "environment-host") is not None


async def test_only_the_runs_own_receipt_makes_a_snapshot_ready(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _enable_archives(store)
    await _host(store)
    env = await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    snapshots = store.environment_snapshots
    snapshot_id = await snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
    )
    run = await _claim(store, capable=True)
    await snapshots.capture_target(run, "environment-host")

    def record(**overrides):
        fence = {
            "run_id": run["id"],
            "host": "environment-host",
            "generation": run["lease_generation"],
        }
        fence.update({k: overrides.pop(k) for k in list(overrides) if k in fence})
        return snapshots.record_capture(**fence, receipt=_receipt(snapshot_id, **overrides))

    other_owner = snapshot_object_key(owner_hash("bob"), snapshot_id)
    assert not await record(object_key=other_owner)
    assert not await record(generation=run["lease_generation"] + 1)
    assert not await record(host="another-host")
    assert not await record(base_image="cloud-agent-sandbox@sha256:" + "a" * 64)
    assert not await record(lockfiles={"README.md": "d" * 40})
    assert (
        await snapshots.snapshot_for_claim(user_id="alice", binding_id=binding, base_image=IMAGE)
        is None
    )
    assert await record()
    found = await snapshots.snapshot_for_claim(
        user_id="alice", binding_id=binding, base_image=IMAGE
    )
    assert found is not None and found["snapshot_id"] == snapshot_id
    assert found["lockfiles"] == LOCKFILES
    assert found["owner"] == owner_hash("alice")
    assert (
        await snapshots.snapshot_for_claim(
            user_id="alice", binding_id=binding, base_image=OTHER_IMAGE
        )
        is None
    )
    assert (
        await snapshots.snapshot_for_claim(user_id="bob", binding_id=binding, base_image=IMAGE)
        is None
    )


async def _save_scripts(store, env, edits=(), **scripts):
    """Publish ``env`` with these scripts and variable edits; other variables are kept."""
    return await store.environments.save_environment(
        "alice",
        repo=env["repo"],
        name=env["name"],
        environment_id=env["id"],
        revision=env["revision"],
        document=EnvironmentScripts(**scripts).model_dump(),
        edits=list(edits),
    )


async def test_a_changed_installation_is_never_matched(store, pool, monkeypatch):
    snapshot_id, _, _run = await _ready_snapshot(store, pool, monkeypatch)
    env = (await store.environments.list_environments("alice"))[0]
    # A start script change installs the same thing, so the snapshot still serves.
    start_only = await _save_scripts(store, env, setup_script="npm ci", start_script="npm start")
    same = await _binding(store, pool, start_only)
    assert (
        await store.environment_snapshots.snapshot_for_claim(
            user_id="alice", binding_id=same, base_image=IMAGE
        )
    )["snapshot_id"] == snapshot_id
    changed = await _save(
        store, environment_id=env["id"], revision=start_only["revision"], script="npm ci --force"
    )
    newer = await _binding(store, pool, changed)
    assert (
        await store.environment_snapshots.snapshot_for_claim(
            user_id="alice", binding_id=newer, base_image=IMAGE
        )
        is None
    )
    current = await store.environments.current_setup_fingerprints("alice", [env["id"]])
    summaries = await store.environment_snapshots.summaries("alice", current)
    assert summaries[env["id"]] is None
    # The change retired it: changing back installs in full and builds anew.
    restored = await _save(
        store, environment_id=env["id"], revision=changed["revision"], script="npm ci"
    )
    again = await _binding(store, pool, restored)
    assert (
        await store.environment_snapshots.snapshot_for_claim(
            user_id="alice", binding_id=again, base_image=IMAGE
        )
        is None
    )


async def test_a_summary_shows_only_a_snapshot_of_that_environments_installation(
    store, pool, monkeypatch
):
    _snapshot_id, _, _run = await _ready_snapshot(store, pool, monkeypatch)
    env = (await store.environments.list_environments("alice"))[0]
    # A copy installs exactly what the original did, so it has the same fingerprint.
    copy = await store.environments.copy_environment(
        "alice", env["id"], repo="org/other", name="Copy", revision=env["revision"]
    )
    changed = await _save(
        store, environment_id=env["id"], revision=env["revision"], script="npm ci --force"
    )
    current = await store.environments.current_setup_fingerprints(
        "alice", [changed["id"], copy["id"]]
    )
    assert current[changed["id"]] != current[copy["id"]]
    summaries = await store.environment_snapshots.summaries("alice", current)
    # The original's snapshot is of the installation the copy has now, not of
    # its own: new tasks of the original install in full.
    assert summaries == {changed["id"]: None, copy["id"]: None}


async def test_use_keeps_a_snapshot_and_shortfalls_queue_a_build(store, pool, monkeypatch):
    snapshot_id, binding, _run = await _ready_snapshot(store, pool, monkeypatch)
    snapshots = store.environment_snapshots
    job_id, fence = await _running(store, pool, binding)
    assert await snapshots.record_use(
        job_id=job_id, **fence, snapshot_id=snapshot_id, outcome="used", base_sha=SOURCE
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT use_count, last_used_at FROM agent_environment_snapshots WHERE id=$1",
            snapshot_id,
        )
    assert row["use_count"] == 1 and row["last_used_at"] is not None
    await snapshots.record_use(
        job_id=job_id,
        **fence,
        snapshot_id=snapshot_id,
        outcome="lockfiles_changed",
        base_sha="f" * 40,
    )
    async with pool.acquire() as conn:
        queued = await conn.fetchrow(
            "SELECT s.state, r.base_sha FROM agent_environment_snapshots s "
            "JOIN agent_environment_runs r ON r.id=s.run_id WHERE s.state='queued'"
        )
    assert queued["base_sha"] == "f" * 40
    await snapshots.record_use(
        job_id=job_id, **fence, snapshot_id=snapshot_id, outcome="invalid", base_sha=None
    )
    async with pool.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert state == "deleting"
    with pytest.raises(ValueError):
        await snapshots.record_use(
            job_id=job_id, **fence, snapshot_id=None, outcome="used", base_sha=None
        )


async def _snapshot_and_builds(pool, snapshot_id) -> tuple[str, int, int]:
    """A snapshot's state and use count, and how many builds are queued."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, use_count FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
        queued = await conn.fetchval(
            "SELECT count(*) FROM agent_environment_snapshots WHERE state='queued'"
        )
    return row["state"], row["use_count"], queued


async def test_only_the_attempt_holding_the_lease_reports_on_a_snapshot(store, pool, monkeypatch):
    """A worker token is a signature over its attempt's fence: no host, no expiry.

    Without asking whether that attempt still holds the task's lease, any
    token ever minted for the task could delete a snapshot every workspace of
    its environment starts from, or queue a build at a commit of its choosing,
    at any later time.
    """
    snapshot_id, binding, _run = await _ready_snapshot(store, pool, monkeypatch)
    job_id, old = await _running(store, pool, binding)

    def report(fence, outcome, *, snapshot=snapshot_id, base_sha=None):
        return store.environment_snapshots.record_use(
            job_id=job_id, **fence, snapshot_id=snapshot, outcome=outcome, base_sha=base_sha
        )

    assert not await report({**old, "lease_generation": old["lease_generation"] + 1}, "invalid")
    await _expire_attempt(pool, store, old["attempt_id"])
    assert not await report(old, "invalid")
    # Superseded: the task runs again, under a new attempt.
    await store.attempt_commands.reap_expired(max_attempts=3)
    claim = await _claim_task(store, pool, "w2")
    assert claim is not None and claim["id"] == job_id
    new = {"attempt_id": claim["attempt_id"], "lease_generation": claim["lease_generation"]}
    assert not await report(old, "invalid")
    assert not await report(old, "absent", snapshot=None, base_sha="f" * 40)
    assert await _snapshot_and_builds(pool, snapshot_id) == ("ready", 0, 0)

    assert await report(new, "used")
    assert await _snapshot_and_builds(pool, snapshot_id) == ("ready", 1, 0)
    # A finished turn reports nothing more.
    assert await store.attempt_commands.transition(
        job_id=job_id, **new, from_states=("running",), to_state="failed", detail="setup failed"
    )
    assert not await report(new, "invalid")
    assert await _snapshot_and_builds(pool, snapshot_id) == ("ready", 1, 0)


async def test_a_removed_hosts_attempt_reports_nothing_on_a_snapshot(store, pool, monkeypatch):
    """Disabling the machine changes neither the attempt nor the task: the host fence refuses."""
    snapshot_id, binding, _run = await _ready_snapshot(store, pool, monkeypatch)
    job_id, fence = await _running(store, pool, binding)
    assert await store.host_pool.set_host_status(host="runner-a", status="disabled") == "ok"

    assert not await store.environment_snapshots.record_use(
        job_id=job_id, **fence, snapshot_id=snapshot_id, outcome="invalid", base_sha=None
    )
    assert await _snapshot_and_builds(pool, snapshot_id) == ("ready", 0, 0)


async def test_a_passing_test_queues_the_build_that_supersedes(store, pool, monkeypatch):
    first, _, _run = await _ready_snapshot(store, pool, monkeypatch)
    snapshots = store.environment_snapshots
    env = (await store.environments.list_environments("alice"))[0]

    async def passing_test(base_sha: str, key: str) -> str | None:
        test = await store.environments.create_environment_run(
            "alice",
            repo="org/repo",
            base_sha=base_sha,
            selection={"environment_id": env["id"], "environment_id_provided": True},
            key=key,
        )
        run = await _claim(store, capable=True)
        assert run["id"] == test["id"]
        # A test's start script ran with runtime values: its container is never saved.
        assert await snapshots.capture_target(run, "environment-host") is None
        assert await store.environments.update_environment_run(
            run["id"],
            "environment-host",
            run["lease_generation"],
            {"state": "succeeded", "cleanup_complete": True, "steps": [], "detail": ""},
        )
        return await snapshots.queue_after_test(run["id"])

    # The tested commit already has a ready snapshot of this installation.
    assert await passing_test(SOURCE, "same-commit") is None
    newer = await passing_test("f" * 40, "newer-commit")
    assert newer is not None
    prebuild = await _claim(store, capable=True)
    assert prebuild["purpose"] == "prebuild" and prebuild["snapshot_id"] == newer
    assert prebuild["base_sha"] == "f" * 40
    assert await snapshots.capture_target(prebuild, "environment-host") is not None
    assert await snapshots.record_capture(
        run_id=prebuild["id"],
        host="environment-host",
        generation=prebuild["lease_generation"],
        receipt=_receipt(newer, source_sha="f" * 40),
    )
    async with pool.acquire() as conn:
        states = dict(
            await conn.fetch(
                "SELECT id, state FROM agent_environment_snapshots ORDER BY created_at"
            )
        )
    assert states == {first: "superseded", newer: "ready"}
    # A prebuild's own success queues nothing more.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_runs SET state='succeeded' WHERE id=$1", prebuild["id"]
        )
    assert await snapshots.queue_after_test(prebuild["id"]) is None
    # Workspaces already starting from the old one get a grace period.
    await snapshots.maintain()
    assert await snapshots.deletions() == []
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET updated_at=NOW()-INTERVAL '25 hours' WHERE id=$1",
            first,
        )
    await snapshots.maintain()
    deletions = await snapshots.deletions()
    assert [d["snapshot_id"] for d in deletions] == [first] and deletions[0]["object_key"]
    assert await snapshots.confirm_deleted(first)
    assert not await snapshots.confirm_deleted(first)
    # Every host that holds its image is told to drop it; unknown images are left alone.
    assert await snapshots.retired_images([first, newer, "0" * 32]) == [
        {"snapshot_id": first, "owner": owner_hash("alice")}
    ]


async def test_unused_old_and_archived_snapshots_are_retired(store, pool, monkeypatch):
    snapshot_id, binding, _run = await _ready_snapshot(store, pool, monkeypatch)
    snapshots = store.environment_snapshots
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET ready_at=NOW()-INTERVAL '8 days' WHERE id=$1",
            snapshot_id,
        )
    assert (
        await snapshots.snapshot_for_claim(user_id="alice", binding_id=binding, base_image=IMAGE)
        is not None
    )
    await snapshots.maintain()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, detail FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert row["state"] == "deleting" and "Not used" in row["detail"]


async def test_an_owner_can_delete_and_the_quota_retires_the_least_used(store, pool, monkeypatch):
    snapshot_id, _, _run = await _ready_snapshot(store, pool, monkeypatch)
    env = (await store.environments.list_environments("alice"))[0]
    assert await store.environment_snapshots.discard("alice", env["id"]) == 1
    assert await store.environment_snapshots.discard("bob", env["id"]) == 0
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM agent_environment_snapshots WHERE id=$1", snapshot_id
            )
            == "deleting"
        )


async def test_snapshots_off_queue_and_hand_out_nothing(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    snapshots = store.environment_snapshots
    assert not await snapshots.enabled()
    assert (
        await snapshots.queue_prebuild(
            user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
        )
        is None
    )
    assert (
        await snapshots.snapshot_for_claim(user_id="alice", binding_id=binding, base_image=IMAGE)
        is None
    )


async def test_a_build_whose_run_failed_is_marked_failed(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _enable_archives(store)
    await _host(store)
    env = await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    snapshot_id = await store.environment_snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
    )
    run = await _claim(store, capable=True)
    await store.environment_snapshots.capture_target(run, "environment-host")
    await store.environment_snapshots.fail_capture(run_id=run["id"], detail="install failed")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, detail FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert dict(row) == {"state": "failed", "detail": "install failed"}
    # Tasks do not queue it again for a while; the owner still can.
    assert (
        await store.environment_snapshots.queue_prebuild(
            user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
        )
        is None
    )
    assert await store.environment_snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE, requested=True
    )


async def _building(store, pool, monkeypatch):
    """A claimed prebuild whose snapshot is building; return the snapshot id and run."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _enable_archives(store)
    await _host(store)
    env = await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    snapshot_id = await store.environment_snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
    )
    run = await _claim(store, capable=True)
    assert await store.environment_snapshots.capture_target(run, "environment-host")
    return snapshot_id, run, env, binding


@pytest.mark.parametrize(
    "ended",
    [
        "cancel_requested=TRUE",
        "lease_expires_at=NOW()-INTERVAL '1 second'",
        "started_at=NOW()-INTERVAL '31 minutes'",
        "purpose='test'",
    ],
)
async def test_a_receipt_from_a_run_that_may_no_longer_report_is_refused(
    store, pool, monkeypatch, ended
):
    snapshot_id, run, _env, binding = await _building(store, pool, monkeypatch)
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE agent_environment_runs SET {ended} WHERE id=$1", run["id"])
    assert not await store.environment_snapshots.record_capture(
        run_id=run["id"],
        host="environment-host",
        generation=run["lease_generation"],
        receipt=_receipt(snapshot_id),
    )
    async with pool.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert state == "building"
    assert (
        await store.environment_snapshots.snapshot_for_claim(
            user_id="alice", binding_id=binding, base_image=IMAGE
        )
        is None
    )


async def test_deleting_snapshots_cancels_their_prebuilds(store, pool, monkeypatch):
    _snapshot_id, running, env, binding = await _building(store, pool, monkeypatch)
    snapshots = store.environment_snapshots
    assert await snapshots.discard("alice", env["id"]) == 1
    # The running build's host is refused at its next report, and stops.
    assert not await store.environments.update_environment_run(
        running["id"], "environment-host", running["lease_generation"], {"steps": []}
    )
    queued = await snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE, requested=True
    )
    assert queued is not None
    assert await snapshots.discard("alice", env["id"]) == 1
    async with pool.acquire() as conn:
        runs = await conn.fetch(
            "SELECT state, cancel_requested, cleanup_complete, host FROM agent_environment_runs "
            "WHERE purpose='prebuild' ORDER BY created_at"
        )
    assert [(r["state"], r["cancel_requested"]) for r in runs] == [
        ("cancelled", True),
        ("cancelled", True),
    ]
    # Nothing ran the queued one, so nothing is left to clean up.
    assert runs[1]["host"] is None and runs[1]["cleanup_complete"]
    assert await _claim(store, capable=True) is None


async def test_a_prebuild_whose_snapshot_ended_is_never_claimed(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _enable_archives(store)
    await _host(store)
    env = await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    snapshot_id = await store.environment_snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET state='failed' WHERE id=$1", snapshot_id
        )
    assert await _claim(store, capable=True) is None


async def test_a_prebuild_maintenance_gave_up_on_does_not_stay_queued(store, pool, monkeypatch):
    """A build no host took within a day is given up on, and so is the run that would build it.

    Maintenance fails the snapshot, and a claim never takes a prebuild whose
    snapshot is no longer pending. Its run stayed queued for good: only an
    owner deleting the environment's snapshots cancelled it. The autoscaler
    counts every queued environment run as waiting work, so the pool never
    read as idle again and its nodes were never released.
    """
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _enable_archives(store)
    await _host(store)
    env = await _save(store, script="npm ci")
    binding = await _binding(store, pool, env)
    waiting = (await store.host_pool.autoscaling_observation(expected_deployment_sha=""))[
        "runnable_new_group_jobs"
    ]
    snapshot_id = await store.environment_snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha=SOURCE
    )
    async with pool.acquire() as conn:
        # A day in which no host was free to take it, as a busy pool's are:
        # hosts look for environment runs only when no task is waiting.
        await conn.execute(
            "UPDATE agent_environment_snapshots SET created_at=NOW()-INTERVAL '25 hours' "
            "WHERE id=$1",
            snapshot_id,
        )

    # What the attempt reaper runs on every pass.
    await store.environments.maintain_environments()
    await store.environment_snapshots.maintain()

    async with pool.acquire() as conn:
        snapshot = await conn.fetchval(
            "SELECT state FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
        run = await conn.fetchrow(
            "SELECT state, cleanup_complete, finished_at IS NOT NULL AS finished "
            "FROM agent_environment_runs WHERE snapshot_id=$1",
            snapshot_id,
        )
    assert snapshot == "failed"
    assert dict(run) == {"state": "failed", "cleanup_complete": True, "finished": True}
    assert (await store.host_pool.autoscaling_observation(expected_deployment_sha=""))[
        "runnable_new_group_jobs"
    ] == waiting
    assert await _claim(store, capable=True) is None


async def test_a_snapshot_saved_by_a_test_is_never_handed_out_and_is_retired(
    store, pool, monkeypatch
):
    snapshot_id, binding, run = await _ready_snapshot(store, pool, monkeypatch)
    # As earlier releases saved a passing test's container.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_runs SET purpose='test' WHERE id=$1", run["id"]
        )
    snapshots = store.environment_snapshots
    assert (
        await snapshots.snapshot_for_claim(user_id="alice", binding_id=binding, base_image=IMAGE)
        is None
    )
    await snapshots.maintain()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, detail FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert dict(row) == {"state": "deleting", "detail": "Saved by an environment test"}


async def test_objects_a_bucket_will_not_delete_never_hold_back_the_rest(store, pool):
    ids = [f"{index:032x}" for index in range(25)]
    async with pool.acquire() as conn:
        for index, snapshot_id in enumerate(ids):
            await conn.execute(
                "INSERT INTO agent_environment_snapshots "
                "(id, user_id, environment_id, revision, setup_fingerprint, state, object_key, updated_at) "
                "VALUES ($1, 'alice', 'env', 1, $2, 'deleting', $3, NOW() - $4 * INTERVAL '1 minute')",
                snapshot_id,
                "f" * 64,
                snapshot_object_key(owner_hash("alice"), snapshot_id),
                25 - index,
            )
    snapshots = store.environment_snapshots
    first = {d["snapshot_id"] for d in await snapshots.deletions()}
    assert first == set(ids[:20])
    # Those just handed out wait behind the ones not yet tried.
    second = {d["snapshot_id"] for d in await snapshots.deletions()}
    assert len(second) == 20 and set(ids[20:]) <= second


async def _configured(store, monkeypatch):
    """An environment whose install reads a setup Secret, beside global variables of each phase."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _save(
        store,
        repo=None,
        name="Global",
        variables=[
            {"name": "REGISTRY", "value": "https://registry.example", "secret": False},
            {"name": "LOG_LEVEL", "value": "info", "secret": False, "phase": "runtime"},
        ],
    )
    return await _save(store, script="npm ci", variables=[SETUP_TOKEN, RUNTIME_TOKEN])


async def _build(store, binding: str, *, repo: str = "org/repo") -> str:
    """Queue, claim and record one prebuild of ``binding``; return its snapshot."""
    snapshots = store.environment_snapshots
    snapshot_id = await snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo=repo, base_sha=SOURCE, requested=True
    )
    run = await _claim(store, capable=True)
    assert run is not None and run["snapshot_id"] == snapshot_id
    assert await snapshots.capture_target(run, "environment-host")
    assert await snapshots.record_capture(
        run_id=run["id"],
        host="environment-host",
        generation=run["lease_generation"],
        receipt=_receipt(snapshot_id),
    )
    return snapshot_id


#: Changes to what an installation installs with: its script, or a setup-time value.
_RETIRING = {
    "install script": {"setup_script": "npm ci --force"},
    "setup Secret rotated": {"edits": [{**SETUP_TOKEN, "value": "npm-token-after"}]},
    "setup Secret removed": {"edits": [{"name": "NPM_TOKEN", "operation": "remove"}]},
    "global setup variable": {
        "globals": [{"name": "REGISTRY", "value": "https://mirror.example", "secret": False}]
    },
}
#: Changes that install the same thing.
_KEEPING = {
    "start script": {"start_script": "npm start"},
    "runtime Secret rotated": {"edits": [{**RUNTIME_TOKEN, "value": "api-token-after"}]},
    "global runtime variable": {
        "globals": [{"name": "LOG_LEVEL", "value": "debug", "secret": False, "phase": "runtime"}]
    },
}


async def _change(store, env, change) -> None:
    """Publish ``change`` to ``env`` as it is now, or to its owner's global variables."""
    if "globals" in change:
        current = await store.environments.get_global_environment("alice")
        await _save(
            store,
            repo=None,
            name="Global",
            revision=current["revision"],
            variables=change["globals"],
        )
        return
    await _save_scripts(
        store,
        env,
        change.get("edits", ()),
        setup_script=change.get("setup_script", env["setup_script"]),
        start_script=change.get("start_script", env["start_script"]),
    )


@pytest.mark.parametrize("change", list(_RETIRING))
async def test_changing_what_an_installation_installs_with_retires_its_snapshot(
    store, pool, monkeypatch, change
):
    """A snapshot of an installation its owner has changed is retired, then deleted.

    New tasks are bound to the new fingerprint and never start from it, but it
    still holds what the old installation wrote: a rotated or removed setup
    Secret in an ``.npmrc``, say. It stayed stored, and on the hosts that had
    loaded it, until it went 7 days unused or for up to 25 days. It is no
    longer handed out, even to a task bound before the change, and is deleted
    after the grace a snapshot replaced by a newer build gets.
    """
    env = await _configured(store, monkeypatch)
    snapshot_id, binding, _run = await _ready_snapshot(store, pool, monkeypatch, env=env)
    snapshots = store.environment_snapshots
    await _change(store, env, _RETIRING[change])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, detail FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert dict(row) == {"state": "superseded", "detail": "The installation changed"}
    assert (
        await snapshots.snapshot_for_claim(user_id="alice", binding_id=binding, base_image=IMAGE)
        is None
    )
    # Workspaces already starting from it get the same grace period.
    await snapshots.maintain()
    assert await snapshots.deletions() == []
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET updated_at=NOW()-INTERVAL '25 hours' WHERE id=$1",
            snapshot_id,
        )
    await snapshots.maintain()
    assert [d["snapshot_id"] for d in await snapshots.deletions()] == [snapshot_id]
    assert await snapshots.retired_images([snapshot_id]) == [
        {"snapshot_id": snapshot_id, "owner": owner_hash("alice")}
    ]
    async with pool.acquire() as conn:
        detail = await conn.fetchval(
            "SELECT detail FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert detail == "The installation changed"


@pytest.mark.parametrize("change", list(_KEEPING))
async def test_a_change_that_installs_the_same_keeps_the_snapshot(store, pool, monkeypatch, change):
    env = await _configured(store, monkeypatch)
    snapshot_id, _binding_id, _run = await _ready_snapshot(store, pool, monkeypatch, env=env)
    await _change(store, env, _KEEPING[change])
    later = await _binding(store, pool, (await store.environments.list_environments("alice"))[0])
    found = await store.environment_snapshots.snapshot_for_claim(
        user_id="alice", binding_id=later, base_image=IMAGE
    )
    assert found is not None and found["snapshot_id"] == snapshot_id


async def test_a_global_change_retires_the_changed_installations_of_its_owner_alone(
    store, pool, monkeypatch
):
    """Global variables feed every environment of their owner, and no one else's."""
    env = await _configured(store, monkeypatch)
    first, _binding_id, _run = await _ready_snapshot(store, pool, monkeypatch, env=env)
    other = await _save(store, repo="org/other", name="Other", script="pip install -r reqs.txt")
    second = await _build(store, await _binding(store, pool, other), repo="org/other")
    bobs = "0" * 32
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_environment_snapshots (id, user_id, environment_id, revision, "
            "setup_fingerprint, state, base_image, source_sha, object_key, sha256, size_bytes, "
            "ready_at) VALUES ($1, 'bob', 'bob-environment', 1, $2, 'ready', $3, $4, $5, $6, "
            "1024, NOW())",
            bobs,
            "f" * 64,
            IMAGE,
            SOURCE,
            snapshot_object_key(owner_hash("bob"), bobs),
            "e" * 64,
        )
    await _change(store, env, _RETIRING["global setup variable"])
    async with pool.acquire() as conn:
        states = dict(await conn.fetch("SELECT id, state FROM agent_environment_snapshots"))
    assert states == {first: "superseded", second: "superseded", bobs: "ready"}


async def test_a_build_of_an_installation_changed_while_it_ran_is_retired_on_receipt(
    store, pool, monkeypatch
):
    """A prebuild that installed with the old values never becomes a usable snapshot.

    It was queued before the change, or for a task or test bound before it,
    so its receipt arrives after the change was published. The object is
    recorded, so the usual cleanup deletes it, but it is retired at once.
    """
    snapshot_id, run, env, binding = await _building(store, pool, monkeypatch)
    await _save(store, environment_id=env["id"], revision=env["revision"], script="npm ci --force")
    assert await store.environment_snapshots.record_capture(
        run_id=run["id"],
        host="environment-host",
        generation=run["lease_generation"],
        receipt=_receipt(snapshot_id),
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, detail, object_key FROM agent_environment_snapshots WHERE id=$1",
            snapshot_id,
        )
    assert (row["state"], row["detail"]) == ("superseded", "The installation changed")
    assert row["object_key"] == snapshot_object_key(owner_hash("alice"), snapshot_id)
    assert (
        await store.environment_snapshots.snapshot_for_claim(
            user_id="alice", binding_id=binding, base_image=IMAGE
        )
        is None
    )


async def test_a_snapshot_of_a_tasks_own_install_script_follows_that_script(
    store, pool, monkeypatch
):
    """A task may replace its environment's install script, and its snapshot installs that.

    Such a snapshot never carries its environment's fingerprint, so it is
    checked against the task's own script with the current values: a new
    environment script leaves it, a rotated setup Secret retires it.
    """
    env = await _configured(store, monkeypatch)
    await _enable_archives(store)
    await _host(store)
    job = await store.conversation_commands.create_job(
        user_id="alice",
        repo=env["repo"],
        task_prompt="work",
        runtime="claude-code",
        model="test-model",
        environment_selection={
            "environment_id": env["id"],
            "environment_id_provided": True,
            "environment_revision": env["revision"],
            "has_setup_override": True,
            "setup_override": "npm ci --ignore-scripts",
        },
    )
    async with pool.acquire() as conn:
        binding = await conn.fetchval(
            "SELECT environment_binding_id FROM agent_threads WHERE id=$1", job["thread_id"]
        )
    snapshot_id = await _build(store, binding)
    snapshots = store.environment_snapshots

    async def handed_out() -> str | None:
        found = await snapshots.snapshot_for_claim(
            user_id="alice", binding_id=binding, base_image=IMAGE
        )
        return found["snapshot_id"] if found else None

    assert await handed_out() == snapshot_id
    changed = await _save(
        store, environment_id=env["id"], revision=env["revision"], script="npm ci --force"
    )
    assert await handed_out() == snapshot_id
    await _change(store, changed, _RETIRING["setup Secret rotated"])
    assert await handed_out() is None


async def _retires_nothing(conn, user_id, environment_ids=None) -> None:
    """Publish as releases did before a publish retired the snapshots it changed."""


async def _state_of(pool, snapshot_id: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, detail FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    return dict(row)


async def _stale_snapshot(pool, owner: str, environment_id: str) -> str:
    """A ready snapshot of an installation ``environment_id`` does not have now."""
    snapshot_id = secrets.token_hex(16)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_environment_snapshots (id, user_id, environment_id, revision, "
            "setup_fingerprint, state, base_image, source_sha, object_key, sha256, size_bytes, "
            "ready_at) VALUES ($1, $2, $3, 1, $4, 'ready', $5, $6, $7, $8, 1024, NOW())",
            snapshot_id,
            owner,
            environment_id,
            "f" * 64,
            IMAGE,
            SOURCE,
            snapshot_object_key(owner_hash(owner), snapshot_id),
            "e" * 64,
        )
    return snapshot_id


@pytest.mark.parametrize("change", list(_RETIRING))
async def test_maintenance_retires_a_snapshot_whose_change_no_publish_retired(
    store, pool, monkeypatch, change
):
    """A snapshot an earlier release left stale is retired by maintenance, then deleted.

    Publishing a configuration and recording a build retire the snapshot of an
    installation that changed. Releases before them did not, and neither looks
    again at an environment that is never published again. Its snapshot stayed
    ready, still handed out to tasks bound before the change and holding the
    rotated or removed setup Secret, until it expired up to 25 days later. One
    maintenance pass retires it, and the grace every retired snapshot gets ends
    in its deletion.
    """
    env = await _configured(store, monkeypatch)
    snapshot_id, binding, _run = await _ready_snapshot(store, pool, monkeypatch, env=env)
    snapshots = store.environment_snapshots
    with monkeypatch.context() as earlier_release:
        earlier_release.setattr(environments_module, "retire_changed_snapshots", _retires_nothing)
        await _change(store, env, _RETIRING[change])

    async def handed_out() -> str | None:
        found = await snapshots.snapshot_for_claim(
            user_id="alice", binding_id=binding, base_image=IMAGE
        )
        return found["snapshot_id"] if found else None

    assert await handed_out() == snapshot_id
    await snapshots.maintain()
    assert await _state_of(pool, snapshot_id) == {
        "state": "superseded",
        "detail": "The installation changed",
    }
    assert await handed_out() is None
    assert await snapshots.deletions() == []
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET updated_at=NOW()-INTERVAL '25 hours' WHERE id=$1",
            snapshot_id,
        )
    await snapshots.maintain()
    assert [d["snapshot_id"] for d in await snapshots.deletions()] == [snapshot_id]
    assert await snapshots.retired_images([snapshot_id]) == [
        {"snapshot_id": snapshot_id, "owner": owner_hash("alice")}
    ]


@pytest.mark.parametrize("change", list(_KEEPING))
async def test_maintenance_keeps_a_snapshot_whose_installation_did_not_change(
    store, pool, monkeypatch, change
):
    """Maintenance retires a snapshot only once its fingerprint no longer matches.

    A change that installs the same thing leaves the fingerprint current, even
    published with no retirement, and so does an environment nobody changed.
    """
    env = await _configured(store, monkeypatch)
    first, _binding_id, _run = await _ready_snapshot(store, pool, monkeypatch, env=env)
    other = await _save(store, repo="org/other", name="Other", script="pip install -r reqs.txt")
    second = await _build(store, await _binding(store, pool, other), repo="org/other")
    with monkeypatch.context() as earlier_release:
        earlier_release.setattr(environments_module, "retire_changed_snapshots", _retires_nothing)
        await _change(store, env, _KEEPING[change])
    await store.environment_snapshots.maintain()
    async with pool.acquire() as conn:
        states = dict(await conn.fetch("SELECT id, state FROM agent_environment_snapshots"))
    assert states == {first: "ready", second: "ready"}


async def test_maintenance_checks_a_bounded_batch_of_owners_and_comes_back_around(
    store, pool, monkeypatch
):
    """Each pass checks the next few owners, so it stays short however many there are.

    The next pass goes on after the last owner checked, and the pass after the
    last owner starts again from the first.
    """
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    monkeypatch.setattr(snapshots_module, "OWNERS_CHECKED_PER_PASS", 2)
    environments = {}
    for owner in ("alice", "bob", "carol"):
        environments[owner] = (await _save(store, owner=owner, script="npm ci"))["id"]
        await _stale_snapshot(pool, owner, environments[owner])
    snapshots = store.environment_snapshots

    async def ready() -> set[str]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id FROM agent_environment_snapshots WHERE state='ready'"
            )
        return {row["user_id"] for row in rows}

    await snapshots.maintain()
    assert await ready() == {"carol"}
    await snapshots.maintain()
    assert await ready() == set()
    await _stale_snapshot(pool, "alice", environments["alice"])
    await snapshots.maintain()
    assert await ready() == set()
