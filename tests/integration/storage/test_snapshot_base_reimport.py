"""A published base Git pruned from the workspace is imported again, on real Postgres.

The host imports a published commit as objects no ref names, so once their pack
is old enough any default ``git gc`` prunes them, and the next capture of that
base finds it missing. Every later snapshot is cut from the same base, so
failing the capture made the workspace unpublishable for good. Now the capture
goes back to ``pending`` and its group to ``importing`` for the same published
base; the import that publication would run fetches the commit again, and the
capture is claimed once it completes. A base no publication made cannot be
fetched again, and a publication waiting on an import that keeps failing fails
visibly instead of waiting for good.
"""

from __future__ import annotations

import pytest
from tests.integration.storage.test_snapshot_supersession import (
    _first_turn,
    _group_state,
    _next_turn,
    _publish,
    _record,
    _request,
    _snapshot,
    pool as pool,
    store as store,
)

from cloud_agent.storage.repositories.snapshot import BASE_IMPORT_WAIT_LIMIT_S

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_FIRST_PATCH = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
_SECOND_PATCH = "diff --git a/later.txt b/later.txt\nnew file mode 100644\n"
_FIRST_COMMIT = "1" * 40
_SECOND_COMMIT = "2" * 40


def _fence(capture: dict) -> dict:
    """The capture claim's identity, as the coordinator hands it back."""
    return {
        "snapshot_id": capture["snapshot_id"],
        "sandbox_id": capture["sandbox_id"],
        "sandbox_generation": capture["generation"],
        "container_id": capture["container_id"],
    }


async def _published_twice(store) -> tuple[dict, dict, dict]:
    """Two publications of one shared chat, each imported; returns the turn, group and second."""
    first, sandbox = await _first_turn(store)
    published = []
    parent = first
    for patch, commit in ((_FIRST_PATCH, _FIRST_COMMIT), (_SECOND_PATCH, _SECOND_COMMIT)):
        if published:
            parent = await _next_turn(store, parent=parent, sandbox=sandbox, prompt="more")
        assert (await _request(store, parent))["requested"] is True
        capture = await store.snapshot.claim_due_sandbox_snapshot()
        assert capture is not None
        assert await _record(store, capture, patch) == "captured"
        publication = await store.snapshot.claim_for_publish()
        assert publication is not None and publication["snapshot_id"] == capture["snapshot_id"]
        await _publish(store, publication, commit_sha=commit)
        published.append(capture)
    return parent, sandbox, published[1]


async def _capture_of_pruned_base(store, pool) -> tuple[dict, dict, dict, dict]:
    """The owner asks again after two publications; Git has pruned the second."""
    last_turn, sandbox, second = await _published_twice(store)
    turn = await _next_turn(store, parent=last_turn, sandbox=sandbox, prompt="after gc")
    requested = await _request(store, turn)
    assert requested["requested"] is True
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None and capture["snapshot_id"] == requested["snapshot_id"]
    assert capture["base_sha"] == _SECOND_COMMIT
    assert await _group_state(pool, sandbox["sandbox_id"]) == "snapshotting"
    return turn, sandbox, second, capture


async def _start_fence_and_base(pool, sandbox_id: str) -> tuple[int, object, str]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT start_seq, idle_deadline, published_base_sha FROM agent_sandboxes "
            "WHERE id = $1",
            sandbox_id,
        )
    assert row is not None
    return int(row["start_seq"]), row["idle_deadline"], str(row["published_base_sha"])


async def test_a_capture_whose_base_git_pruned_waits_for_it_to_be_imported_again(store, pool):
    """The capture is not failed: the group imports its base again, then it is captured."""
    _turn, sandbox, second, capture = await _capture_of_pruned_base(store, pool)
    start_seq, _deadline, _base = await _start_fence_and_base(pool, sandbox["sandbox_id"])

    # A hand-back from another lifecycle identity changes nothing.
    stale = {**_fence(capture), "container_id": "container-replaced"}
    assert not await store.snapshot.reimport_snapshot_base(**stale, base_sha=_SECOND_COMMIT)
    assert (await _snapshot(pool, capture["snapshot_id"]))["state"] == "capturing"

    assert await store.snapshot.reimport_snapshot_base(**_fence(capture), base_sha=_SECOND_COMMIT)

    waiting = await _snapshot(pool, capture["snapshot_id"])
    assert waiting["state"] == "pending", "the snapshot that found its base missing must not fail"
    assert waiting["base_sha"] == _SECOND_COMMIT
    assert "imported again" in waiting["last_error"]
    assert await _group_state(pool, sandbox["sandbox_id"]) == "importing"
    # As publication leaves it: the start fenced, no idle stop, the same base.
    new_start_seq, deadline, base = await _start_fence_and_base(pool, sandbox["sandbox_id"])
    assert new_start_seq == start_seq + 1
    assert deadline is None
    assert base == _SECOND_COMMIT
    # The capture waits for the import instead of being claimed into it.
    assert await store.snapshot.claim_due_sandbox_snapshot() is None
    assert await store.snapshot.next_snapshot_retry_due() is None

    pending_import = await store.snapshot.pending_snapshot_base_import()
    assert pending_import is not None
    assert pending_import["snapshot_id"] == second["snapshot_id"]
    assert pending_import["published_commit_sha"] == _SECOND_COMMIT
    # Cut from the first publication, but bundled against the workspace's
    # first base, which Git keeps when it prunes both published commits.
    assert pending_import["base_sha"] == _FIRST_COMMIT
    async with pool.acquire() as conn:
        first_base = await conn.fetchval(
            "SELECT base_sha FROM agent_sandbox_snapshots WHERE sandbox_id = $1 "
            "ORDER BY workspace_epoch LIMIT 1",
            sandbox["sandbox_id"],
        )
    assert first_base not in (_FIRST_COMMIT, _SECOND_COMMIT)
    assert pending_import["origin_base_sha"] == first_base

    assert await store.snapshot.complete_snapshot_base_import(
        snapshot_id=second["snapshot_id"],
        commit_sha=_SECOND_COMMIT,
        generation=pending_import["generation"],
        container_id=pending_import["container_id"],
    )
    assert await _group_state(pool, sandbox["sandbox_id"]) == "pending"
    recaptured = await store.snapshot.claim_due_sandbox_snapshot()
    assert recaptured is not None
    assert recaptured["snapshot_id"] == capture["snapshot_id"]
    assert recaptured["base_sha"] == _SECOND_COMMIT
    assert await _record(store, recaptured, _FIRST_PATCH) == "captured"
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["snapshot_id"] == capture["snapshot_id"]
    await _publish(store, publication, commit_sha="3" * 40)
    assert (await _snapshot(pool, capture["snapshot_id"]))["state"] == "published"


async def test_a_missing_base_no_publication_made_is_left_to_fail(store, pool):
    """The base a workspace started from was never published, so nothing can fetch it."""
    first, sandbox = await _first_turn(store)
    assert (await _request(store, first))["requested"] is True
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None

    assert not await store.snapshot.reimport_snapshot_base(
        **_fence(capture), base_sha=capture["base_sha"]
    )

    assert (await _snapshot(pool, capture["snapshot_id"]))["state"] == "capturing"
    assert await _group_state(pool, sandbox["sandbox_id"]) == "snapshotting"
    assert await store.snapshot.pending_snapshot_base_import() is None


async def test_a_publication_waiting_on_a_failing_import_fails_once_it_waited_too_long(store, pool):
    """A commit GitHub no longer serves never imports; the owner sees a failure, not a wait."""
    turn, sandbox, second, capture = await _capture_of_pruned_base(store, pool)
    assert await store.snapshot.reimport_snapshot_base(**_fence(capture), base_sha=_SECOND_COMMIT)
    detail = "published base import retry failed: git fetch failed: not our ref"

    await store.snapshot.record_snapshot_base_import_failure(
        snapshot_id=second["snapshot_id"], detail=detail
    )
    assert (await _snapshot(pool, capture["snapshot_id"]))["state"] == "pending"
    assert await _group_state(pool, sandbox["sandbox_id"]) == "importing"

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots "
            "SET updated_at = clock_timestamp() - make_interval(secs => $2) WHERE id = $1",
            capture["snapshot_id"],
            BASE_IMPORT_WAIT_LIMIT_S + 60,
        )
    await store.snapshot.record_snapshot_base_import_failure(
        snapshot_id=second["snapshot_id"], detail=detail
    )

    failed = await _snapshot(pool, capture["snapshot_id"])
    assert failed["state"] == "failed"
    assert detail in failed["last_error"]
    assert await _group_state(pool, sandbox["sandbox_id"]) == "dirty"
    # The retries end with the wait: nothing holds the group in importing.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET retry_at = NULL WHERE id = $1",
            second["snapshot_id"],
        )
    assert await store.snapshot.pending_snapshot_base_import() is None
    # The published commit stays the base; the owner's next request captures again.
    requested = await _request(store, turn)
    assert requested["requested"] is True
    again = await store.snapshot.claim_due_sandbox_snapshot()
    assert again is not None and again["snapshot_id"] == requested["snapshot_id"]
    assert again["base_sha"] == _SECOND_COMMIT
