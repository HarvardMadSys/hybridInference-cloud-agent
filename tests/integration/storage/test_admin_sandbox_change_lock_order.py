"""The admin inventory's change marker, taken after its group as every writer takes it.

Migration 0097's triggers mark a changed group in ``agent_admin_sandbox_changes``.
A write to the group holds the group's row when its trigger marks it. A
membership write marked the group straight after the membership, before the
group, and settling a turn writes its membership and only then its group. So a
settlement held the marker while it waited for the group, and a writer holding
the group waited for the marker: Postgres aborted one of them. Migration 0102
has the membership trigger take the group first.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_agent_job_store import (
    _reserve_shared_chat,
    _waiting_on,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)
from tests.integration.storage.test_lock_order import _close_a_cycle, _settle as _drop

from cloud_agent.storage._sql.sandbox import FENCE_START_SQL

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_CONTAINER = "container-admin-marker"

# ``fence_sandbox_start``'s two statements, which a workspace request runs to
# open a file, a terminal or a Git view: it holds the group between them.
_LOCK_GROUP = "SELECT id, state, host FROM agent_sandboxes WHERE id = $1 FOR UPDATE"
_FENCE_GROUP = (
    f"UPDATE agent_sandboxes SET {FENCE_START_SQL}, updated_at = NOW() "
    "WHERE id = $1 RETURNING state, start_seq"
)


async def _admitted_turn(store: Storage) -> tuple[dict, dict, dict]:
    """A chat whose turn is running in its group and was admitted, ready to settle."""
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
    fence["residency_generation"] = sandbox["residency_generation"]
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    return job, fence, sandbox


def _settlement(store: Storage, job: dict, fence: dict) -> asyncio.Task:
    """The runner's turn boundary: membership first, then the group."""
    return asyncio.create_task(
        store.sandbox.settle_thread_sandbox(
            **fence,
            job_id=job["id"],
            context_status="live",
            native_session_id="native-admin-marker",
            idle_timeout_seconds=1800,
            base_sha=job["base_sha"],
        )
    )


async def test_settling_a_turn_does_not_deadlock_with_the_admin_terminal_check(
    pool, store: Storage
):
    """The terminal check and a settlement both reach the marker through the group.

    The check writes the group in one statement, whose trigger then marks it.
    A check that reached the group between the settlement's membership write
    and its group write waited for the marker the settlement held. A workspace
    start holding the group fixes the order they arrive in: the check queues
    on the group first, and the settlement writes its membership and then
    queues behind the check.
    """
    job, fence, sandbox = await _admitted_turn(store)
    check = settle = None
    async with pool.acquire() as start:
        try:
            async with start.transaction():
                await start.execute(_LOCK_GROUP, sandbox["sandbox_id"])
                check = asyncio.create_task(
                    store.sandbox.record_terminal_observation(
                        sandbox_id=sandbox["sandbox_id"],
                        host=sandbox["host"],
                        generation=sandbox["generation"],
                        container_id=_CONTAINER,
                        terminal_count=1,
                    )
                )
                check_pid = await _waiting_on(pool, await start.fetchval("SELECT pg_backend_pid()"))
                settle = _settlement(store, job, fence)
                await _waiting_on(pool, check_pid)
            recorded, settled = await asyncio.wait_for(asyncio.gather(check, settle), timeout=10)
        finally:
            await _drop(check)
            await _drop(settle)
    assert recorded is True
    assert settled is not None and settled["context_status"] == "live"


async def test_settling_a_turn_does_not_deadlock_with_a_workspace_start(pool, store: Storage):
    """A settlement waits for a workspace start holding its group before it marks the group.

    The start locks the group and then writes its start fence. A settlement
    that wrote its membership in between held the group's marker while it
    waited for the group, and the start's write then waited for that marker.
    """
    job, fence, sandbox = await _admitted_turn(store)
    settle = None
    async with pool.acquire() as start:
        try:
            async with start.transaction():
                await start.execute(_LOCK_GROUP, sandbox["sandbox_id"])
                settle = _settlement(store, job, fence)
                await _waiting_on(pool, await start.fetchval("SELECT pg_backend_pid()"))
                await _close_a_cycle(start, _FENCE_GROUP, sandbox["sandbox_id"])
            settled = await asyncio.wait_for(settle, timeout=10)
        finally:
            await _drop(settle)
    assert settled is not None and settled["context_status"] == "live"
