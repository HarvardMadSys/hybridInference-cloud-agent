"""Publication preflight retries preserve immutable artifacts on real Postgres."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from tests.integration.storage.test_agent_job_store import (
    _SHARED_HOST_CAPABILITIES,
    _claim,
    _create_job,
    _shared_claim_attestation,
    pool as pool,
    store as store,
)

from cloud_agent import publish_worker
from cloud_agent.entitlement import RepoAccessUnavailable
from cloud_agent.publish_worker import GitHubCredential
from cloud_agent.publisher import PublishError, PublishResult
from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.snapshot import PUBLISH_MAX_ATTEMPTS

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _capture_settled_workspace(store, key: str) -> tuple[dict, dict, str]:
    """Run a distinct shared turn through settlement and immutable capture."""
    host = f"publish-retry-{key}"
    credential_hash = f"credential-{key}"
    await store.host_pool.register_host(
        host=host,
        credential_hash=credential_hash,
        capabilities=_SHARED_HOST_CAPABILITIES,
        slots_total=4,
    )
    await store.host_pool.record_host_heartbeat(
        host=host,
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )
    job = await _create_job(store, user_id=f"owner-{key}", base_sha="a" * 40)
    claim = await _claim(
        store,
        worker_id=f"worker-{key}",
        lease_ttl_seconds=60,
        host=host,
        host_credential_hash=credential_hash,
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=host,
        sandbox_id=f"sandbox-publish-{key}",
        container_name=f"container-publish-{key}",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
    )
    assert sandbox is not None
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id=f"container-{key}", context_status="new"
    )
    residency = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=host,
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
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence,
        job_id=job["id"],
        residency_generation=residency["generation"],
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=residency["generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id=f"native-{key}",
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
    # The owner publishes the finished turn; a turn boundary publishes nothing.
    requested = await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )
    assert requested.get("requested") is True, requested
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None and capture["anchor_job_id"] == job["id"]
    patch = f"diff --git a/answer-{key}.txt b/answer-{key}.txt\nnew file mode 100644\n"
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
    return job, capture, patch


async def _insert_later_snapshot(pool, *, job: dict, capture: dict, patch: str) -> int:
    """Arrange a later captured epoch on the same lineage."""
    async with pool.acquire() as conn, conn.transaction():
        snapshot_id = await conn.fetchval(
            """
            INSERT INTO agent_sandbox_snapshots (
                sandbox_id, workspace_epoch, anchor_job_id, base_sha,
                content_sha256, patch_content, state, captured_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'captured', NOW())
            RETURNING id
            """,
            capture["sandbox_id"],
            capture["workspace_epoch"] + 1,
            job["id"],
            capture["base_sha"],
            hashlib.sha256(patch.encode()).hexdigest(),
            patch,
        )
        await conn.execute(
            "INSERT INTO agent_snapshot_jobs (snapshot_id, job_id) VALUES ($1, $2)",
            snapshot_id,
            job["id"],
        )
    return int(snapshot_id)


async def _allow_publication(monkeypatch) -> None:
    """Skip the deployment allowlist; these tests are about the publish queue."""

    async def entitled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(publish_worker, "require_entitled_repo", entitled)


def _push(**kwargs):
    return PublishResult(
        branch=f"agent/{kwargs['branch_id']}", commit_sha="b" * 40, changed_files=["answer"]
    )


async def _import_base(store, *, snapshot, commit_sha, bundle):
    return await store.snapshot.complete_snapshot_base_import(
        snapshot_id=snapshot["snapshot_id"], commit_sha=commit_sha
    )


async def test_entitlement_outage_preserves_snapshot_and_later_work_then_retries_once(
    store, pool, monkeypatch
):
    first, capture, patch = await _capture_settled_workspace(store, "a")
    later, later_capture, _ = await _capture_settled_workspace(store, "b")
    assert not await store.snapshot.defer_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=0,
        detail="not claimed",
    ), "a captured artifact has no publishing claim to release"
    assert await store.snapshot.next_snapshot_publish_retry_due() is None

    unavailable = True
    checked: list[str] = []
    pushed: list[str] = []
    opened: list[str] = []

    async def entitlement(repo, user_id, **kwargs):
        checked.append(user_id)
        if user_id == first["user_id"] and unavailable:
            raise RepoAccessUnavailable("temporary repository lookup outage " * 100)

    def push(**kwargs):
        pushed.append(kwargs["job_id"])
        return PublishResult(
            branch=f"agent/{kwargs['branch_id']}", commit_sha="b" * 40, changed_files=["answer"]
        )

    async def pull_request(**kwargs):
        opened.append(kwargs["head_branch"])
        return f"https://example.test/pull/{len(opened)}"

    async def import_base(store, *, snapshot, commit_sha, bundle):
        return await store.snapshot.complete_snapshot_base_import(
            snapshot_id=snapshot["snapshot_id"], commit_sha=commit_sha
        )

    monkeypatch.setattr(publish_worker, "require_entitled_repo", entitlement)
    monkeypatch.setattr(publish_worker, "publish_patch", push)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", pull_request)
    monkeypatch.setattr(publish_worker, "_import_snapshot_base", import_base)

    async def publish_due():
        await publish_worker._publish_due(
            store,
            credential=GitHubCredential("local-test-token"),
            app_credentials=None,
            base_branch="dev",
        )

    await publish_due()
    assert checked == [first["user_id"], later["user_id"]]
    assert pushed == [later["id"]]
    assert len(opened) == 1
    due = await store.snapshot.next_snapshot_publish_retry_due()
    assert (
        due is not None
        and 25 < (due - await pool.fetchval("SELECT clock_timestamp()")).total_seconds() <= 30
    )
    assert 0 < await publish_worker._next_look_s(store) <= 30
    async with pool.acquire() as conn:
        retained = await conn.fetchrow(
            "SELECT state, patch_content, content_sha256, last_error, retry_at "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
        assert retained["state"] == "captured"
        assert retained["patch_content"] == patch
        assert retained["content_sha256"] == hashlib.sha256(patch.encode()).hexdigest()
        assert len(retained["last_error"]) == 2000
        assert retained["retry_at"] == due
        assert (
            await conn.fetchval(
                "SELECT job_id FROM agent_snapshot_jobs WHERE snapshot_id = $1",
                capture["snapshot_id"],
            )
            == first["id"]
        )
        assert (
            await conn.fetchval(
                "SELECT state FROM agent_jobs WHERE id = $1",
                first["id"],
            )
            == "succeeded"
        )
        assert (
            await conn.fetchval(
                "SELECT state FROM agent_sandbox_snapshots WHERE id = $1",
                later_capture["snapshot_id"],
            )
            == "published"
        )
    assert await store.snapshot.reap_stalled_snapshot_publishes(stall_seconds=0) == []

    unavailable = False
    await publish_due()
    assert pushed == [later["id"]], "recovery still waits for its scheduled retry"
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET retry_at = NOW() WHERE id = $1",
            capture["snapshot_id"],
        )
    await publish_due()
    await publish_due()
    assert checked == [first["user_id"], later["user_id"], first["user_id"]]
    assert pushed == [later["id"], first["id"]]
    assert len(opened) == 2
    assert await store.snapshot.next_snapshot_publish_retry_due() is None
    assert not await store.snapshot.defer_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=1,
        detail="stale preflight",
    ), "a late failure cannot requeue a published artifact"
    async with pool.acquire() as conn:
        final = await conn.fetchrow(
            "SELECT state, retry_at, last_error FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
        assert dict(final) == {"state": "published", "retry_at": None, "last_error": None}
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_jobs WHERE id = ANY($1::text[]) AND state = 'succeeded'",
                [first["id"], later["id"]],
            )
            == 2
        )


async def test_transient_pr_failure_is_claimable_again_after_retry_at(store, pool, monkeypatch):
    """A 502 after the push returns the snapshot to the queue, then it publishes."""
    _job, capture, patch = await _capture_settled_workspace(store, "transient")
    await _allow_publication(monkeypatch)
    calls = {"pr": 0}

    async def pull_request(**_kwargs):
        calls["pr"] += 1
        if calls["pr"] == 1:
            raise PublishError("GitHub refused the draft PR (502): bad gateway")
        return "https://example.test/pull/1"

    monkeypatch.setattr(publish_worker, "publish_patch", _push)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", pull_request)
    monkeypatch.setattr(publish_worker, "_import_snapshot_base", _import_base)

    await publish_worker._publish_due(
        store,
        credential=GitHubCredential("local-test-token"),
        app_credentials=None,
        base_branch="dev",
    )
    assert calls["pr"] == 1
    async with pool.acquire() as conn:
        retained = await conn.fetchrow(
            "SELECT state, publish_attempt_count, retry_at, patch_content, last_error "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
    assert retained["state"] == "captured"
    assert retained["publish_attempt_count"] == 1
    assert retained["patch_content"] == patch
    assert "502" in retained["last_error"]
    assert retained["retry_at"] is not None
    assert (
        25
        < (retained["retry_at"] - await pool.fetchval("SELECT clock_timestamp()")).total_seconds()
        <= 30
    )
    assert await store.snapshot.claim_for_publish() is None

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET retry_at = NOW() WHERE id = $1",
            capture["snapshot_id"],
        )
    claimed = await store.snapshot.claim_for_publish()
    assert claimed is not None
    assert claimed["snapshot_id"] == capture["snapshot_id"]
    assert claimed["publish_attempt_count"] == 1

    url = await publish_worker._publish_claimed(
        store,
        claimed,
        credential=GitHubCredential("local-test-token"),
        app_credentials=None,
        base_branch="dev",
    )
    assert url == "https://example.test/pull/1"
    assert calls["pr"] == 2
    async with pool.acquire() as conn:
        final = await conn.fetchrow(
            "SELECT state, publish_attempt_count, retry_at, last_error "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
    assert dict(final) == {
        "state": "published",
        "publish_attempt_count": 1,
        "retry_at": None,
        "last_error": None,
    }


async def test_a_live_heartbeat_after_the_reaper_scan_fences_recovery(store, pool, monkeypatch):
    """The reaper must recheck staleness atomically after selecting a claim."""
    _job, capture, _patch = await _capture_settled_workspace(store, "live-heartbeat")
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_sandbox_snapshots
            SET updated_at = clock_timestamp() - INTERVAL '2 hours'
            WHERE id = $1
            """,
            capture["snapshot_id"],
        )

    selected = asyncio.Event()
    continue_reaping = asyncio.Event()
    defer_stalled = store.snapshot._defer_snapshot_publish

    async def pause_after_scan(**kwargs):
        selected.set()
        await continue_reaping.wait()
        return await defer_stalled(**kwargs)

    monkeypatch.setattr(store.snapshot, "_defer_snapshot_publish", pause_after_scan)
    reaping = asyncio.create_task(store.snapshot.reap_stalled_snapshot_publishes(stall_seconds=900))
    await asyncio.wait_for(selected.wait(), timeout=1)
    assert await store.snapshot.renew_snapshot_publish_claim(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
    )
    continue_reaping.set()

    assert await reaping == []
    async with pool.acquire() as conn:
        current = await conn.fetchrow(
            "SELECT state, publish_attempt_count FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
    assert dict(current) == {
        "state": "publishing",
        "publish_attempt_count": publication["publish_attempt_count"],
    }


async def test_stalled_publish_is_reclaimed_after_restart_without_skipping_lineage(store, pool):
    """A crash spends one durable defer and a fresh process resumes the same epoch."""
    job, capture, patch = await _capture_settled_workspace(store, "restart")
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    assert publication["snapshot_id"] == capture["snapshot_id"]
    later_snapshot_id = await _insert_later_snapshot(pool, job=job, capture=capture, patch=patch)

    assert await store.snapshot.reap_stalled_snapshot_publishes(stall_seconds=0) == [
        capture["snapshot_id"]
    ]
    async with pool.acquire() as conn:
        retained = await conn.fetchrow(
            """
            SELECT state, patch_content, retry_at, publish_attempt_count
            FROM agent_sandbox_snapshots WHERE id = $1
            """,
            capture["snapshot_id"],
        )
        group_state = await conn.fetchval(
            "SELECT snapshot_state FROM agent_sandboxes WHERE id = $1",
            capture["sandbox_id"],
        )
    assert retained["state"] == "captured"
    assert retained["patch_content"] == patch
    assert retained["retry_at"] is not None
    assert retained["publish_attempt_count"] == 1
    assert group_state == "quiescent"

    restarted = Storage(pool)
    assert await restarted.snapshot.claim_for_publish() is None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET retry_at = NOW() WHERE id = $1",
            capture["snapshot_id"],
        )
    reclaimed = await restarted.snapshot.claim_for_publish()
    assert reclaimed is not None
    assert reclaimed["snapshot_id"] == capture["snapshot_id"]
    assert await restarted.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=reclaimed["publish_attempt_count"],
        pr_url="https://example.test/pull/restarted",
        commit_sha="c" * 40,
    )
    later = await restarted.snapshot.claim_for_publish()
    assert later is not None
    assert later["snapshot_id"] == later_snapshot_id


async def test_reclaimed_publish_rejects_every_stale_publisher_write(store, pool):
    """A publisher superseded by the reaper cannot mutate its replacement."""
    _, capture, _ = await _capture_settled_workspace(store, "stale-writer")
    stale = await store.snapshot.claim_for_publish()
    assert stale is not None
    assert stale["publish_attempt_count"] == 0

    assert await store.snapshot.reap_stalled_snapshot_publishes(stall_seconds=0) == [
        capture["snapshot_id"]
    ]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET retry_at = NOW() WHERE id = $1",
            capture["snapshot_id"],
        )
    current = await store.snapshot.claim_for_publish()
    assert current is not None
    assert current["publish_attempt_count"] == stale["publish_attempt_count"] + 1

    assert not await store.snapshot.renew_snapshot_publish_claim(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=stale["publish_attempt_count"],
    )
    assert await store.snapshot.renew_snapshot_publish_claim(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=current["publish_attempt_count"],
    )
    assert not await store.snapshot.defer_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=stale["publish_attempt_count"],
        detail="stale retry",
    )
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=stale["publish_attempt_count"],
        detail="stale failure",
    )
    assert not await store.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=stale["publish_attempt_count"],
        pr_url="https://example.test/pull/stale",
        commit_sha="d" * 40,
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, publish_attempt_count, published_pr_url "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
    assert dict(row) == {
        "state": "publishing",
        "publish_attempt_count": current["publish_attempt_count"],
        "published_pr_url": None,
    }

    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=current["publish_attempt_count"],
        pr_url="https://example.test/pull/current",
        commit_sha="e" * 40,
    )


async def test_the_sixth_transient_failure_fails_instead_of_deferring(store, pool, monkeypatch):
    """Five recorded defers is the budget; the next 503 fails the snapshot.

    The failed snapshot no longer holds the later one back: the same pass goes
    on to it, and it spends a retry of its own on the same outage.
    """
    job, capture, patch = await _capture_settled_workspace(store, "budget")
    later_snapshot_id = await _insert_later_snapshot(pool, job=job, capture=capture, patch=patch)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET publish_attempt_count = $2 WHERE id = $1",
            capture["snapshot_id"],
            PUBLISH_MAX_ATTEMPTS,
        )
    await _allow_publication(monkeypatch)

    async def pull_request(**_kwargs):
        raise PublishError("GitHub refused the PR lookup (503)")

    monkeypatch.setattr(publish_worker, "publish_patch", _push)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", pull_request)
    monkeypatch.setattr(publish_worker, "_import_snapshot_base", _import_base)

    await publish_worker._publish_due(
        store,
        credential=GitHubCredential("local-test-token"),
        app_credentials=None,
        base_branch="dev",
    )
    async with pool.acquire() as conn:
        failed = await conn.fetchrow(
            "SELECT state, publish_attempt_count, retry_at, last_error "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
        group = await conn.fetchrow(
            "SELECT snapshot_state, idle_deadline FROM agent_sandboxes WHERE id = $1",
            capture["sandbox_id"],
        )
        later = await conn.fetchrow(
            "SELECT state, publish_attempt_count, retry_at IS NOT NULL AS waits "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            later_snapshot_id,
        )
        event_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM agent_job_events
            WHERE job_id = $1
              AND payload->>'phase' = 'shared_snapshot_publish_retry_exhausted'
            """,
            job["id"],
        )
    assert failed["state"] == "failed"
    assert failed["publish_attempt_count"] == PUBLISH_MAX_ATTEMPTS
    assert failed["retry_at"] is None
    assert "503" in failed["last_error"]
    assert "retry limit" in failed["last_error"].lower()
    assert group["snapshot_state"] == "dirty"
    assert group["idle_deadline"] is not None
    assert dict(later) == {"state": "captured", "publish_attempt_count": 1, "waits": True}, (
        "the later snapshot was tried next, and deferred by the same outage"
    )
    assert event_count == 1
    assert await store.snapshot.claim_for_publish() is None, (
        "the later one waits for its retry, and the failed one is never retried"
    )


async def test_a_failed_earlier_snapshot_no_longer_blocks_the_lineage(store, pool):
    """A terminal publication failure is superseded by the later snapshot.

    The later one was cut from the same published base, so it carries the
    failed one's changes; holding it back left the group unpublishable for
    good. The failed one stays failed, with its reason, and is not retried.
    """
    job, capture, _patch = await _capture_settled_workspace(store, "lineage")
    claimed = await store.snapshot.claim_for_publish()
    assert claimed is not None and claimed["snapshot_id"] == capture["snapshot_id"]
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=claimed["publish_attempt_count"],
        detail="publisher rejected the immutable patch",
    )
    later_patch = "diff --git a/later.txt b/later.txt\nnew file mode 100644\n"
    async with pool.acquire() as conn, conn.transaction():
        later_id = await conn.fetchval(
            """
            INSERT INTO agent_sandbox_snapshots (
                sandbox_id, workspace_epoch, anchor_job_id, base_sha,
                patch_content, content_sha256, state
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'captured')
            RETURNING id
            """,
            capture["sandbox_id"],
            capture["workspace_epoch"] + 1,
            job["id"],
            job["base_sha"],
            later_patch,
            hashlib.sha256(later_patch.encode()).hexdigest(),
        )
        await conn.execute(
            "INSERT INTO agent_snapshot_jobs (snapshot_id, job_id) VALUES ($1, $2)",
            later_id,
            job["id"],
        )

    later = await store.snapshot.claim_for_publish()
    assert later is not None and later["snapshot_id"] == later_id
    assert later["patch"] == later_patch
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT state FROM agent_sandbox_snapshots WHERE id = $1", later_id)
            == "publishing"
        )
        failed = await conn.fetchrow(
            "SELECT state, last_error FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
    assert dict(failed) == {
        "state": "failed",
        "last_error": "publisher rejected the immutable patch",
    }
    assert await store.snapshot.claim_for_publish() is None


async def test_a_push_recorded_before_its_failed_pull_request_reaches_the_next_claim(store, pool):
    """The next publication from that base builds on the push instead of refusing it.

    The record is fenced like every other publisher write, and survives the
    failure that follows it; the group's published base does not move.
    """
    job, capture, _patch = await _capture_settled_workspace(store, "pushed")
    claimed = await store.snapshot.claim_for_publish()
    assert claimed is not None and claimed["own_pushes"] == []
    pushed = "c" * 40
    assert not await store.snapshot.record_snapshot_push(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=claimed["publish_attempt_count"] + 1,
        commit_sha=pushed,
    )
    assert await store.snapshot.record_snapshot_push(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=claimed["publish_attempt_count"],
        commit_sha=pushed,
    )
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=claimed["publish_attempt_count"],
        detail="GitHub refused the draft PR (422)",
    )
    later_id = await _insert_later_snapshot(
        pool, job=job, capture=capture, patch="diff --git a/later.txt b/later.txt\n"
    )

    later = await store.snapshot.claim_for_publish()

    assert later is not None and later["snapshot_id"] == later_id
    assert later["own_pushes"] == [pushed]
    async with pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT published_base_sha, snapshot_state FROM agent_sandboxes WHERE id = $1",
            capture["sandbox_id"],
        )
    assert group["published_base_sha"] != pushed
    assert group["snapshot_state"] == "dirty"
    async with pool.acquire() as conn:
        failed = await conn.fetchrow(
            "SELECT pushed_commit_sha, published_commit_sha FROM agent_sandbox_snapshots "
            "WHERE id = $1",
            capture["snapshot_id"],
        )
    # Kept apart from the published commit, which only publication sets.
    assert dict(failed) == {"pushed_commit_sha": pushed, "published_commit_sha": None}


@pytest.mark.parametrize(
    ("delay_s", "waits"),
    [(None, 30.0), (5.0, 30.0), (600.0, 600.0), (86_400.0, 3600.0)],
)
async def test_a_rate_limited_publication_waits_as_long_as_github_asks(store, pool, delay_s, waits):
    """At least the usual interval, as long as GitHub asked, and never past the hour."""
    _job, capture, _patch = await _capture_settled_workspace(store, f"wait-{delay_s}")
    claimed = await store.snapshot.claim_for_publish()
    assert claimed is not None and claimed["snapshot_id"] == capture["snapshot_id"]

    assert await store.snapshot.defer_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=claimed["publish_attempt_count"],
        detail="GitHub refused the PR lookup (403)",
        delay_s=delay_s,
    )

    async with pool.acquire() as conn:
        wait = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM retry_at - clock_timestamp()) "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            capture["snapshot_id"],
        )
    assert waits - 5 < float(wait) <= waits
    assert await store.snapshot.claim_for_publish() is None
