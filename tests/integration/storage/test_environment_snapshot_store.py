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
