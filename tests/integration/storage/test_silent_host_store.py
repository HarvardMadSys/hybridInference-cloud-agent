"""Real Postgres checks for the groups of a Host that stopped heartbeating."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_sandbox_restore_store import (
    _ARCHIVE_HOST,
    _BASE,
    _RESTORE_HOST,
    _claim,
    _covered,
    _host,
    _settled_stopped_group,
    _shared_claim_attestation,
    _state,
    pool as pool,
    store as store,
)

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _silent_for(pool, seconds: int, host: str = "runner-a") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET last_heartbeat_at = NOW() - make_interval(secs => $2) "
            "WHERE host = $1",
            host,
            seconds,
        )


async def _follow_up(store: Storage, job: dict) -> dict:
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up is not None
    return follow_up


async def test_another_host_takes_a_long_silent_hosts_covered_group_at_its_claim(
    pool, store: Storage
):
    """A machine that died does not keep its chats waiting for its return."""
    job, _ = await _covered(pool, store)
    follow_up = await _follow_up(store, job)
    await _host(store, "runner-c", _RESTORE_HOST)

    await _silent_for(pool, 600)
    assert await _claim(store, "runner-c", _RESTORE_HOST) is None
    assert (await _state(pool))["state"] == "stopped"

    await _silent_for(pool, 7200)
    claim = await _claim(store, "runner-c", _RESTORE_HOST)

    assert claim is not None and claim["id"] == follow_up["id"]
    state = await _state(pool)
    assert state["state"] == "restoring" and state["host"] == "runner-c"
    # It keeps its credential: it may only be partitioned.
    assert (await store.host_pool.get_host(host="runner-a"))["enrolled"]


async def test_a_silent_host_with_no_other_host_continues_with_its_group_on_return(
    pool, store: Storage
):
    """Nothing moves without a taker, so the Host that comes back still has it."""
    job, group = await _covered(pool, store)
    follow_up = await _follow_up(store, job)
    await _silent_for(pool, 7200)
    # A host that cannot restore takes nothing.
    await _host(store, "runner-b", _ARCHIVE_HOST)
    assert await _claim(store, "runner-b", _ARCHIVE_HOST) is None

    await _silent_for(pool, 0)
    claim = await _claim(store, "runner-a", _ARCHIVE_HOST)

    assert claim is not None and claim["id"] == follow_up["id"]
    state = await _state(pool)
    assert state["state"] == "stopped" and state["host"] == "runner-a"
    assert state["container_id"] == group["container_id"]


async def test_a_claim_that_finds_no_slot_leaves_the_group_with_its_silent_host(
    pool, store: Storage
):
    job, _ = await _covered(pool, store)
    await _follow_up(store, job)
    await _host(store, "runner-c", _RESTORE_HOST)
    await _silent_for(pool, 7200)

    # A stale credential is refused at the slot, after the hand-over.
    refused = await store.attempt_commands.claim_job(
        worker_id="runner-c-worker",
        lease_ttl_seconds=60,
        host="runner-c",
        host_credential_hash="stale-hash",
        claim_attestation={
            **_shared_claim_attestation(),
            "sandbox_image_digest": _BASE,
            "runner_capabilities": _RESTORE_HOST,
            "sandbox_archive_settings_applied_revision": 1,
        },
    )

    assert refused is None
    state = await _state(pool)
    assert state["state"] == "stopped" and state["host"] == "runner-a"


async def test_a_silent_hosts_group_without_a_covering_checkpoint_stays(pool, store: Storage):
    job, _ = await _settled_stopped_group(pool, store)
    await _follow_up(store, job)
    await _host(store, "runner-c", _RESTORE_HOST)
    await _silent_for(pool, 7200)

    assert await _claim(store, "runner-c", _RESTORE_HOST) is None
    assert (await _state(pool))["state"] == "stopped"


async def test_a_turn_a_host_claimed_before_going_silent_keeps_its_group(pool, store: Storage):
    """The reaper requeues that turn first; only then may the group move."""
    job, _ = await _covered(pool, store)
    await _follow_up(store, job)
    assert await _claim(store, "runner-a", _ARCHIVE_HOST) is not None
    await _host(store, "runner-c", _RESTORE_HOST)

    await _silent_for(pool, 7200)

    assert await _claim(store, "runner-c", _RESTORE_HOST) is None
    assert (await _state(pool))["state"] == "stopped"
