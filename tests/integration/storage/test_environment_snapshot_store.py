"""Real PostgreSQL lifecycle of installation snapshots: build, receipt, use and retirement."""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
import pytest
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _SHARED_HOST_CAPABILITIES,
    _shared_claim_attestation,
    pool,
    store,
)
from tests.integration.storage.test_environment_store import _job, _save

from cloud_agent_shared.environment_snapshots import owner_hash, snapshot_object_key

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

IMAGE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
OTHER_IMAGE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "b" * 64
SOURCE = "c" * 40
LOCKFILES = {"package-lock.json": "d" * 40}


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
    # A test while a build of the same installation is pending saves nothing.
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


async def test_a_changed_installation_is_never_matched(store, pool, monkeypatch):
    snapshot_id, _, _run = await _ready_snapshot(store, pool, monkeypatch)
    env = (await store.environments.list_environments("alice"))[0]
    changed = await _save(
        store, environment_id=env["id"], revision=env["revision"], script="npm ci --force"
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
    # A start script change installs the same thing, so the snapshot still serves.
    start_only = await _save(
        store,
        environment_id=env["id"],
        revision=changed["revision"],
        script="npm ci",
    )
    same = await _binding(store, pool, start_only)
    assert (
        await store.environment_snapshots.snapshot_for_claim(
            user_id="alice", binding_id=same, base_image=IMAGE
        )
    )["snapshot_id"] == snapshot_id


async def test_use_keeps_a_snapshot_and_shortfalls_queue_a_build(store, pool, monkeypatch):
    snapshot_id, binding, _run = await _ready_snapshot(store, pool, monkeypatch)
    snapshots = store.environment_snapshots
    async with pool.acquire() as conn:
        job_id = await conn.fetchval(
            "SELECT j.id FROM agent_jobs j JOIN agent_threads t ON t.id=j.thread_id "
            "WHERE t.environment_binding_id=$1",
            binding,
        )
    assert await snapshots.record_use(
        job_id=job_id, snapshot_id=snapshot_id, outcome="used", base_sha=SOURCE
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT use_count, last_used_at FROM agent_environment_snapshots WHERE id=$1",
            snapshot_id,
        )
    assert row["use_count"] == 1 and row["last_used_at"] is not None
    await snapshots.record_use(
        job_id=job_id, snapshot_id=snapshot_id, outcome="lockfiles_changed", base_sha="f" * 40
    )
    async with pool.acquire() as conn:
        queued = await conn.fetchrow(
            "SELECT s.state, r.base_sha FROM agent_environment_snapshots s "
            "JOIN agent_environment_runs r ON r.id=s.run_id WHERE s.state='queued'"
        )
    assert queued["base_sha"] == "f" * 40
    await snapshots.record_use(
        job_id=job_id, snapshot_id=snapshot_id, outcome="invalid", base_sha=None
    )
    async with pool.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state FROM agent_environment_snapshots WHERE id=$1", snapshot_id
        )
    assert state == "deleting"
    with pytest.raises(ValueError):
        await snapshots.record_use(job_id=job_id, snapshot_id=None, outcome="used", base_sha=None)


async def test_a_newer_build_supersedes_and_retired_objects_are_deleted(store, pool, monkeypatch):
    first, _, _run = await _ready_snapshot(store, pool, monkeypatch)
    snapshots = store.environment_snapshots
    # A passing test of the same installation saves the next snapshot.
    env = (await store.environments.list_environments("alice"))[0]
    test = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha=SOURCE,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key="test",
    )
    run = await _claim(store, capable=True)
    assert run["id"] == test["id"]
    target = await snapshots.capture_target(run, "environment-host")
    assert target is not None
    assert await snapshots.record_capture(
        run_id=run["id"],
        host="environment-host",
        generation=run["lease_generation"],
        receipt=_receipt(target["snapshot_id"]),
    )
    async with pool.acquire() as conn:
        states = dict(
            await conn.fetch(
                "SELECT id, state FROM agent_environment_snapshots ORDER BY created_at"
            )
        )
    assert states == {first: "superseded", target["snapshot_id"]: "ready"}
    # Workspaces already starting from the old one get a grace period.
    await snapshots.maintain()
    assert all(d["object_key"] is None for d in await snapshots.deletions())
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET updated_at=NOW()-INTERVAL '25 hours' WHERE id=$1",
            first,
        )
    await snapshots.maintain()
    deletions = await snapshots.deletions()
    assert [d["snapshot_id"] for d in deletions if d["object_key"]] == [first]
    assert await snapshots.confirm_deleted(first)
    assert not await snapshots.confirm_deleted(first)
    # A deleted snapshot is still named for a while, so hosts drop its image.
    retired = next(d for d in await snapshots.deletions() if d["snapshot_id"] == first)
    assert retired["object_key"] is None


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
