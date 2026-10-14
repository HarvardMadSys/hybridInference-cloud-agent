"""A failed workspace snapshot is superseded by the next one, on real Postgres.

Every capture is the whole diff from the group's published base, and a failure
never advances that base, so the snapshot a later turn produces carries the
failed one's changes, or their reversal. A failure therefore must not hold the
group back for good: the failed snapshot keeps its reason, the group leaves
``dirty`` when a later turn settles or a later snapshot is captured, and that
snapshot is captured and published as usual. Epochs still publish in order: a
snapshot that may yet publish holds every later one back, and one that failed
is never claimed again.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _reserve_shared_chat,
    _shared_claim_attestation,
    pool as pool,
    store as store,
)

from cloud_agent.storage.repositories.snapshot import PUBLISH_MAX_ATTEMPTS

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_CONTAINER = "container-supersession"
_REJECTED_PATCH = (
    "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\nnew file mode 100644\n"
)
_FIXED_PATCH = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"


async def _first_turn(store) -> tuple[dict, dict]:
    """One shared chat whose first turn has settled and succeeded."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id=_CONTAINER, context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-supersession",
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
    return job, sandbox


async def _next_turn(store, *, parent: dict, sandbox: dict, prompt: str) -> dict:
    """A follow-up in the same chat, run through admission and settlement."""
    job = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id=parent["user_id"], prompt=prompt
    )
    assert job is not None and job["state"] == "queued"
    claim = await _claim(
        store,
        worker_id=f"worker-{job['id']}",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None and claim["id"] == job["id"]
    activated = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        container_name=sandbox["container_name"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        sandbox_image_digest=sandbox["sandbox_image_digest"],
        execution_profile=sandbox["execution_profile"],
        disk_limit_mib=claim["thread_sandbox"]["disk_limit_mib"],
    )
    assert activated is not None
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": activated["generation"],
        "activation_generation": activated["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id=_CONTAINER, context_status="restored"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="restored",
        native_session_id="native-supersession",
        idle_timeout_seconds=1800,
        base_sha=parent["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    return job


async def _record(store, capture: dict, patch: str) -> str | None:
    """Hand the coordinator's claim the host's receipt for ``patch``."""
    return await store.snapshot.record_sandbox_snapshot_capture(
        snapshot_id=capture["snapshot_id"],
        sandbox_id=capture["sandbox_id"],
        sandbox_generation=capture["generation"],
        container_id=capture["container_id"],
        workspace_epoch=capture["workspace_epoch"],
        base_sha=capture["base_sha"],
        patch=patch,
        content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
    )


async def _publish(store, publication: dict, *, commit_sha: str) -> None:
    """Record the publisher's success and the base import that follows it."""
    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=publication["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        pr_url=f"https://example.test/pull/{publication['snapshot_id']}",
        commit_sha=commit_sha,
    )
    assert await store.snapshot.complete_snapshot_base_import(
        snapshot_id=publication["snapshot_id"], commit_sha=commit_sha
    )


async def _snapshot(pool, snapshot_id: int) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, workspace_epoch, base_sha, last_error, published_pr_url "
            "FROM agent_sandbox_snapshots WHERE id = $1",
            snapshot_id,
        )
    assert row is not None
    return dict(row)


async def _group_state(pool, sandbox_id: str) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT snapshot_state FROM agent_sandboxes WHERE id = $1", sandbox_id
        )


async def _fail(store, pool, mode: str, capture: dict) -> None:
    """End the claimed snapshot ``capture`` the way ``mode`` names, terminally."""
    if mode == "capture_refused":
        assert await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            detail="the workspace holds content the capture refused",
            dirty=True,
        )
        return
    assert await _record(store, capture, _REJECTED_PATCH) == "captured"
    if mode == "retries_exhausted":
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_sandbox_snapshots SET publish_attempt_count = $2 WHERE id = $1",
                capture["snapshot_id"],
                PUBLISH_MAX_ATTEMPTS,
            )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == capture["snapshot_id"]
    if mode == "retries_exhausted":
        assert await store.snapshot.defer_snapshot_publish(
            snapshot_id=capture["snapshot_id"],
            expected_publish_attempt_count=publication["publish_attempt_count"],
            detail="GitHub refused the PR lookup (503)",
        )
    else:
        await store.snapshot.fail_snapshot_publish(
            snapshot_id=capture["snapshot_id"],
            expected_publish_attempt_count=publication["publish_attempt_count"],
            detail="patch rejected: .github/workflows/ci.yml changes workflows",
        )


@pytest.mark.parametrize("mode", ["publish_rejected", "retries_exhausted", "capture_refused"])
async def test_the_next_settled_turn_supersedes_a_failed_snapshot(store, pool, mode):
    """A failure dirties the group; the next turn is captured and published anyway."""
    first, sandbox = await _first_turn(store)
    failed = await store.snapshot.claim_due_sandbox_snapshot()
    assert failed is not None
    await _fail(store, pool, mode, failed)

    failure = await _snapshot(pool, failed["snapshot_id"])
    assert failure["state"] == "failed" and failure["last_error"]
    assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"
    assert await store.snapshot.claim_due_sandbox_snapshot() is None, "nothing settled since"
    assert await store.snapshot.claim_for_publish() is None

    # The owner fixes the workspace in a later turn of the same chat.
    second = await _next_turn(store, parent=first, sandbox=sandbox, prompt="drop the workflow")
    assert await _group_state(pool, sandbox["sandbox_id"]) == "pending"

    superseding = await store.snapshot.claim_due_sandbox_snapshot()
    assert superseding is not None
    assert superseding["anchor_job_id"] == second["id"]
    assert superseding["workspace_epoch"] > failed["workspace_epoch"]
    # Cut from the base the failure never advanced, so it carries that work too.
    assert superseding["base_sha"] == failed["base_sha"]
    assert await _record(store, superseding, _FIXED_PATCH) == "captured"
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == superseding["snapshot_id"]
    assert publication["patch"] == _FIXED_PATCH
    await _publish(store, publication, commit_sha="c" * 40)

    published = await _snapshot(pool, superseding["snapshot_id"])
    assert published["state"] == "published"
    assert published["published_pr_url"] is not None
    assert await _group_state(pool, sandbox["sandbox_id"]) == "quiescent"
    # The failure stays on record, with its own turn and its own reason.
    assert await _snapshot(pool, failed["snapshot_id"]) == failure
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT array_agg(job_id) FROM agent_snapshot_jobs WHERE snapshot_id = $1",
            failed["snapshot_id"],
        ) == [first["id"]]
    assert await store.snapshot.claim_for_publish() is None, "a failed snapshot is never retried"


async def test_a_later_snapshot_still_carrying_the_change_fails_and_is_superseded_too(store, pool):
    """Refused again while the change is there; published once a turn removes it."""
    first, sandbox = await _first_turn(store)
    rejected = []
    parent = first
    for prompt in (None, "rename the variable"):
        if prompt is not None:
            parent = await _next_turn(store, parent=parent, sandbox=sandbox, prompt=prompt)
        capture = await store.snapshot.claim_due_sandbox_snapshot()
        assert capture is not None and capture["anchor_job_id"] == parent["id"]
        await _fail(store, pool, "publish_rejected", capture)
        assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"
        rejected.append(capture)

    third = await _next_turn(store, parent=parent, sandbox=sandbox, prompt="drop the workflow")
    superseding = await store.snapshot.claim_due_sandbox_snapshot()
    assert superseding is not None and superseding["anchor_job_id"] == third["id"]
    assert await _record(store, superseding, _FIXED_PATCH) == "captured"
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == superseding["snapshot_id"]
    await _publish(store, publication, commit_sha="b" * 40)

    states = [(await _snapshot(pool, capture["snapshot_id"]))["state"] for capture in rejected]
    assert states == ["failed", "failed"]
    assert (await _snapshot(pool, superseding["snapshot_id"]))["state"] == "published"


async def test_a_turn_that_settled_before_the_failure_is_captured_next(store, pool):
    """A pending later epoch is captured as soon as the earlier one fails.

    The later turn settled while the earlier snapshot was being published, so
    no further turn is coming to return the group from ``dirty`` to
    ``pending``; the capture of that later epoch does it.
    """
    first, sandbox = await _first_turn(store)
    earlier = await store.snapshot.claim_due_sandbox_snapshot()
    assert earlier is not None
    assert await _record(store, earlier, _REJECTED_PATCH) == "captured"
    second = await _next_turn(store, parent=first, sandbox=sandbox, prompt="drop the workflow")
    assert await _group_state(pool, sandbox["sandbox_id"]) == "pending"

    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == earlier["snapshot_id"]
    assert await store.snapshot.claim_due_sandbox_snapshot() is None, (
        "an earlier snapshot still on its way holds the later one back"
    )
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=earlier["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        detail="patch rejected: .github/workflows/ci.yml changes workflows",
    )
    assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"

    later = await store.snapshot.claim_due_sandbox_snapshot()
    assert later is not None and later["anchor_job_id"] == second["id"]
    assert later["workspace_epoch"] > earlier["workspace_epoch"]
    assert await _group_state(pool, sandbox["sandbox_id"]) == "snapshotting"
    assert await _record(store, later, _FIXED_PATCH) == "captured"
    assert await _group_state(pool, sandbox["sandbox_id"]) == "quiescent"
    later_publication = await store.snapshot.claim_for_publish()
    assert later_publication is not None
    assert later_publication["snapshot_id"] == later["snapshot_id"]
    await _publish(store, later_publication, commit_sha="d" * 40)
    assert (await _snapshot(pool, earlier["snapshot_id"]))["state"] == "failed"


async def test_an_older_epoch_never_publishes_after_a_newer_one(store, pool):
    """In flight, an earlier epoch holds the later one back; failed, it stays failed."""
    first, sandbox = await _first_turn(store)
    earlier = await store.snapshot.claim_due_sandbox_snapshot()
    assert earlier is not None
    assert await _record(store, earlier, _REJECTED_PATCH) == "captured"
    second = await _next_turn(store, parent=first, sandbox=sandbox, prompt="drop the workflow")

    # A deferred publication is still on its way, and still holds the next back.
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == earlier["snapshot_id"]
    assert await store.snapshot.defer_snapshot_publish(
        snapshot_id=earlier["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        detail="GitHub refused the PR lookup (503)",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET retry_at = NOW() WHERE id = $1",
            earlier["snapshot_id"],
        )
    assert await store.snapshot.claim_due_sandbox_snapshot() is None
    retried = await store.snapshot.claim_for_publish()
    assert retried is not None and retried["snapshot_id"] == earlier["snapshot_id"]
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=earlier["snapshot_id"],
        expected_publish_attempt_count=retried["publish_attempt_count"],
        detail="patch rejected: .github/workflows/ci.yml changes workflows",
    )

    later = await store.snapshot.claim_due_sandbox_snapshot()
    assert later is not None and later["anchor_job_id"] == second["id"]
    assert await _record(store, later, _FIXED_PATCH) == "captured"
    later_publication = await store.snapshot.claim_for_publish()
    assert later_publication is not None
    assert later_publication["snapshot_id"] == later["snapshot_id"]
    await _publish(store, later_publication, commit_sha="e" * 40)

    # The failed earlier epoch cannot be published, or queued again, after it.
    assert not await store.snapshot.record_snapshot_publish(
        snapshot_id=earlier["snapshot_id"],
        expected_publish_attempt_count=retried["publish_attempt_count"],
        pr_url="https://example.test/pull/stale",
        commit_sha="f" * 40,
    )
    assert not await store.snapshot.defer_snapshot_publish(
        snapshot_id=earlier["snapshot_id"],
        expected_publish_attempt_count=retried["publish_attempt_count"],
        detail="stale retry",
    )
    assert await store.snapshot.reap_stalled_snapshot_publishes(stall_seconds=0) == []
    assert await store.snapshot.claim_for_publish() is None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, state FROM agent_sandbox_snapshots WHERE sandbox_id = $1 "
            "ORDER BY workspace_epoch",
            sandbox["sandbox_id"],
        )
        base = await conn.fetchval(
            "SELECT published_base_sha FROM agent_sandboxes WHERE id = $1", sandbox["sandbox_id"]
        )
    assert [(row["id"], row["state"]) for row in rows] == [
        (earlier["snapshot_id"], "failed"),
        (later["snapshot_id"], "published"),
    ]
    assert base == "e" * 40, "the group's lineage advanced only by the newer epoch"
