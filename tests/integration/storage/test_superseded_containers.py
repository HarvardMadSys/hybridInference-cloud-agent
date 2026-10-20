"""Real Postgres checks for which containers a moved group left behind on a host."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from tests.integration.storage.test_admin_sandbox_deletion import request_delete, worker_fence
from tests.integration.storage.test_compute_workflow import rig, run_next
from tests.integration.storage.test_sandbox_restore_store import (
    _RESTORE_HOST,
    _age_restore_failure,
    _claim,
    _covered,
    _fence,
    _host,
    _reserve,
    _state,
    pool as pool,
    store as store,
)

from cloud_agent.ports.external.compute_provider import ProviderReceipt
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.storage._sql.hosts import (
    authorize_superseded_removals,
    superseded_containers,
    superseded_removal_request_id,
)
from cloud_agent_shared.sandbox_restore import MAX_RESTORE_ATTEMPTS, RESTORE_RETRY_DELAYS_S

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _held(job: dict, *, generation: int = 1, container_id: str = "container-a") -> dict:
    """One container as a host's Sandbox Manager lists it."""
    return {
        "sandbox_id": "asbx_restore",
        "generation": generation,
        "container_id": container_id,
        "origin_thread_id": job["thread_id"],
    }


async def _superseded(pool, host: str, *held: dict) -> list[dict]:
    """What ``SUPERSEDED_CONTAINER_SQL`` matches now, authorizing nothing."""
    async with pool.acquire() as conn:
        return await superseded_containers(conn, host, list(held))


async def _authorize(store: Storage, host: str, *held: dict) -> list[dict]:
    return await store.compute_resources.authorize_superseded_removals(
        host=host, containers=list(held), operation_id=None
    )


async def _handed_over(pool, store: Storage) -> tuple[dict, dict]:
    """A covered group whose host was removed, and the turn that will restore it."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"
    return job, follow_up


async def _deletion(pool, state: str) -> None:
    """Put the group's deletion in ``state``, with the receipt that state requires."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_state = $1, deletion_request_id = 'request-1', "
            "deletion_archive_key = 's3://bucket/archive.tar', deletion_sha256 = $2, "
            "deletion_size_bytes = 1 WHERE id = 'asbx_restore'",
            state,
            "d" * 64,
        )


async def _fail_restore(store: Storage, follow_up: dict) -> None:
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    assert await store.attempt_commands.release_claim(
        job_id=follow_up["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        reason="sandbox_restore_failed",
    )


async def test_a_group_handed_over_and_never_resumed_leaves_its_container(pool, store: Storage):
    """Nobody writes to the chat again: its group stays cold, and still lets go."""
    job, _ = await _handed_over(pool, store)
    container = _held(job)

    assert await _superseded(pool, "runner-a", container) == [container]
    # Only that exact container, of that group, under that generation.
    assert await _superseded(pool, "runner-a", _held(job, container_id="container-x")) == []
    assert await _superseded(pool, "runner-a", _held(job, generation=2)) == []
    assert await _superseded(pool, "runner-a", {**container, "origin_thread_id": "athr_x"}) == []
    assert await _superseded(pool, "runner-b", container) == []
    assert await _superseded(pool, "runner-a", {**container, "sandbox_id": "asbx_x"}) == []


async def test_a_group_still_on_its_host_keeps_its_container(pool, store: Storage):
    job, _ = await _covered(pool, store)
    # Stopped and covered, but not handed over: the host may start it again.
    assert await _superseded(pool, "runner-a", _held(job)) == []


async def test_a_container_stays_while_its_group_has_no_checkpoint_to_restore_from(
    pool, store: Storage
):
    job, _ = await _handed_over(pool, store)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_checkpoints SET state = 'deleted'")
    assert await _superseded(pool, "runner-a", _held(job)) == []


@pytest.mark.parametrize("deletion_state", ["pending", "archived"])
async def test_a_group_being_deleted_keeps_its_container_for_the_deletion(
    pool, store: Storage, deletion_state: str
):
    job, _ = await _handed_over(pool, store)
    await _deletion(pool, deletion_state)
    assert await _superseded(pool, "runner-a", _held(job)) == []


async def test_the_old_container_lets_go_through_the_restore_and_after_it(pool, store: Storage):
    """Once true, the answer holds whatever the group does next."""
    job, follow_up = await _handed_over(pool, store)
    old = _held(job)
    assert await _superseded(pool, "runner-a", old) == [old]

    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    # Restoring: the group counts on its checkpoint, never on the old container.
    assert await _superseded(pool, "runner-a", old) == [old]

    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    assert (await _state(pool))["restore_checkpoint_id"] is None
    # Running from a newer generation: the old container holds nothing needed.
    assert await _superseded(pool, "runner-a", old) == [old]
    # The group's own container never goes.
    current = _held(job, generation=2, container_id="container-c")
    assert await _superseded(pool, "runner-c", current) == []
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET state = 'stopped' WHERE id = 'asbx_restore'")
    assert await _superseded(pool, "runner-c", current) == []
    assert await _superseded(pool, "runner-a", old) == [old]


async def test_a_failed_restore_leaves_its_container_behind_until_the_group_is_lost(
    pool, store: Storage
):
    job, follow_up = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    await _fail_restore(store, follow_up)
    assert (await _state(pool))["state"] == "cold"
    failed = _held(job, generation=2, container_id="container-failed")
    old = _held(job)

    assert await _superseded(pool, "runner-c", failed) == [failed]
    assert await _superseded(pool, "runner-a", old) == [old]
    # The failed restore's container was made here; another host's is not it.
    assert await _superseded(pool, "runner-a", failed) == []

    for attempt in range(2, MAX_RESTORE_ATTEMPTS + 1):
        await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[attempt - 2])
        await _fail_restore(store, follow_up)
    assert (await _state(pool))["state"] == "lost"
    # Every restore failed: what any host still holds may be the only good copy.
    assert await _superseded(pool, "runner-a", old) == []
    assert await _superseded(pool, "runner-c", failed) == []


async def _restored(pool, store: Storage, follow_up: dict) -> dict:
    """Restore the group on ``runner-c``, settle the turn, and stop its container."""
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    return claim


async def test_a_group_deleted_after_its_restore_keeps_nothing(pool, store: Storage):
    """The deletion path itself leaves the group ``lost`` and ``deleted``."""
    job, follow_up = await _handed_over(pool, store)
    claim = await _restored(pool, store, follow_up)
    assert await store.attempt_commands.transition(
        job_id=follow_up["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', idle_deadline = NULL "
            "WHERE id = 'asbx_restore'"
        )
    old = _held(job)
    assert await _superseded(pool, "runner-a", old) == [old]

    assert await request_delete(store, "asbx_restore", discard=True) == "accepted"
    # Its deletion owns it until done.
    assert await _superseded(pool, "runner-a", old) == []
    deletion = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert deletion is not None and deletion["id"] == "asbx_restore"
    assert await store.sandbox.complete_admin_sandbox_discard(**worker_fence(deletion))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, deletion_state FROM agent_sandboxes WHERE id = 'asbx_restore'"
        )
    assert dict(row) == {"state": "lost", "deletion_state": "deleted"}
    assert await _superseded(pool, "runner-a", old) == [old]


async def test_a_group_lost_after_its_restore_keeps_what_is_left_of_it(pool, store: Storage):
    """Its new host's machine went away: the old container may be all there is."""
    job, follow_up = await _handed_over(pool, store)
    await _restored(pool, store, follow_up)
    old = _held(job)
    assert await _superseded(pool, "runner-a", old) == [old]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET state = 'lost' WHERE id = 'asbx_restore'")
    assert await _superseded(pool, "runner-a", old) == []


async def test_a_newer_generation_without_a_container_keeps_the_old_one(pool, store: Storage):
    """Past the old generation, but neither restoring nor holding a container yet."""
    job, _ = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'starting', restore_checkpoint_id = NULL "
            "WHERE id = 'asbx_restore'"
        )
    assert await _superseded(pool, "runner-a", _held(job)) == []


async def _fail_restores_until_lost(pool, store: Storage, follow_up: dict) -> None:
    for attempt in range(1, MAX_RESTORE_ATTEMPTS + 1):
        if attempt > 1:
            await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[attempt - 2])
        await _fail_restore(store, follow_up)
    assert (await _state(pool))["state"] == "lost"


async def test_a_group_lost_before_the_authorization_keeps_its_container(pool, store: Storage):
    job, follow_up = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    await _fail_restores_until_lost(pool, store, follow_up)

    assert await _authorize(store, "runner-a", _held(job)) == []
    async with pool.acquire() as conn:
        assert not await conn.fetchval("SELECT count(*) FROM agent_superseded_container_removals")


async def test_an_authorization_recorded_first_stands_once_the_group_is_lost(pool, store: Storage):
    """The removal may already have run: a later ``lost`` takes nothing back."""
    job, follow_up = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    old = _held(job)
    request_id = superseded_removal_request_id("runner-a", old)

    authorized = await _authorize(store, "runner-a", old)
    assert authorized == [{**old, "request_id": request_id}]
    await _fail_restores_until_lost(pool, store, follow_up)
    assert await _superseded(pool, "runner-a", old) == []
    # A retry sends the same request for what was authorized, and only that.
    assert await _authorize(store, "runner-a", old) == authorized
    # Another container of the group is not authorized with it.
    assert await _authorize(store, "runner-a", _held(job, container_id="container-x")) == (
        authorized
    )
    assert await _authorize(store, "runner-b", old) == []
    # Unconfirmed, it is resent even once the host no longer lists it.
    assert await _authorize(store, "runner-a") == authorized
    await store.compute_resources.record_superseded_removal(request_id=request_id)
    assert await _authorize(store, "runner-a") == []
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT host, sandbox_id, generation, container_id, removed_at "
            "FROM agent_superseded_container_removals"
        )
    assert row is not None and row["removed_at"] is not None
    assert (row["host"], row["sandbox_id"], row["generation"], row["container_id"]) == (
        "runner-a",
        "asbx_restore",
        1,
        "container-a",
    )


async def test_a_group_becoming_lost_waits_for_an_authorization_under_way(pool, store: Storage):
    """Both take the group's row: the one that commits first decides."""
    job, follow_up = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    for attempt in range(1, MAX_RESTORE_ATTEMPTS):
        if attempt > 1:
            await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[attempt - 2])
        await _fail_restore(store, follow_up)
    await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[MAX_RESTORE_ATTEMPTS - 2])
    old = _held(job)

    async with pool.acquire() as conn, conn.transaction():
        assert await authorize_superseded_removals(conn, "runner-a", [old], None) != []
        last_failure = asyncio.create_task(_fail_restore(store, follow_up))
        await asyncio.sleep(0.5)
        # The last restore failure cannot make the group lost mid-authorization.
        assert not last_failure.done()
        assert (await _state(pool))["state"] == "cold"
    await asyncio.wait_for(last_failure, 10)
    assert (await _state(pool))["state"] == "lost"
    assert await _authorize(store, "runner-a", old) != []


async def test_an_authorization_waits_for_a_group_becoming_lost(pool, store: Storage):
    job, _ = await _handed_over(pool, store)
    old = _held(job)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE agent_sandboxes SET state = 'lost' WHERE id = 'asbx_restore'")
        authorizing = asyncio.create_task(_authorize(store, "runner-a", old))
        await asyncio.sleep(0.5)
        assert not authorizing.done()
    assert await asyncio.wait_for(authorizing, 10) == []


async def _machine_of_runner_a(pool, store: Storage, monkeypatch):
    """A managed machine, acquired by the real worker, that ``runner-a`` ran on."""
    r = await rig(store, store.host_pool, monkeypatch)
    assert (await run_next(r))["state"] == "succeeded"
    allocation = await r.repo.get_allocation(r.op["allocation_id"])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET compute_node_id = $1 WHERE host = 'runner-a'",
            allocation["hosts"][0]["compute_node_id"],
        )
    return r, allocation


async def test_a_handed_over_group_without_a_restore_point_still_holds_its_machine(
    pool, store: Storage, monkeypatch
):
    await _covered(pool, store)
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    r, allocation = await _machine_of_runner_a(pool, store, monkeypatch)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_checkpoints SET state = 'deleted'")

    impact = await r.repo.retirement_impact(allocation["id"])
    assert "retained_groups_present" in impact["blocked_reasons"]
    assert [g["id"] for g in impact["retained_groups"]] == ["asbx_restore"]


@pytest.mark.parametrize("answer_lost", [False, True])
async def test_a_release_removes_what_a_group_never_resumed_left_behind(
    pool, store: Storage, monkeypatch, answer_lost: bool
):
    """From the hand-over to the released machine, with no chat ever written to again.

    With ``answer_lost`` the Host removes the container but its answer never
    arrives: the next pass lists nothing, and confirms the removal anyway.
    """
    job, _ = await _covered(pool, store)
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"
    r, allocation = await _machine_of_runner_a(pool, store, monkeypatch)
    impact = await r.repo.retirement_impact(allocation["id"])
    assert "retained_groups_present" not in impact["blocked_reasons"], impact

    old = {**_held(job), "running": False}
    held = {"runner-a": [old]}

    def listed(host: str) -> list[dict]:
        return list(held.get(host, []))

    lost = [answer_lost]

    async def discard(thread_id: str, payload: dict) -> dict:
        assert thread_id == job["thread_id"]
        held["runner-a"] = []
        if lost and lost.pop():
            raise SandboxManagerError(0, "no answer")
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    managers = {}

    def manager_for(host: dict) -> SimpleNamespace:
        return managers.setdefault(
            host["host"],
            SimpleNamespace(
                list_sandboxes=AsyncMock(side_effect=lambda: listed(host["host"])),
                discard_sandbox_deletion=AsyncMock(side_effect=discard),
            ),
        )

    monkeypatch.setattr(
        "cloud_agent.application.compute_reconcile.sandbox_manager_for_host", manager_for
    )

    async def release(ref, intent):
        gone = r.observation.model_copy(
            update={"state": "terminated", "raw_state": "terminated", "nodes": ()}
        )
        return ProviderReceipt(outcome="confirmed", resource=ref, observation=gone)

    r.provider.release.side_effect = release
    await r.repo.create_operation(
        request_id="retire",
        fingerprint="retire",
        kind="retire",
        allocation_id=allocation["id"],
        parameters={},
        requested_by="admin",
    )
    retired = await run_next(r)
    if answer_lost:
        assert retired["state"] == "waiting", retired
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT removed_at IS NULL FROM agent_superseded_container_removals"
            )
            await conn.execute(
                "UPDATE agent_compute_operations SET next_attempt_at = NOW() WHERE id = $1",
                retired["id"],
            )
        retired = await run_next(r)

    assert retired["state"] == "succeeded", retired
    assert retired["result"]["superseded_removed"] == 1
    assert (await r.repo.get_allocation(allocation["id"]))["release_committed_at"]
    r.provider.release.assert_awaited_once()
    sent = [call.args[1] for call in managers["runner-a"].discard_sandbox_deletion.await_args_list]
    assert sent == [
        {
            "request_id": superseded_removal_request_id("runner-a", old),
            "generation": 1,
            "expected_container_id": "container-a",
            "sandbox_id": "asbx_restore",
        }
    ] * (2 if answer_lost else 1)
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT removed_at IS NOT NULL FROM agent_superseded_container_removals"
        )
        assert not await conn.fetchval(
            "SELECT count(*) FROM agent_jobs WHERE id <> $1 AND thread_id = $2",
            job["id"],
            job["thread_id"],
        )
    # Still cold: its next turn restores it from the checkpoint, elsewhere.
    assert (await _state(pool))["state"] == "cold"
