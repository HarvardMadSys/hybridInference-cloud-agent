"""Real Postgres checks for which containers a moved group left behind on a host."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
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


async def _superseded(store: Storage, host: str, *held: dict) -> list[dict]:
    return await store.compute_resources.superseded_containers(host=host, containers=list(held))


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

    assert await _superseded(store, "runner-a", container) == [container]
    # Only that exact container, of that group, under that generation.
    assert await _superseded(store, "runner-a", _held(job, container_id="container-x")) == []
    assert await _superseded(store, "runner-a", _held(job, generation=2)) == []
    assert await _superseded(store, "runner-a", {**container, "origin_thread_id": "athr_x"}) == []
    assert await _superseded(store, "runner-b", container) == []
    assert await _superseded(store, "runner-a", {**container, "sandbox_id": "asbx_x"}) == []


async def test_a_group_still_on_its_host_keeps_its_container(pool, store: Storage):
    job, _ = await _covered(pool, store)
    # Stopped and covered, but not handed over: the host may start it again.
    assert await _superseded(store, "runner-a", _held(job)) == []


async def test_a_container_stays_while_its_group_has_no_checkpoint_to_restore_from(
    pool, store: Storage
):
    job, _ = await _handed_over(pool, store)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_checkpoints SET state = 'deleted'")
    assert await _superseded(store, "runner-a", _held(job)) == []


@pytest.mark.parametrize("deletion_state", ["pending", "archived"])
async def test_a_group_being_deleted_keeps_its_container_for_the_deletion(
    pool, store: Storage, deletion_state: str
):
    job, _ = await _handed_over(pool, store)
    await _deletion(pool, deletion_state)
    assert await _superseded(store, "runner-a", _held(job)) == []


async def test_the_old_container_lets_go_through_the_restore_and_after_it(pool, store: Storage):
    """Once true, the answer holds whatever the group does next."""
    job, follow_up = await _handed_over(pool, store)
    old = _held(job)
    assert await _superseded(store, "runner-a", old) == [old]

    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    # Restoring: the group counts on its checkpoint, never on the old container.
    assert await _superseded(store, "runner-a", old) == [old]

    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    assert (await _state(pool))["restore_checkpoint_id"] is None
    # Running from a newer generation: the old container holds nothing needed.
    assert await _superseded(store, "runner-a", old) == [old]
    # The group's own container never goes.
    current = _held(job, generation=2, container_id="container-c")
    assert await _superseded(store, "runner-c", current) == []
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET state = 'stopped' WHERE id = 'asbx_restore'")
    assert await _superseded(store, "runner-c", current) == []
    assert await _superseded(store, "runner-a", old) == [old]


async def test_a_failed_restore_leaves_its_container_behind_until_the_group_is_lost(
    pool, store: Storage
):
    job, follow_up = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    await _fail_restore(store, follow_up)
    assert (await _state(pool))["state"] == "cold"
    failed = _held(job, generation=2, container_id="container-failed")
    old = _held(job)

    assert await _superseded(store, "runner-c", failed) == [failed]
    assert await _superseded(store, "runner-a", old) == [old]
    # The failed restore's container was made here; another host's is not it.
    assert await _superseded(store, "runner-a", failed) == []

    for attempt in range(2, MAX_RESTORE_ATTEMPTS + 1):
        await _age_restore_failure(pool, RESTORE_RETRY_DELAYS_S[attempt - 2])
        await _fail_restore(store, follow_up)
    assert (await _state(pool))["state"] == "lost"
    # Every restore failed: what any host still holds may be the only good copy.
    assert await _superseded(store, "runner-a", old) == []
    assert await _superseded(store, "runner-c", failed) == []


async def test_a_deleted_group_keeps_nothing(pool, store: Storage):
    job, follow_up = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    await _fail_restore(store, follow_up)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_checkpoints SET state = 'deleted'")
    await _deletion(pool, "deleted")
    old = _held(job)
    assert await _superseded(store, "runner-a", old) == [old]


async def test_a_group_lost_after_its_restore_keeps_what_is_left_of_it(pool, store: Storage):
    """Its new host's machine went away: the old container may be all there is."""
    job, follow_up = await _handed_over(pool, store)
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )
    old = _held(job)
    assert await _superseded(store, "runner-a", old) == [old]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET state = 'lost' WHERE id = 'asbx_restore'")
    assert await _superseded(store, "runner-a", old) == []


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
    assert await _superseded(store, "runner-a", _held(job)) == []
