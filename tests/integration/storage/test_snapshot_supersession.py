"""A failed workspace snapshot is superseded by the owner's next request, on real Postgres.

Every capture is the whole diff from the group's published base, and a failure
never advances that base, so the snapshot the owner's next publication request
opens carries the failed one's changes, or their reversal. A failure therefore
must not hold the group back for good: the failed snapshot keeps its reason,
the group stays ``dirty`` until its owner asks again, and that request's
snapshot is captured and published as usual -- whether or not a turn settled in
between. A turn boundary publishes nothing. Epochs still publish in order: a
snapshot that may yet publish holds every later one back, and one that failed is
never claimed again.
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


async def _request(store, job: dict) -> dict:
    """The owner's publication request, as the conversation's button sends it."""
    return await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )


async def _included(pool, snapshot_id: int) -> list[str]:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT array_agg(job_id ORDER BY created_at) FROM agent_snapshot_jobs "
            "WHERE snapshot_id = $1",
            snapshot_id,
        )


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
async def test_the_owners_next_request_supersedes_a_failed_snapshot(store, pool, mode):
    """A failure dirties the group; the next request is captured and published anyway."""
    first, sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    failed = await store.snapshot.claim_due_sandbox_snapshot()
    assert failed is not None
    await _fail(store, pool, mode, failed)

    failure = await _snapshot(pool, failed["snapshot_id"])
    assert failure["state"] == "failed" and failure["last_error"]
    assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"
    assert await store.snapshot.claim_due_sandbox_snapshot() is None, "nothing requested since"
    assert await store.snapshot.claim_for_publish() is None

    # The owner fixes the workspace in a later turn of the same chat. Settling
    # it publishes nothing: the group stays dirty until the owner asks.
    second = await _next_turn(store, parent=first, sandbox=sandbox, prompt="drop the workflow")
    assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"
    assert await store.snapshot.claim_due_sandbox_snapshot() is None
    requested = await _request(store, second)
    assert requested["requested"] is True
    assert await _group_state(pool, sandbox["sandbox_id"]) == "pending"

    superseding = await store.snapshot.claim_due_sandbox_snapshot()
    assert superseding is not None
    assert superseding["snapshot_id"] == requested["snapshot_id"]
    assert superseding["anchor_job_id"] == second["id"]
    assert superseding["workspace_epoch"] > failed["workspace_epoch"]
    # Cut from the base the failure never advanced, so it carries that work too.
    assert superseding["base_sha"] == failed["base_sha"]
    assert await _included(pool, superseding["snapshot_id"]) == [second["id"]]
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
    assert await _included(pool, failed["snapshot_id"]) == [first["id"]]
    assert await store.snapshot.claim_for_publish() is None, "a failed snapshot is never retried"


async def test_a_retry_without_a_new_turn_opens_a_fresh_epoch(store, pool):
    """Asking again after a failure, with nothing settled since, captures the workspace anew.

    The owner may have fixed the file from the terminal. The new snapshot takes
    the newest settled turn again, at a new epoch; the failed one stays failed.
    """
    first, _sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    failed = await store.snapshot.claim_due_sandbox_snapshot()
    assert failed is not None
    await _fail(store, pool, "publish_rejected", failed)

    retried = await _request(store, first)
    assert retried["requested"] is True
    assert retried["snapshot_id"] != failed["snapshot_id"]
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None and capture["snapshot_id"] == retried["snapshot_id"]
    assert capture["anchor_job_id"] == first["id"]
    assert capture["workspace_epoch"] > failed["workspace_epoch"]
    assert capture["base_sha"] == failed["base_sha"]
    assert await _record(store, capture, _FIXED_PATCH) == "captured"
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == capture["snapshot_id"]
    await _publish(store, publication, commit_sha="a" * 40)

    assert (await _snapshot(pool, failed["snapshot_id"]))["state"] == "failed"
    assert (await _snapshot(pool, capture["snapshot_id"]))["state"] == "published"
    assert await _included(pool, capture["snapshot_id"]) == [first["id"]]


async def test_a_retry_builds_on_the_commit_its_failed_attempt_pushed(store, pool):
    """A push recorded before a failed pull request reaches the retry's claim.

    Reopening the failed snapshot would recapture it and lose that record; a
    fresh epoch carries it as one of the platform's own pushes instead.
    """
    first, _sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    assert await _record(store, capture, _FIXED_PATCH) == "captured"
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    pushed = "9" * 40
    assert await store.snapshot.record_snapshot_push(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        commit_sha=pushed,
    )
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        detail="GitHub rejected the draft PR (422): the head branch is invalid or unavailable",
    )

    assert (await _request(store, first))["requested"] is True
    retry_capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert retry_capture is not None
    assert await _record(store, retry_capture, _FIXED_PATCH) == "captured"
    retry = await store.snapshot.claim_for_publish()
    assert retry is not None and retry["snapshot_id"] == retry_capture["snapshot_id"]
    assert retry["own_pushes"] == [pushed]


async def test_a_later_snapshot_still_carrying_the_change_fails_and_is_superseded_too(store, pool):
    """Refused again while the change is there; published once a turn removes it."""
    first, sandbox = await _first_turn(store)
    rejected = []
    parent = first
    for prompt in (None, "rename the variable"):
        if prompt is not None:
            parent = await _next_turn(store, parent=parent, sandbox=sandbox, prompt=prompt)
        assert (await _request(store, parent))["requested"] is True
        capture = await store.snapshot.claim_due_sandbox_snapshot()
        assert capture is not None and capture["anchor_job_id"] == parent["id"]
        await _fail(store, pool, "publish_rejected", capture)
        assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"
        rejected.append(capture)

    third = await _next_turn(store, parent=parent, sandbox=sandbox, prompt="drop the workflow")
    assert (await _request(store, third))["requested"] is True
    superseding = await store.snapshot.claim_due_sandbox_snapshot()
    assert superseding is not None and superseding["anchor_job_id"] == third["id"]
    assert await _record(store, superseding, _FIXED_PATCH) == "captured"
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == superseding["snapshot_id"]
    await _publish(store, publication, commit_sha="b" * 40)

    states = [(await _snapshot(pool, capture["snapshot_id"]))["state"] for capture in rejected]
    assert states == ["failed", "failed"]
    assert (await _snapshot(pool, superseding["snapshot_id"]))["state"] == "published"


async def test_a_turn_that_settled_during_a_publication_waits_for_the_owner(store, pool):
    """A turn settled while an earlier snapshot was publishing is published on request.

    While the earlier snapshot is on its way, a request returns it instead of
    opening a second one. Once it fails, nothing is captured until the owner
    asks again; that request takes the turn that settled meanwhile.
    """
    first, sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    earlier = await store.snapshot.claim_due_sandbox_snapshot()
    assert earlier is not None
    assert await _record(store, earlier, _REJECTED_PATCH) == "captured"
    second = await _next_turn(store, parent=first, sandbox=sandbox, prompt="drop the workflow")
    assert await _group_state(pool, sandbox["sandbox_id"]) == "quiescent"
    in_flight = await _request(store, second)
    assert in_flight == {
        "snapshot_id": earlier["snapshot_id"],
        "state": "captured",
        "requested": False,
    }

    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == earlier["snapshot_id"]
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=earlier["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        detail="patch rejected: .github/workflows/ci.yml changes workflows",
    )
    assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"
    assert await store.snapshot.claim_due_sandbox_snapshot() is None

    assert (await _request(store, second))["requested"] is True
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
    assert (await _request(store, first))["requested"] is True
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
    assert (await _request(store, second))["requested"] is False
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

    assert (await _request(store, second))["requested"] is True
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
