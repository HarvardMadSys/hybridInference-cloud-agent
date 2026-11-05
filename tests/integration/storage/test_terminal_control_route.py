"""The one read behind an attached terminal's keystrokes, against a real database.

A terminal's input, resize and kill used to read the whole job, then the
thread's readiness, then the job's host, and to fence the group's start in a
write transaction between them. ``terminal_control_route`` answers the same
questions in one statement and writes nothing. What only Postgres can settle is
that its answers are the ones those reads give, in each state they tell apart,
and that it neither changes nor waits on the group's row.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _reserve_retained_chat,
    _waiting_on,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_sandbox_restore_store import (
    _RESTORE_HOST,
    _claim as _claim_on,
    _covered,
    _fence,
    _host,
    _receipt,
    _reserve,
    _settled_stopped_group,
    _state,
)
from tests.support.follow_ups import append_follow_up

if TYPE_CHECKING:
    from cloud_agent.ports.sandbox import TerminalControlRoute
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_OWNER = "user-1"
_OTHER = "user-2"
_MANAGER_URL = "http://sandbox-manager.runner-a.test:8092"


async def _route(store: Storage, job: dict, user_id: str = _OWNER) -> TerminalControlRoute:
    route = await store.sandbox.terminal_control_route(job_id=job["id"], user_id=user_id)
    assert route is not None
    return route


async def _answers_as_the_reads_it_replaced(store: Storage, job: dict) -> TerminalControlRoute:
    """The route, after checking it against the three reads the routes made before."""
    route = await _route(store, job)
    full = await store.conversation_queries.get_job(job["id"])
    assert full is not None
    sandbox = full.get("thread_sandbox")
    assert route.thread_id == full["thread_id"]
    assert route.workspace_ready is (
        sandbox is not None
        and await store.sandbox.thread_workspace_ready(thread_id=str(full["thread_id"]))
    )
    assert route.terminal_resume_pending is bool(full["terminal_resume_pending"])
    assert route.group_state == (None if sandbox is None else sandbox["state"])
    host = await store.host_pool.host_for_job(job_id=job["id"])
    assert route.host == (
        None
        if host is None
        else {"host": host["host"], "sandbox_manager_url": host["sandbox_manager_url"]}
    )
    return route


async def _group(pool: Any, sandbox_id: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, start_seq, restore_checkpoint_id, updated_at "
            "FROM agent_sandboxes WHERE id = $1",
            sandbox_id,
        )
    assert row is not None
    return dict(row)


async def test_the_route_answers_as_the_reads_it_replaced_through_a_turn(store: Storage):
    """Readiness closes while a turn prepares and captures, as the thread-wide fence does."""
    job, claim, sandbox = await _reserve_retained_chat(store)
    await store.host_pool.record_host_heartbeat(host="runner-a", sandbox_manager_url=_MANAGER_URL)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }

    reserved = await _answers_as_the_reads_it_replaced(store, job)
    assert (reserved.group_state, reserved.workspace_ready) == ("starting", False)
    assert reserved.host == {"host": "runner-a", "sandbox_manager_url": _MANAGER_URL}

    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-retained-one", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    preparing = await _answers_as_the_reads_it_replaced(store, job)
    assert (preparing.group_state, preparing.workspace_ready) == ("running", False)

    for phase, ready in (("workspace_ready", True), ("workspace_finalizing", False)):
        assert await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="lifecycle",
            payload={"phase": phase},
        )
        assert (await _answers_as_the_reads_it_replaced(store, job)).workspace_ready is ready

    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="new",
        native_session_id="native-session-one",
        idle_timeout_seconds=1800,
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    settled = await _answers_as_the_reads_it_replaced(store, job)
    assert (settled.group_state, settled.workspace_ready, settled.host_revoked) == (
        "running",
        True,
        False,
    )
    assert settled.terminal_resume_pending is False


async def test_the_route_is_only_read_by_the_jobs_owner(pool, store: Storage):
    """Ownership is ``owns_job``'s: another owner, a missing job and a deleted chat read nothing."""
    job, _claim_row, _sandbox = await _reserve_retained_chat(store)

    for user_id in (_OWNER, _OTHER):
        owned = await store.conversation_queries.owns_job(job_id=job["id"], user_id=user_id)
        route = await store.sandbox.terminal_control_route(job_id=job["id"], user_id=user_id)
        assert (route is not None) is owned
    assert await store.sandbox.terminal_control_route(job_id="ajob_missing", user_id=_OWNER) is None

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", job["thread_id"]
        )
    assert not await store.conversation_queries.owns_job(job_id=job["id"], user_id=_OWNER)
    assert await store.sandbox.terminal_control_route(job_id=job["id"], user_id=_OWNER) is None


async def test_a_chat_without_a_group_is_routed_by_its_running_attempt(store: Storage):
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == job["id"]

    route = await _answers_as_the_reads_it_replaced(store, job)

    assert (route.group_state, route.workspace_ready, route.host_revoked) == (None, False, False)
    assert route.host is not None and route.host["host"] == "runner-a"


async def test_a_settled_turns_pending_terminal_resume_is_reported(pool, store: Storage):
    job, _group_row = await _settled_stopped_group(pool, store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET terminal_resume_pending = TRUE WHERE id = $1", job["id"]
        )

    route = await _answers_as_the_reads_it_replaced(store, job)

    assert route.terminal_resume_pending is True


async def test_reading_the_route_fences_nothing(pool, store: Storage):
    """A keystroke no longer counts a start, so the checkpoint keeps covering the group.

    The start fence a workspace request takes drops the group's restore point:
    after it, the group can no longer move with its last checkpoint. Input into
    a terminal cannot start the container, and its read leaves the row as it was.
    """
    job, group = await _covered(pool, store)
    before = await _group(pool, group["sandbox_id"])
    assert before["restore_checkpoint_id"] == "c" * 32

    for _ in range(3):
        route = await _answers_as_the_reads_it_replaced(store, job)
        assert (route.group_state, route.host_revoked) == ("stopped", False)

    assert await _group(pool, group["sandbox_id"]) == before


async def test_the_route_reads_past_a_group_row_another_transaction_holds(pool, store: Storage):
    """Heartbeats and admission lock the group's row; the start fence waited for them."""
    job, group = await _settled_stopped_group(pool, store)

    async with pool.acquire() as holder, holder.transaction():
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        await holder.execute(
            "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE", group["sandbox_id"]
        )
        route = await asyncio.wait_for(
            store.sandbox.terminal_control_route(job_id=job["id"], user_id=_OWNER), timeout=5
        )
        fence = asyncio.create_task(store.sandbox.fence_workspace_start(thread_id=job["thread_id"]))
        # The general path's fence waits for the row; the read above did not.
        await _waiting_on(pool, holder_pid)

    assert route is not None and route.group_state == "stopped"
    assert await fence is not None


async def test_a_moved_group_is_reached_on_its_new_host_from_an_earlier_turn(pool, store: Storage):
    """An earlier turn's attempt ran on the host the group has left; its keystrokes follow the group."""
    job, _group_row = await _covered(pool, store)
    follow_up = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=_OWNER, prompt="continue"
    )
    assert follow_up is not None
    assert await store.host_pool.remove_host(host="runner-a") == "ok"
    await _host(store, "runner-c", _RESTORE_HOST)
    claim = await _claim_on(store, "runner-c", _RESTORE_HOST)
    assert claim is not None and claim["id"] == follow_up["id"]
    reserved = await _reserve(store, follow_up, claim, "runner-c")
    assert await store.sandbox.mark_thread_sandbox_running(
        **_fence(follow_up, claim, reserved),
        container_id="container-c",
        context_status="restored",
    )

    for turn in (job, follow_up):
        route = await _answers_as_the_reads_it_replaced(store, turn)
        assert route.host is not None and route.host["host"] == "runner-c"
        assert (route.group_state, route.host_revoked) == ("running", False)


async def test_a_group_its_checkpoint_holds_is_reported_cold(pool, store: Storage):
    job, _group_row = await _covered(pool, store)
    assert await store.host_pool.remove_host(host="runner-a") == "ok"

    route = await _answers_as_the_reads_it_replaced(store, job)

    assert (route.group_state, route.host_revoked) == ("cold", True)


async def test_a_group_on_a_removed_host_is_reported_without_being_handed_on(pool, store: Storage):
    """The read only reports the removed host: handing the group on stays the fence's.

    A checkpoint that lands after the removal covers the group. The start
    fence then moves it on rather than start it; the read changes nothing, so
    the request that falls back to that fence still finds it to move.
    """
    job, group = await _settled_stopped_group(pool, store)
    assert await store.host_pool.remove_host(host="runner-a", allow_unmoved=True) == "ok"
    assert await store.sandbox.record_sandbox_checkpoint(
        host="runner-a", thread_id=job["thread_id"], checkpoint=_receipt(group, job)
    )

    route = await _answers_as_the_reads_it_replaced(store, job)

    assert (route.group_state, route.host_revoked) == ("stopped", True)
    assert (await _state(pool))["state"] == "stopped"
    assert await store.sandbox.fence_workspace_start(thread_id=job["thread_id"]) == ("cold", None)
