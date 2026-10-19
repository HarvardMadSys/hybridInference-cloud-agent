"""Real Postgres checks for moving a leaving Host's groups out one at a time."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_sandbox_restore_store import (
    _ARCHIVE_HOST,
    _RESTORE_HOST,
    _claim,
    _covered,
    _enable_archives,
    _fence,
    _host,
    _receipt,
    _reserve,
    _settled_stopped_group,
    _state,
    pool as pool,
    store as store,
)

from cloud_agent.execution_profiles import KATA_RUNTIME
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.workers import sandbox_deletion

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


async def _run_claimed_turn(
    store: Storage, job: dict, claim: dict, host: str, container_id: str, base_sha: str
) -> None:
    """The claimed turn starts its group, runs and settles; the group stays up."""
    sandbox = await _reserve(store, job, claim, host)
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
    fence = _fence(job, claim, sandbox)
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id=container_id, context_status="restored"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=residency["generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=residency["generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-restore",
        idle_timeout_seconds=15,
        base_sha=base_sha,
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )


async def test_a_turn_claimed_before_the_removal_moves_out_the_group_it_starts(
    pool, store: Storage
):
    """The group's checkpoint covers it now, but the turn starts it before any revocation."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    claim = await _claim(store, "runner-a", _ARCHIVE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)

    removal = await store.host_pool.remove_host(host="runner-a")
    await _run_claimed_turn(store, follow_up, claim, "runner-a", "container-a", job["base_sha"])

    # The group runs on the host now; revoking it would leave the group there.
    assert await store.host_pool.finish_host_removals() == []
    assert removal == "moving_out"
    [row] = await store.sandbox.sandboxes_moving_out()
    assert row["sandbox_id"] == "asbx_restore"
    assert (
        await store.sandbox.request_move_out_stop(
            sandbox_id=row["sandbox_id"],
            generation=row["generation"],
            container_id=row["container_id"],
            actor_id="host-move-out",
        )
        == "accepted"
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'stopped', stop_requested_at = NULL WHERE id = $1",
            row["sandbox_id"],
        )
    stopped = await store.sandbox.thread_sandbox(thread_id=follow_up["thread_id"])
    assert stopped is not None
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a",
        thread_id=follow_up["thread_id"],
        checkpoint=_receipt(stopped, follow_up, checkpoint_id="f" * 32),
    )
    assert (await _state(pool))["state"] == "cold"
    assert await store.host_pool.finish_host_removals() == ["runner-a"]


async def test_removing_a_host_while_it_restores_a_group_moves_that_group_out(pool, store: Storage):
    """A restoring group is not counted, yet it runs on the host once its turn ends."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    await _captures(pool, "runner-c")
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    assert (await _state(pool))["state"] == "restoring"
    await _host(store, "runner-d", _RESTORE_HOST)

    removal = await store.host_pool.remove_host(host="runner-c")
    await _run_claimed_turn(store, follow_up, claim, "runner-c", "container-c", job["base_sha"])

    restored = await _state(pool)
    assert restored["state"] == "running" and restored["host"] == "runner-c"
    # Revoking runner-c now would leave the restored group on it.
    assert await store.host_pool.finish_host_removals() == []
    assert removal == "moving_out"
    [row] = await store.sandbox.sandboxes_moving_out()
    assert row["sandbox_id"] == "asbx_restore" and row["host"] == "runner-c"


async def test_a_group_whose_container_was_never_created_does_not_hold_the_removal(
    pool, store: Storage
):
    """Nothing can stop it into a checkpoint, so a move would wait for it forever."""
    await _enable_archives(store)
    await _host(store, "runner-a", _ARCHIVE_HOST)
    job = await _create_job(store)
    claim = await _claim(store, "runner-a", _ARCHIVE_HOST)
    assert claim is not None and claim["id"] == job["id"]
    sandbox = await _reserve(store, job, claim, "runner-a")
    # Docker never created the container: the first turn gives up before
    # admission, as the runner does when its image cannot be pulled.
    abandoned = await store.sandbox.abandon_thread_sandbox(
        **_fence(job, claim, sandbox),
        lost=False,
        detail="image pull failed",
        idle_timeout_seconds=15,
    )
    assert abandoned is not None and abandoned["state"] == "provisioning"
    assert abandoned["container_id"] is None
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
    )
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)

    removal = await store.host_pool.remove_host(host="runner-a")

    # The sweep has nothing it could stop, so a move would never end.
    assert await store.sandbox.sandboxes_moving_out() == []
    assert removal == "cannot_move"
    hosts, *_ = await store.host_pool.runner_pool()
    [kept] = [row for row in hosts if row["host"] == "runner-a"]
    assert kept["sandboxes_left"] == 0 and kept["sandboxes_unmovable"] == 1
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    assert not (await store.host_pool.get_host(host="runner-a"))["enrolled"]


async def _on_machine(pool, allocation_id: str, *, observed_state: str = "ready") -> None:
    """Bind ``runner-a`` to one provider machine in ``observed_state``."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_compute_allocations (id,name,provider_id,mode,observed_state) "
            "VALUES ($1,$1,'cloudlab','provision',$2)",
            allocation_id,
            observed_state,
        )
        await conn.execute(
            "INSERT INTO agent_compute_nodes "
            "(id,allocation_id,provider_node_id,instance_identity,name) "
            "VALUES ($1 || '-node',$1,'node0',$1 || ':node0',$1)",
            allocation_id,
        )
        await conn.execute(
            "UPDATE agent_runner_hosts SET compute_node_id=$1 || '-node' WHERE host='runner-a'",
            allocation_id,
        )


async def _silent(pool, host: str = "runner-a", minutes: int = 20) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts "
            "SET last_heartbeat_at = NOW() - make_interval(mins => $2) WHERE host = $1",
            host,
            minutes,
        )


async def test_a_reclaimed_machines_uncovered_group_is_lost_once_its_host_is_silent(
    pool, store: Storage
):
    """Its only Docker copy is gone; its queued turns get an answer instead of waiting."""
    job, group = await _settled_stopped_group(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    await _on_machine(pool, "ended", observed_state="terminated")
    repo = store.compute_resources

    # The Host still beat moments ago: the attempt reaper and the Host itself go first.
    assert await repo.lose_reclaimed_groups(allocation_id="ended", quiet_seconds=600) == []
    assert (await _state(pool))["state"] == "stopped"

    await _silent(pool)
    assert await repo.lose_reclaimed_groups(allocation_id="ended", quiet_seconds=600) == [
        group["sandbox_id"]
    ]
    assert (await _state(pool))["state"] == "lost"
    async with pool.acquire() as conn:
        member = await conn.fetchrow(
            "SELECT context_status, native_state FROM agent_thread_sandbox_memberships "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
        queued = await conn.fetchrow(
            "SELECT state, detail FROM agent_jobs WHERE id = $1", follow_up["id"]
        )
        event = await conn.fetchrow(
            "SELECT event_type, from_state, to_state, details FROM agent_sandbox_events "
            "WHERE sandbox_id = $1 ORDER BY id DESC LIMIT 1",
            group["sandbox_id"],
        )
    assert (member["context_status"], member["native_state"]) == ("lost", "lost")
    assert queued["state"] == "failed"
    assert "reclaimed by its provider" in queued["detail"]
    assert (event["event_type"], event["from_state"], event["to_state"]) == (
        "server_reclaimed",
        "stopped",
        "lost",
    )
    # Once lost, a later pass changes nothing.
    assert await repo.lose_reclaimed_groups(allocation_id="ended", quiet_seconds=600) == []


async def test_a_reclaimed_machines_covered_group_moves_instead_of_being_lost(pool, store: Storage):
    await _covered(pool, store)
    await _on_machine(pool, "ended")
    await store.compute_resources.observe_allocation(
        allocation_id="ended", observation={"observed_state": "terminated"}
    )
    await _silent(pool)

    assert (
        await store.compute_resources.lose_reclaimed_groups(
            allocation_id="ended", quiet_seconds=600
        )
        == []
    )
    assert (await _state(pool))["state"] == "cold"


async def test_a_machine_the_provider_still_runs_keeps_its_groups(pool, store: Storage):
    await _settled_stopped_group(pool, store)
    await _on_machine(pool, "running")
    await _silent(pool)

    assert (
        await store.compute_resources.lose_reclaimed_groups(
            allocation_id="running", quiet_seconds=600
        )
        == []
    )
    assert (await _state(pool))["state"] == "stopped"


async def test_a_group_with_a_turn_in_flight_waits_for_the_reaper(pool, store: Storage):
    job, _ = await _settled_stopped_group(pool, store)
    await _on_machine(pool, "ended", observed_state="terminated")
    await _silent(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'running' WHERE id = $1",
            job["id"],
        )

    assert (
        await store.compute_resources.lose_reclaimed_groups(
            allocation_id="ended", quiet_seconds=600
        )
        == []
    )
    assert (await _state(pool))["state"] == "stopped"


async def test_the_move_out_report_names_why_groups_would_stay(pool, store: Storage):
    await _settled_stopped_group(pool, store)
    await _on_machine(pool, "leaving")
    repo = store.compute_resources

    # runner-a archives but does not capture checkpoints.
    report = await repo.move_out_report(allocation_id="leaving")
    assert report == {
        "moving": False,
        "blocker": "host_without_checkpoints",
        "unmoved": 1,
        "unmovable": 1,
    }

    await _host(store, "runner-a", [*_RESTORE_HOST, "sandbox-checkpoints-v1"])
    assert (await repo.move_out_report(allocation_id="leaving"))["blocker"] == "no_restore_target"

    await _host(store, "runner-c", _RESTORE_HOST)
    report = await repo.move_out_report(allocation_id="leaving")
    # The move can start, but this stopped group has no checkpoint to move with.
    assert (report["blocker"], report["unmoved"], report["unmovable"]) == (None, 1, 1)


async def _untrusted(pool, sandbox_id: str = "asbx_restore") -> None:
    """The group and its chats run ``untrusted-kata-v1``, the deployed default."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET execution_profile = 'untrusted-kata-v1' WHERE id IN ("
            "SELECT thread_id FROM agent_thread_sandbox_memberships WHERE sandbox_id = $1)",
            sandbox_id,
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET execution_profile = 'untrusted-kata-v1' WHERE id = $1",
            sandbox_id,
        )


async def _kata(store: Storage, host: str) -> None:
    """The host's runner reports the Kata runtime, the only one an untrusted chat runs on."""
    assert await store.host_pool.record_host_heartbeat(
        host=host, credential_hash=f"{host}-hash", sandbox_runtime=KATA_RUNTIME
    )


async def test_a_removal_does_not_move_an_untrusted_group_to_a_host_without_kata(
    pool, store: Storage
):
    """Its next turn is claimed only under Kata, so a runc Host would never restore it."""
    _, group = await _settled_stopped_group(pool, store)
    await _untrusted(pool)
    await _running(pool, group["sandbox_id"])
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)

    assert await store.host_pool.remove_host(host="runner-a") == "no_move_target"
    assert (await store.host_pool.get_host(host="runner-a"))["status"] == "online"

    await _kata(store, "runner-c")
    assert await store.host_pool.remove_host(host="runner-a") == "moving_out"


async def test_a_lease_move_out_needs_a_host_that_runs_its_untrusted_groups(pool, store: Storage):
    _, group = await _settled_stopped_group(pool, store)
    await _untrusted(pool)
    await _running(pool, group["sandbox_id"])
    await _captures(pool)
    await _on_machine(pool, "leaving")
    await _host(store, "runner-c", _RESTORE_HOST)
    repo = store.compute_resources

    # Holding every claim now would only strand the chat: nothing could restore it.
    assert await repo.set_move_out_hold(allocation_id="leaving", due=True) is False
    assert (await repo.move_out_report(allocation_id="leaving"))["blocker"] == "no_restore_target"

    await _kata(store, "runner-c")
    assert await repo.set_move_out_hold(allocation_id="leaving", due=True) is True


async def _deletion_failed(store: Storage, sandbox_id: str = "asbx_restore") -> None:
    """An administrator's deletion fails before its archive; the group serves its chats on."""
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None
    assert (
        await store.sandbox.request_admin_delete(
            sandbox_id=sandbox_id,
            generation=detail.sandbox.generation,
            container_id=detail.sandbox.container_id,
            last_activity_at=detail.sandbox.last_activity_at,
            actor_id="admin:operator",
            admin_ip="127.0.0.1",
        )
        == "accepted"
    )
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=60)
    assert claim is not None
    assert await store.sandbox.fail_admin_sandbox_deletion(
        sandbox_id=sandbox_id,
        request_id=claim["deletion_request_id"],
        claim_token=claim["deletion_claim_token"],
        generation=claim["generation"],
        container_id=claim["container_id"],
        error="The archive was refused before deletion.",
    )
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None and detail.sandbox.deletion_state == "failed"


async def test_a_removed_hosts_covered_group_whose_deletion_failed_is_handed_over(
    pool, store: Storage
):
    """The claim still serves such a group, so its checkpoint must still move it."""
    job, _ = await _covered(pool, store)
    await _deletion_failed(store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None

    assert await store.host_pool.remove_host(host="runner-a") == "ok"

    assert (await _state(pool))["state"] == "cold"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]


async def test_a_removal_moves_out_a_running_group_whose_deletion_failed(pool, store: Storage):
    _, group = await _settled_stopped_group(pool, store)
    await _deletion_failed(store)
    await _running(pool, group["sandbox_id"])
    await _captures(pool)
    await _host(store, "runner-c", _RESTORE_HOST)

    # Revoked at once, the host would keep the group and its chat would wait for good.
    assert await store.host_pool.remove_host(host="runner-a") == "moving_out"
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


async def test_a_reclaimed_machine_loses_a_group_whose_deletion_failed(pool, store: Storage):
    job, group = await _settled_stopped_group(pool, store)
    await _deletion_failed(store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    await _on_machine(pool, "ended", observed_state="terminated")
    await _silent(pool)

    assert await store.compute_resources.lose_reclaimed_groups(
        allocation_id="ended", quiet_seconds=600
    ) == [group["sandbox_id"]]
    async with pool.acquire() as conn:
        queued = await conn.fetchval("SELECT state FROM agent_jobs WHERE id = $1", follow_up["id"])
    assert queued == "failed"


async def _request_discard(store: Storage, sandbox_id: str = "asbx_restore") -> None:
    """An administrator deletes the stopped group without an S3 archive."""
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None
    assert (
        await store.sandbox.request_admin_delete(
            sandbox_id=sandbox_id,
            generation=detail.sandbox.generation,
            container_id=detail.sandbox.container_id,
            last_activity_at=detail.sandbox.last_activity_at,
            actor_id="admin:operator",
            admin_ip="127.0.0.1",
            discard=True,
        )
        == "accepted"
    )


def _unreachable_sandbox_manager(monkeypatch) -> AsyncMock:
    """Every call to the owning host's sandbox manager fails to connect."""
    failure = SandboxManagerError(503, "The shared chat supervisor is temporarily unavailable.")
    failure.__cause__ = httpx.ConnectError("connection refused")
    discard = AsyncMock(side_effect=failure)
    monkeypatch.setattr(
        sandbox_deletion,
        "sandbox_manager_for_host",
        lambda _host: SimpleNamespace(discard_sandbox_deletion=discard),
    )
    return discard


async def _deletion(pool, sandbox_id: str = "asbx_restore") -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, deletion_state, deletion_error FROM agent_sandboxes WHERE id = $1",
            sandbox_id,
        )
    assert row is not None
    return dict(row)


async def _due_again(pool, sandbox_id: str = "asbx_restore") -> None:
    """The minute a failed deletion attempt waits before its retry has passed."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_retry_at = NOW() WHERE id = $1", sandbox_id
        )


async def test_a_reclaimed_machine_finishes_a_pending_discard_without_its_host(
    pool, store: Storage, monkeypatch
):
    """The container went with the machine; the host the discard waits for never answers."""
    job, group = await _settled_stopped_group(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    await _request_discard(store)
    discard = _unreachable_sandbox_manager(monkeypatch)
    await _on_machine(pool, "ended", observed_state="terminated")

    # The Host beat moments ago and may still hold the container: wait for it.
    assert await sandbox_deletion.delete_one_archived_sandbox(store)
    assert (await _deletion(pool))["deletion_state"] == "pending"
    discard.assert_awaited_once()

    await _silent(pool)
    await _due_again(pool)
    assert await sandbox_deletion.delete_one_archived_sandbox(store)

    deletion = await _deletion(pool)
    assert (deletion["state"], deletion["deletion_state"]) == ("lost", "deleted")
    assert deletion["deletion_error"] is None
    discard.assert_awaited_once()
    async with pool.acquire() as conn:
        member = await conn.fetchrow(
            "SELECT context_status, native_state FROM agent_thread_sandbox_memberships "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
        queued = await conn.fetchval("SELECT state FROM agent_jobs WHERE id = $1", follow_up["id"])
        event = await conn.fetchrow(
            "SELECT from_state, to_state, details->>'host_reclaimed' AS reclaimed "
            "FROM agent_sandbox_events WHERE sandbox_id = $1 AND event_type = 'deletion_completed'",
            group["sandbox_id"],
        )
        audit = await conn.fetchrow(
            "SELECT action, details->>'host_reclaimed' AS reclaimed "
            "FROM agent_admin_audit_log ORDER BY id DESC LIMIT 1"
        )
    assert (member["context_status"], member["native_state"]) == ("lost", "lost")
    assert queued == "failed"
    assert dict(event) == {"from_state": "pending", "to_state": "deleted", "reclaimed": "true"}
    assert dict(audit) == {"action": "agent.sandbox.delete.discarded", "reclaimed": "true"}
    # Nothing is left for the reclaimed machine's own pass.
    assert (
        await store.compute_resources.lose_reclaimed_groups(
            allocation_id="ended", quiet_seconds=600
        )
        == []
    )


async def test_a_discard_on_a_machine_its_provider_still_runs_keeps_waiting_for_its_host(
    pool, store: Storage, monkeypatch
):
    """A silent Host on a live machine may still hold the container and return."""
    await _settled_stopped_group(pool, store)
    await _request_discard(store)
    discard = _unreachable_sandbox_manager(monkeypatch)
    await _on_machine(pool, "running")
    await _silent(pool)

    assert await sandbox_deletion.delete_one_archived_sandbox(store)

    deletion = await _deletion(pool)
    assert (deletion["state"], deletion["deletion_state"]) == ("stopped", "pending")
    discard.assert_awaited_once()
    # The page shows what the deletion is waiting for, and what ends the wait.
    assert deletion["deletion_error"] == (
        "Container removal without an archive could not be confirmed. "
        "The owning host is offline, and only it can remove the container: "
        "deletion finishes when the host reconnects, "
        "or without it once its provider confirms the machine is gone. "
        "Deletion will be retried."
    )


async def test_a_discard_on_a_removed_host_says_it_waits_for_that_host(
    pool, store: Storage, monkeypatch
):
    """A removed Host keeps its machine; enrolled again, it removes the container."""
    await _settled_stopped_group(pool, store)
    await _request_discard(store)
    _unreachable_sandbox_manager(monkeypatch)
    assert await store.host_pool.remove_host(host="runner-a") == "ok"

    assert await sandbox_deletion.delete_one_archived_sandbox(store)

    deletion = await _deletion(pool)
    assert (deletion["state"], deletion["deletion_state"]) == ("stopped", "pending")
    assert deletion["deletion_error"] == (
        "Container removal without an archive could not be confirmed. "
        "The owning host was removed from the pool, and only it can remove the container: "
        "deletion finishes if that host is enrolled again. Deletion will be retried."
    )


async def _waiting(pool, job_id: str) -> None:
    """The turn has waited longer than the queue alert allows."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET updated_at = NOW() - INTERVAL '11 minutes' WHERE id = $1",
            job_id,
        )


async def _queue(store: Storage) -> tuple[int, int]:
    health = await store.host_pool.queue_health(expected_deployment_sha="test-deployment")
    return health["queued"], health["unavailable"]


async def test_queue_health_reports_a_moved_groups_turn_no_host_can_restore(pool, store: Storage):
    """The Host the group left still serves, but never claims its own cold group."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    await _moving_out(pool)
    assert await store.sandbox.release_moving_out_groups() == ["asbx_restore"]
    await _waiting(pool, follow_up["id"])

    assert await _queue(store) == (1, 1)

    await _host(store, "runner-c", _RESTORE_HOST)
    assert await _queue(store) == (1, 0)


async def test_queue_health_lets_any_restore_host_take_a_removed_hosts_cold_group(
    pool, store: Storage
):
    """The Host it left is gone, which strands nothing while another can restore it."""
    job, _ = await _covered(pool, store)
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    assert (await _state(pool))["state"] == "cold"
    await _waiting(pool, follow_up["id"])

    assert await _queue(store) == (1, 1)

    await _host(store, "runner-c", _RESTORE_HOST)
    assert await _queue(store) == (1, 0)
