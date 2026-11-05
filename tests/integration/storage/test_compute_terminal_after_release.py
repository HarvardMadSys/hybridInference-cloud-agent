"""A queued read must not resurrect a released immutable resource."""

from datetime import UTC, datetime

import pytest
from tests.integration.storage.test_compute_workflow import (
    db as db,
    hosts as hosts,
    rig,
    run_next,
    store as store,
)

from cloud_agent.ports.external.compute_provider import ProviderReceipt

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest.mark.parametrize("stale_state", ["pending", "ready", "unknown"])
async def test_serialized_inspect_after_release_preserves_terminal_observation(
    db, store, hosts, monkeypatch, stale_state
):
    """The dispatcher serializes calls; a provider can still return stale state."""
    r = await rig(store, hosts, monkeypatch)
    assert (await run_next(r))["state"] == "succeeded"
    allocation_id = r.op["allocation_id"]
    terminal = r.observation.model_copy(
        update={
            "state": "terminated",
            "raw_state": "delete_acknowledged",
            "nodes": (),
            "observed_at": datetime.now(UTC),
            "provider_http_status": 204,
        }
    )
    r.provider.release.return_value = ProviderReceipt(
        outcome="confirmed", resource=terminal.ref, observation=terminal
    )
    retire = await r.repo.create_operation(
        request_id="serial-retire",
        fingerprint="serial-retire",
        kind="retire",
        allocation_id=allocation_id,
        parameters={},
        requested_by="test",
    )
    inspect = await r.repo.create_operation(
        request_id="queued-inspect",
        fingerprint="queued-inspect",
        kind="inspect",
        allocation_id=allocation_id,
        parameters={},
        requested_by="test",
    )
    released = await run_next(r)
    assert (released["id"], released["state"]) == (retire["id"], "succeeded")
    before = await r.repo.get_allocation(allocation_id)
    assert before["observed_state"] == "terminated"
    assert before["release_committed_at"] is not None

    # The next read begins only after retire has completed. The stale provider
    # response must not downgrade even the original proof's source and time.
    r.provider.inspect.return_value = r.observation.model_copy(
        update={
            "state": stale_state,
            "raw_state": "terminating" if stale_state == "pending" else stale_state,
            "nodes": () if stale_state == "pending" else r.observation.nodes,
            "scope_complete": stale_state != "pending",
        }
    )
    read = await run_next(r)
    assert read["id"] == inspect["id"]
    after = await r.repo.get_allocation(allocation_id)
    for field in ("observed_state", "provider_state", "observed_at", "release_committed_at"):
        assert after[field] == before[field], field
    r.provider.release.assert_awaited_once()
