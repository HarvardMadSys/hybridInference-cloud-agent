"""A snapshot's title context covers the turns no stored context holds, on real Postgres.

Each capture extends the bounded context of the newest earlier snapshot that
holds one. An epoch can end without one: its capture was refused before it was
taken, or it was captured before 0096 or by the release before it, which leave
the column default. The turns of such an epoch are still in the workspace, and
in the pull request every later snapshot updates, so the next context must read
them rather than continue from an empty one. A snapshot claimed with the column
default gets its context at the claim, from its transcript.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _reserve_shared_chat,
    _shared_claim_attestation,
    pool as pool,
    store as store,
)
from tests.support.follow_ups import append_follow_up

from cloud_agent.publish_worker import _pr_title

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_CONTAINER = "container-title-context"
_PATCH = "diff --git a/retry.py b/retry.py\nnew file mode 100644\n"


async def _turn(
    store, pool, *, prompt: str, reply: str, parent: dict | None = None, sandbox=None
) -> tuple[dict, dict]:
    """Run one turn of the shared chat through settlement, closing with ``reply``.

    The first turn creates the chat and its sandbox; a later one follows
    ``parent`` in the same chat and sandbox.
    """
    if parent is None:
        job, claim, sandbox = await _reserve_shared_chat(store, task_prompt=prompt)
        activated = sandbox
    else:
        job = await append_follow_up(
            pool, parent_job_id=parent["id"], user_id=parent["user_id"], prompt=prompt
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
    context_status = "new" if parent is None else "restored"
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id=_CONTAINER, context_status=context_status
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": reply},
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live" if parent is None else "restored",
        native_session_id="native-title-context",
        idle_timeout_seconds=1800,
        base_sha=(parent or job)["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    return job, sandbox


async def _claim_capture(store, job: dict) -> dict:
    """The owner asks to publish; the coordinator claims the snapshot that opens."""
    requested = await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )
    assert requested["requested"] is True, requested
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None and capture["snapshot_id"] == requested["snapshot_id"]
    return capture


async def _record(store, capture: dict) -> None:
    """Hand the coordinator's claim the host's receipt."""
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=_PATCH,
            content_sha256=hashlib.sha256(_PATCH.encode()).hexdigest(),
        )
        == "captured"
    )


async def _publish(store, capture: dict, *, commit_sha: str) -> None:
    """Claim, publish and import the base of the captured snapshot ``capture``."""
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == capture["snapshot_id"]
    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=publication["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        pr_url="https://example.test/pull/1",
        commit_sha=commit_sha,
    )
    assert await store.snapshot.complete_snapshot_base_import(
        snapshot_id=publication["snapshot_id"],
        commit_sha=commit_sha,
        generation=publication["generation"],
        container_id=publication["container_id"],
    )


async def _clear_title_context(pool, snapshot_id: int) -> None:
    """Leave the column default, as a capture before 0096 or by the release before it does."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET title_context = DEFAULT WHERE id = $1",
            snapshot_id,
        )


@pytest.mark.parametrize("ended", ["capture_refused", "captured_without_context"])
async def test_a_capture_reads_the_turns_of_an_epoch_that_stored_no_context(store, pool, ended):
    """The epoch between two computed contexts still names the next publication.

    ``capture_refused``: the host refused the capture (an unsafe census), so the
    snapshot failed before it was captured. ``captured_without_context``: it was
    captured and published by a release that stores no context.
    """
    first, sandbox = await _turn(
        store, pool, prompt="add a retry helper", reply="Added the retry helper."
    )
    earlier = await _claim_capture(store, first)
    await _record(store, earlier)
    await _publish(store, earlier, commit_sha="b" * 40)

    second, _ = await _turn(
        store,
        pool,
        prompt="document the retry helper",
        reply="Documented the retry helper.",
        parent=first,
        sandbox=sandbox,
    )
    between = await _claim_capture(store, second)
    if ended == "capture_refused":
        assert await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=between["snapshot_id"],
            sandbox_id=between["sandbox_id"],
            sandbox_generation=between["generation"],
            container_id=between["container_id"],
            detail="the workspace holds content the capture refused",
            dirty=True,
        )
    else:
        await _record(store, between)
        await _publish(store, between, commit_sha="c" * 40)
        await _clear_title_context(pool, between["snapshot_id"])

    third, _ = await _turn(
        store,
        pool,
        prompt="log each retry",
        reply="Logged each retry.",
        parent=second,
        sandbox=sandbox,
    )
    later = await _claim_capture(store, third)
    await _record(store, later)
    publication = await store.snapshot.claim_for_publish()

    assert publication is not None and publication["snapshot_id"] == later["snapshot_id"]
    # The middle turn belongs to the epoch that stored nothing, not to this one.
    assert publication["included_job_ids"] == [third["id"]]
    assert [
        (message["job_id"], message["role"], message["content"])
        for message in publication["session_messages"]
    ] == [
        (first["id"], "user", "add a retry helper"),
        (first["id"], "assistant", "Added the retry helper."),
        (second["id"], "user", "document the retry helper"),
        (second["id"], "assistant", "Documented the retry helper."),
        (third["id"], "user", "log each retry"),
        (third["id"], "assistant", "Logged each retry."),
    ]


async def test_a_snapshot_captured_without_a_context_is_titled_from_its_transcript(store, pool):
    """A claim builds the missing context, so the draft is not named after its files."""
    first, _sandbox = await _turn(
        store, pool, prompt="add a retry helper", reply="Added the retry helper."
    )
    capture = await _claim_capture(store, first)
    await _record(store, capture)
    await _clear_title_context(pool, capture["snapshot_id"])

    publication = await store.snapshot.claim_for_publish()

    assert publication is not None and publication["snapshot_id"] == capture["snapshot_id"]
    assert publication["session_messages"] == [
        {
            "thread_id": first["thread_id"],
            "job_id": first["id"],
            "role": "user",
            "content": "add a retry helper",
        },
        {
            "thread_id": first["thread_id"],
            "job_id": first["id"],
            "role": "assistant",
            "content": "Added the retry helper.",
        },
    ]
    assert publication["session_summaries"] == ["Added the retry helper."]
    assert publication["session_summary_count"] == 1
    assert _pr_title(publication, ["retry.py"]) == "[agent] Added the retry helper."
    # Stored once, so a retried claim and the group's next capture build on it.
    async with pool.acquire() as conn:
        stored = json.loads(
            await conn.fetchval(
                "SELECT title_context FROM agent_sandbox_snapshots WHERE id = $1",
                capture["snapshot_id"],
            )
        )
    assert [message["content"] for message in stored["messages"]] == [
        "add a retry helper",
        "Added the retry helper.",
    ]
    assert stored["summary_count"] == 1
