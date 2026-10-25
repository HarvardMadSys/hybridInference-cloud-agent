"""A released drain receipt outlives the host going offline between rollouts."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from uuid import uuid4

import pytest
from tests.integration.storage.test_host_pool_store import (
    HOST_DRAINING,
    HOST_OFFLINE,
    HOST_ONLINE,
    PostgresHostPoolRepository,
    _age_heartbeat,
    _drain_epoch,
    _enrol,
    db,  # noqa: F401 - pytest fixture
    hosts,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_a_released_drain_receipt_survives_a_missed_heartbeat_between_rollouts(
    db, store, hosts: PostgresHostPoolRepository
):
    """The next rollout must still find the token its predecessor released.

    This failed on staging: the reaper erased the receipt along with marking
    the host offline, so after a four-minute network blip every later rollout
    saw a host with no receipt and refused to take ownership.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    first = uuid4()
    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=await _drain_epoch(hosts),
        fence_token=first,
    )
    released = await hosts.release_host_drain_fence(host="runner-a", fence_token=first)
    assert acquired[0] == released[0] == "ok"

    await _age_heartbeat(db, store, "runner-a", seconds=600)
    assert await hosts.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == ["runner-a"]
    offline = await hosts.get_host(host="runner-a")
    assert offline["status"] == HOST_OFFLINE
    assert offline["drain_fence_token"] == first
    assert offline["drain_fence_status_before"] == HOST_ONLINE
    assert offline["drain_fence_active"] is False
    assert offline["drain_fence_epoch"] != released[1]

    await hosts.record_host_heartbeat(host="runner-a")
    returned_epoch = await _drain_epoch(hosts)
    assert (await hosts.get_host(host="runner-a"))["status"] == HOST_ONLINE

    # A delayed request carrying the pre-blip epoch is still fenced.
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=first,
        expected_fence_epoch=released[1],
        fence_token=uuid4(),
    ) == ("stale", None, returned_epoch)

    second = uuid4()
    transferred = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=first,
        expected_fence_epoch=returned_epoch,
        fence_token=second,
    )
    assert transferred[:2] == ("ok", HOST_ONLINE)
    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_DRAINING
    assert row["drain_fence_token"] == second
    assert row["drain_fence_active"] is True
