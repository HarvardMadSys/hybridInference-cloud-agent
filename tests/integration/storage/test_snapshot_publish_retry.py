"""Publication preflight retries preserve immutable artifacts on real Postgres."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

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
from cloud_agent.publisher import PublishResult

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
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
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


async def test_entitlement_outage_preserves_snapshot_and_later_work_then_retries_once(
    store, pool, monkeypatch
):
    first, capture, patch = await _capture_settled_workspace(store, "a")
    later, later_capture, _ = await _capture_settled_workspace(store, "b")
    assert not await store.snapshot.defer_snapshot_publish(
        snapshot_id=capture["snapshot_id"], detail="not claimed"
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
    assert due is not None and 25 < (due - datetime.now(UTC)).total_seconds() <= 30
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
        snapshot_id=capture["snapshot_id"], detail="stale preflight"
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
