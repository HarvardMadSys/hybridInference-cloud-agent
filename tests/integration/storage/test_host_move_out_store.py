"""Real Postgres checks for moving a leaving Host's groups out one at a time."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_sandbox_restore_store import (
    _ARCHIVE_HOST,
    _RESTORE_HOST,
    _claim,
    _covered,
    _host,
    _receipt,
    _reserve,
    _settled_stopped_group,
    _state,
    pool as pool,
    store as store,
)

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _moving_out(pool, host: str = "runner-a", allocation_id: str = "leaving") -> None:
    """The hold the compute reconciler sets once the Host's move-out window opens."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_compute_allocations (id,name,provider_id,mode) "
            "VALUES ($1,$1,'cloudlab','provision') ON CONFLICT (id) DO NOTHING",
            allocation_id,
        )
        await conn.execute(
            "INSERT INTO agent_host_admission_holds (host,allocation_id,purpose,source_id,scope) "
            "VALUES ($1,$2,'move_out',$2,'all_claims')",
            host,
            allocation_id,
        )


async def test_a_checkpoint_on_a_host_moving_out_hands_its_group_over_at_once(pool, store: Storage):
    job, group = await _settled_stopped_group(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    await _moving_out(pool)

    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )

    # The Host is still enrolled, yet the group is already the checkpoint's.
    assert (await _state(pool))["state"] == "cold"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    assert claim["thread_sandbox"]["state"] == "restoring"
    assert claim["thread_sandbox"]["host"] == "runner-c"


async def test_the_sweep_stops_an_idle_group_and_hands_it_over_once_covered(pool, store: Storage):
    job, group = await _settled_stopped_group(pool, store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'running', container_id = 'container-a' "
            "WHERE id = $1",
            group["sandbox_id"],
        )
    assert await store.sandbox.sandboxes_moving_out() == []
    await _moving_out(pool)

    [row] = await store.sandbox.sandboxes_moving_out()
    assert row["sandbox_id"] == group["sandbox_id"]
    assert (
        await store.sandbox.request_move_out_stop(
            sandbox_id=row["sandbox_id"],
            generation=row["generation"],
            container_id=row["container_id"],
            actor_id="host-move-out",
        )
        == "accepted"
    )
    stopping = await _state(pool)
    assert stopping["state"] == "stopping"
    assert await store.sandbox.sandboxes_moving_out() == []

    # The Host confirms the stop, then its final archive reports a
    # checkpoint that arrived before this sweep saw the Host was leaving.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', stop_requested_at = NULL WHERE id = $1",
            group["sandbox_id"],
        )
        await conn.execute("DELETE FROM agent_host_admission_holds WHERE purpose = 'move_out'")
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )
    assert (await _state(pool))["state"] == "stopped"
    await _moving_out(pool)

    assert await store.sandbox.release_moving_out_groups() == [group["sandbox_id"]]
    assert (await _state(pool))["state"] == "cold"


async def test_a_busy_group_on_a_host_moving_out_is_left_running(pool, store: Storage):
    _, group = await _settled_stopped_group(pool, store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'running', container_id = 'container-a', "
            "active_operation_count = 1 WHERE id = $1",
            group["sandbox_id"],
        )
    await _moving_out(pool)

    assert (
        await store.sandbox.request_move_out_stop(
            sandbox_id=group["sandbox_id"],
            generation=group["generation"],
            container_id="container-a",
            actor_id="host-move-out",
        )
        == "busy"
    )
    assert (await _state(pool))["state"] == "running"


async def test_a_host_never_restores_its_own_cold_group(pool, store: Storage):
    """It may still hold the old container under the group's stable name."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"

    # The same name enrolled again, now able to restore, is not a target.
    await _host(store, "runner-a", _RESTORE_HOST)
    assert await _claim(store, "runner-a", _RESTORE_HOST) is None

    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]


async def test_a_machine_the_provider_ended_hands_its_covered_groups_over(pool, store: Storage):
    """Its lease ran out with groups still on it; they need not stay there."""
    await _covered(pool, store)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_compute_allocations (id,name,provider_id,mode) "
            "VALUES ('ended','ended','cloudlab','provision')"
        )
        await conn.execute(
            "INSERT INTO agent_compute_nodes "
            "(id,allocation_id,provider_node_id,instance_identity,name) "
            "VALUES ('ended-node','ended','node0','ended:node0','ended')"
        )
        await conn.execute(
            "UPDATE agent_runner_hosts SET compute_node_id='ended-node' WHERE host='runner-a'"
        )

    await store.compute_resources.observe_allocation(
        allocation_id="ended", observation={"observed_state": "terminated"}
    )

    assert (await _state(pool))["state"] == "cold"
    # The Host row, its credential and any deployment fence stay for removal.
    assert (await store.host_pool.get_host(host="runner-a"))["enrolled"]


async def test_a_turn_claimed_before_the_hold_keeps_its_group(pool, store: Storage):
    """It reserves the group only after its claim; moving the group would end it."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    claim = await _claim(store, "runner-a", _ARCHIVE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    await _moving_out(pool)

    assert await store.sandbox.release_moving_out_groups() == []

    assert (await _state(pool))["state"] == "stopped"
    reserved = await _reserve(store, follow_up, claim, "runner-a")
    assert reserved["host"] == "runner-a"


async def test_a_checkpoint_after_a_claimed_turn_does_not_move_its_group(pool, store: Storage):
    job, group = await _settled_stopped_group(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    claim = await _claim(store, "runner-a", _ARCHIVE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    await _moving_out(pool)

    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )

    assert (await _state(pool))["state"] == "stopped"
    assert (await _reserve(store, follow_up, claim, "runner-a"))["host"] == "runner-a"


async def _captures(pool, host: str = "runner-a") -> None:
    """The host declares that its stops capture checkpoints."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts "
            "SET capabilities = capabilities || '[\"sandbox-checkpoints-v1\"]'::jsonb "
            "WHERE host = $1",
            host,
        )


async def _running(pool, sandbox_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'running', container_id = 'container-a' "
            "WHERE id = $1",
            sandbox_id,
        )


async def test_removing_a_host_moves_its_groups_out_before_revoking_it(pool, store: Storage):
    job, group = await _settled_stopped_group(pool, store)
    await _running(pool, group["sandbox_id"])
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)

    assert await store.host_pool.remove_host(host="runner-a") == "moving_out"

    leaving = await store.host_pool.get_host(host="runner-a")
    assert leaving["status"] == "draining" and leaving["enrolled"]
    [row] = await store.sandbox.sandboxes_moving_out()
    assert row["sandbox_id"] == group["sandbox_id"]
    assert (
        await store.sandbox.request_move_out_stop(
            sandbox_id=row["sandbox_id"],
            generation=row["generation"],
            container_id=row["container_id"],
            actor_id="host-move-out",
        )
        == "accepted"
    )
    # Not revoked while a group is still bound to it.
    assert await store.host_pool.finish_host_removals() == []

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', stop_requested_at = NULL WHERE id = $1",
            group["sandbox_id"],
        )
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )
    assert (await _state(pool))["state"] == "cold"

    assert await store.host_pool.finish_host_removals() == ["runner-a"]
    assert not (await store.host_pool.get_host(host="runner-a"))["enrolled"]


async def test_removing_a_host_whose_groups_cannot_move_needs_consent(pool, store: Storage):
    """With no other host, a running group would stay on the removed host."""
    _, group = await _settled_stopped_group(pool, store)
    await _running(pool, group["sandbox_id"])
    await _captures(pool)

    assert await store.host_pool.remove_host(host="runner-a") == "no_move_target"
    assert (await store.host_pool.get_host(host="runner-a"))["enrolled"]

    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    assert (await _state(pool))["host"] == "runner-a"


async def test_putting_a_removed_host_back_to_work_stops_its_move(pool, store: Storage):
    _, group = await _settled_stopped_group(pool, store)
    await _running(pool, group["sandbox_id"])
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)
    assert await store.host_pool.remove_host(host="runner-a") == "moving_out"
    assert len(await store.sandbox.sandboxes_moving_out()) == 1

    assert await store.host_pool.set_host_status(host="runner-a", status="online") == "ok"

    assert await store.sandbox.sandboxes_moving_out() == []
    assert await store.host_pool.finish_host_removals() == []
    assert (await store.host_pool.get_host(host="runner-a"))["enrolled"]


async def test_removal_counts_a_checkpoint_that_no_longer_covers_its_group(pool, store: Storage):
    """A checkpoint of a local image moves nothing; revoking would strand the group."""
    _, group = await _covered(pool, store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET sandbox_image_digest = 'cloud-agent-sandbox:local' "
            "WHERE id = $1",
            group["sandbox_id"],
        )
        await conn.execute(
            "UPDATE agent_sandbox_checkpoints SET base_image = 'cloud-agent-sandbox:local' "
            "WHERE sandbox_id = $1",
            group["sandbox_id"],
        )
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)

    assert await store.host_pool.remove_host(host="runner-a") == "cannot_move"
    assert (await store.host_pool.get_host(host="runner-a"))["enrolled"]

    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    state = await _state(pool)
    assert state["state"] == "stopped" and state["host"] == "runner-a"


async def test_removal_does_not_wait_for_a_stopped_group_without_a_checkpoint(pool, store: Storage):
    """Nothing checkpoints a group already stopped, so it would wait forever."""
    await _settled_stopped_group(pool, store)
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)

    assert await store.host_pool.remove_host(host="runner-a") == "cannot_move"
    assert await store.sandbox.sandboxes_moving_out() == []
    assert (await store.host_pool.get_host(host="runner-a"))["status"] != "draining"


async def test_a_checkpoint_failing_during_the_move_holds_the_removal_for_consent(
    pool, store: Storage
):
    _, group = await _settled_stopped_group(pool, store)
    await _running(pool, group["sandbox_id"])
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)
    assert await store.host_pool.remove_host(host="runner-a") == "moving_out"

    # The stop completes, but its checkpoint failed.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', stop_requested_at = NULL WHERE id = $1",
            group["sandbox_id"],
        )
    assert await store.host_pool.finish_host_removals() == []
    hosts, *_ = await store.host_pool.runner_pool()
    [leaving] = [row for row in hosts if row["host"] == "runner-a"]
    assert leaving["sandboxes_left"] == 0 and leaving["sandboxes_unmovable"] == 1

    assert await store.host_pool.remove_host(host="runner-a") == "cannot_move"
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "moving_out"
    assert await store.host_pool.finish_host_removals() == ["runner-a"]
    assert (await _state(pool))["host"] == "runner-a"
