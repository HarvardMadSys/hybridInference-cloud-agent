"""Deleting a ``cold`` sandbox, which only its checkpoint holds, without its gone host.

A cold group's chats wait for any restore-capable host to claim their next
turn, so a deletion races a restore claim for the group's row. These run both
on real Postgres, interleaved: whichever commits first wins, and the other
changes nothing.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from tests.integration.storage.test_agent_job_store import (
    _waiting_on,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)
from tests.integration.storage.test_lock_order import _settle
from tests.integration.storage.test_sandbox_restore_store import (
    _RESTORE_HOST,
    _claim,
    _covered,
    _host,
)
from tests.support.follow_ups import append_follow_up

from cloud_agent.domain.host_pool import SILENT_HOST_HANDOVER_SECONDS
from cloud_agent.storage._sql.hosts import superseded_containers
from cloud_agent.storage.repositories.sandbox import admin_deletion
from cloud_agent_shared.sandbox_restore import RESTORE_FAILED_RELEASE_REASON

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_SANDBOX = "asbx_restore"
_CHECKPOINT = "c" * 32


async def _group(pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_sandboxes WHERE id = $1", _SANDBOX)
    assert row is not None
    return dict(row)


async def _cold(pool, store: Storage) -> dict[str, Any]:
    """A group handed over when ``runner-a`` was removed, with a turn queued for it.

    ``runner-c`` can restore it: the queued turn is what a claim hands out.
    """
    job, _ = await _covered(pool, store)
    follow_up = await append_follow_up(
        pool, parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    group = await _group(pool)
    assert (group["state"], group["host"], group["container_id"]) == (
        "cold",
        "runner-a",
        "container-aa",
    )
    return follow_up


def _delete(store: Storage, group: dict[str, Any], *, discard: bool = True):
    """The administrator's deletion of the cold group exactly as reviewed."""
    return store.sandbox.request_admin_delete(
        sandbox_id=_SANDBOX,
        generation=group["generation"],
        container_id=group["container_id"],
        last_activity_at=group["last_activity_at"],
        actor_id="admin:operator",
        admin_ip="127.0.0.1",
        discard=discard,
    )


async def _job_state(store: Storage, job: dict[str, Any]) -> str:
    current = await store.conversation_queries.get_job(job["id"])
    assert current is not None
    return str(current["state"])


async def _checkpoint_states(pool) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT state FROM agent_sandbox_checkpoints WHERE sandbox_id = $1 ORDER BY id",
            _SANDBOX,
        )
    return [row["state"] for row in rows]


async def _deletion_requests(pool) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_admin_audit_log "
                "WHERE action = 'agent.sandbox.delete.requested'"
            )
        )


async def _assert_untouched(pool, store: Storage, follow_up: dict[str, Any]) -> None:
    """Nothing of the deletion happened: the group still waits in its checkpoint."""
    group = await _group(pool)
    assert group["deletion_state"] == "none"
    assert group["deleted_at"] is None
    assert await _checkpoint_states(pool) == ["reported"]
    assert await _deletion_requests(pool) == 0
    assert await _job_state(store, follow_up) in {"queued", "running"}


async def _assert_deleted(pool, store: Storage, follow_up: dict[str, Any]) -> None:
    """Deleted in full, by the request alone: no host and no worker took part."""
    group = await _group(pool)
    assert group["state"] == "lost"
    assert group["deletion_state"] == "deleted"
    assert group["deletion_discard"] is True
    assert group["deleted_at"] is not None
    assert group["deletion_claim_token"] is None
    native = await store.sandbox.thread_sandbox(thread_id=follow_up["thread_id"])
    assert native is not None
    assert (native["native_state"], native["context_status"]) == ("lost", "lost")
    # The turn waiting for the restore can never run now; it says so.
    assert await _job_state(store, follow_up) == "failed"
    # The receipts back no group any more. Their objects stay in S3, under the
    # bucket's lifecycle rules: the control plane deletes no object.
    assert await _checkpoint_states(pool) == ["superseded"]
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, source, actor_id, details FROM agent_sandbox_events "
            "WHERE sandbox_id = $1 AND event_type IN "
            "('admin_delete_requested', 'deletion_completed') ORDER BY id",
            _SANDBOX,
        )
        outcome = await conn.fetchrow(
            "SELECT r.outcome, r.reported_by, r.details FROM agent_admin_audit_outcomes r "
            "JOIN agent_admin_audit_log l ON l.id = r.event_id "
            "WHERE l.action = 'agent.sandbox.delete.requested'"
        )
        attempts = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_sandbox_deletion_attempts WHERE sandbox_id = $1",
            _SANDBOX,
        )
    assert [(row["event_type"], row["source"], row["actor_id"]) for row in events] == [
        ("admin_delete_requested", "admin", "admin:operator"),
        ("deletion_completed", "admin", "admin:operator"),
    ]
    completed = json.loads(events[1]["details"])
    assert completed["discard"] is True
    assert completed["checkpoint_only"] is True
    assert completed["retired_checkpoints"] == 1
    assert outcome is not None
    assert (outcome["outcome"], outcome["reported_by"]) == ("applied", "admin:operator")
    assert json.loads(outcome["details"]) == {
        "discarded": True,
        "checkpoint_only": True,
        "retired_checkpoints": 1,
    }
    assert attempts == 0


@pytest.mark.parametrize("forgotten", [False, True])
async def test_a_cold_sandbox_of_a_removed_host_is_deleted_at_once(
    pool, store: Storage, forgotten: bool
):
    """Its host was removed from the pool, or forgotten as well: nothing can be asked."""
    follow_up = await _cold(pool, store)
    if forgotten:
        assert await store.host_pool.forget_runner_host(host="runner-a") == "deleted"
    group = await _group(pool)

    assert await _delete(store, group) == "accepted"
    await _assert_deleted(pool, store, follow_up)
    # The same review again changes nothing more.
    assert await _delete(store, group) == "accepted"
    assert await _deletion_requests(pool) == 1
    # No host can restore it any more.
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None
    assert (await _group(pool))["state"] == "lost"


async def test_a_cold_sandbox_has_nothing_to_archive(pool, store: Storage):
    follow_up = await _cold(pool, store)
    assert await _delete(store, await _group(pool), discard=False) == "checkpoint_only"
    await _assert_untouched(pool, store, follow_up)


async def test_a_changed_cold_sandbox_is_reviewed_again(pool, store: Storage):
    follow_up = await _cold(pool, store)
    reviewed = await _group(pool)
    assert await _delete(store, reviewed | {"generation": reviewed["generation"] + 1}) == "stale"
    assert await _delete(store, reviewed | {"container_id": None}) == "stale"
    await _assert_untouched(pool, store, follow_up)


async def _failed_restore(pool, store: Storage, follow_up: dict[str, Any]) -> None:
    """``runner-c`` claims the group, and its restore fails.

    The group is cold again, now naming ``runner-c``, which still reports,
    and no container at all.
    """
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    assert await store.attempt_commands.release_claim(
        job_id=follow_up["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        reason=RESTORE_FAILED_RELEASE_REASON,
    )
    group = await _group(pool)
    assert (group["state"], group["host"], group["container_id"]) == ("cold", "runner-c", None)
    assert await _job_state(store, follow_up) == "queued"


async def _silence(pool, host: str, seconds: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts "
            "SET last_heartbeat_at = NOW() - make_interval(secs => $2) WHERE host = $1",
            host,
            seconds,
        )


async def test_a_cold_sandbox_waits_while_its_host_may_still_report(pool, store: Storage):
    """A host still in the pool is gone only once it has been silent long enough.

    A failed restore leaves the group naming the host that tried it, with no
    container: the deletion names none either.
    """
    follow_up = await _cold(pool, store)
    await _failed_restore(pool, store, follow_up)

    assert await _delete(store, await _group(pool)) == "host_present"
    await _silence(pool, "runner-c", SILENT_HOST_HANDOVER_SECONDS - 60)
    assert await _delete(store, await _group(pool)) == "host_present"
    group = await _group(pool)
    assert group["deletion_state"] == "none"
    assert await _job_state(store, follow_up) == "queued"

    await _silence(pool, "runner-c", SILENT_HOST_HANDOVER_SECONDS + 60)
    assert await _delete(store, await _group(pool)) == "accepted"
    await _assert_deleted(pool, store, follow_up)


async def test_any_other_deletion_still_names_its_container(pool, store: Storage):
    """Only a cold group may be deleted without naming a container."""
    job, _ = await _covered(pool, store)
    group = await _group(pool)
    assert group["state"] == "stopped"
    assert await _delete(store, group | {"container_id": None}) == "no_container"
    assert (await _group(pool))["deletion_state"] == "none"
    assert (await _job_state(store, job)) == "succeeded"


async def test_a_restore_claim_holding_a_queued_turn_wins_over_a_cold_deletion(
    pool, store: Storage
):
    """The deletion takes the group first, but the claim already holds the turn.

    A claim locks the queued turn it hands out and then waits for the group.
    Waiting for that turn while holding the group would close a cycle, so the
    deletion takes the turns without waiting, finds one held, and gives way.
    The claim then restores the group as if no deletion had been asked for.
    """
    follow_up = await _cold(pool, store)
    reviewed = await _group(pool)
    delete = claim = None
    async with pool.acquire() as blocker:
        try:
            async with blocker.transaction():
                await blocker.execute(
                    "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE", _SANDBOX
                )
                delete = asyncio.create_task(_delete(store, reviewed))
                deleting = await _waiting_on(
                    pool, await blocker.fetchval("SELECT pg_backend_pid()")
                )
                # The claim holds the turn and queues for the group behind it.
                claim = asyncio.create_task(_claim(store, "runner-c", _RESTORE_HOST))
                await _waiting_on(pool, deleting)
            assert await asyncio.wait_for(delete, timeout=10) == "claimed"
            claimed = await asyncio.wait_for(claim, timeout=10)
        finally:
            await _settle(delete)
            await _settle(claim)

    assert claimed is not None and claimed["id"] == follow_up["id"]
    group = await _group(pool)
    assert (group["state"], group["host"]) == ("restoring", "runner-c")
    assert group["generation"] == reviewed["generation"] + 1
    await _assert_untouched(pool, store, follow_up)
    assert await _job_state(store, follow_up) == "running"


async def test_a_cold_deletion_waiting_behind_a_restore_claim_finds_it_restoring(
    pool, store: Storage
):
    """The claim takes the group first and moves it on; the review is stale."""
    follow_up = await _cold(pool, store)
    reviewed = await _group(pool)
    delete = claim = None
    async with pool.acquire() as blocker:
        try:
            async with blocker.transaction():
                await blocker.execute(
                    "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE", _SANDBOX
                )
                claim = asyncio.create_task(_claim(store, "runner-c", _RESTORE_HOST))
                claiming = await _waiting_on(
                    pool, await blocker.fetchval("SELECT pg_backend_pid()")
                )
                delete = asyncio.create_task(_delete(store, reviewed))
                await _waiting_on(pool, claiming)
            claimed = await asyncio.wait_for(claim, timeout=10)
            assert await asyncio.wait_for(delete, timeout=10) == "stale"
        finally:
            await _settle(delete)
            await _settle(claim)

    assert claimed is not None and claimed["id"] == follow_up["id"]
    assert (await _group(pool))["state"] == "restoring"
    await _assert_untouched(pool, store, follow_up)


async def test_a_restore_claim_during_a_cold_deletion_takes_nothing(
    pool, store: Storage, monkeypatch
):
    """A claim that comes while the deletion holds the turns skips them.

    The deletion is held after it took the group's turns, before it commits.
    The claim finds no turn it can lock, hands nothing out, and changes
    nothing; once the deletion commits there is nothing left to restore.
    """
    follow_up = await _cold(pool, store)
    reviewed = await _group(pool)
    held, release = asyncio.Event(), asyncio.Event()
    original = admin_deletion.fail_unrunnable_follow_ups

    async def hold_after_the_turns(conn, **fields):
        held.set()
        await release.wait()
        return await original(conn, **fields)

    monkeypatch.setattr(admin_deletion, "fail_unrunnable_follow_ups", hold_after_the_turns)
    delete = None
    try:
        delete = asyncio.create_task(_delete(store, reviewed))
        await asyncio.wait_for(held.wait(), timeout=10)
        assert await asyncio.wait_for(_claim(store, "runner-c", _RESTORE_HOST), 10) is None
        release.set()
        assert await asyncio.wait_for(delete, timeout=10) == "accepted"
    finally:
        release.set()
        await _settle(delete)

    await _assert_deleted(pool, store, follow_up)
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None


async def test_a_cold_deletion_leaves_a_returning_host_its_container_to_remove(
    pool, store: Storage
):
    """Should the removed host come back, the container it kept is superseded.

    A deleted group keeps nothing on any host, so the container the old host
    may still report is one it may remove.
    """
    follow_up = await _cold(pool, store)
    assert await _delete(store, await _group(pool)) == "accepted"
    await _assert_deleted(pool, store, follow_up)
    group = await _group(pool)
    reported = {
        "sandbox_id": _SANDBOX,
        "generation": group["generation"],
        "container_id": "container-aa",
        "origin_thread_id": group["origin_thread_id"],
    }
    async with pool.acquire() as conn:
        assert await superseded_containers(conn, "runner-a", [reported]) == [reported]
