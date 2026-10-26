"""A release keeps its terminal proof, and nothing read later can reopen it.

Real persistence, fences and dispatcher; only the provider and Host are fakes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.integration.storage.test_compute_workflow import (
    db as db,
    hosts as hosts,
    rig,
    run_next,
    store as store,
)

from cloud_agent.domain.compute import public_operation
from cloud_agent.ports.external.compute_provider import ProviderError, ProviderReceipt

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_KEPT = ("observed_state", "provider_state", "observed_at", "observation_error")


async def ready(store, hosts, monkeypatch):
    """An acquired, ready server whose scheduler sees no control-plane release."""
    monkeypatch.setattr(
        "cloud_agent.application.compute_reconcile.load_control_plane_host_release",
        lambda: None,
    )
    r = await rig(store, hosts, monkeypatch)
    assert (await run_next(r))["state"] == "succeeded"
    return r, r.op["allocation_id"]


def gone(r, *, status: int | None = 204, raw_state: str = "delete_acknowledged"):
    return r.observation.model_copy(
        update={
            "state": "terminated",
            "raw_state": raw_state,
            "nodes": (),
            "observed_at": datetime.now(UTC),
            "provider_http_status": status,
        }
    )


async def queue(r, kind: str, allocation_id: str) -> dict:
    request = f"{kind}-{uuid4()}"
    return await r.repo.create_operation(
        request_id=request,
        fingerprint=request,
        kind=kind,
        allocation_id=allocation_id,
        parameters={},
        requested_by="test",
    )


async def released(r, allocation_id: str, *, then: str | None = None) -> dict | None:
    """Retire the server on a DELETE acknowledgement; ``then`` queues a read behind it."""
    terminal = gone(r)
    r.provider.release.return_value = ProviderReceipt(
        outcome="confirmed", resource=terminal.ref, observation=terminal
    )
    retire = await queue(r, "retire", allocation_id)
    queued = await queue(r, then, allocation_id) if then else None
    done = await run_next(r)
    assert (done["id"], done["state"]) == (retire["id"], "succeeded"), done
    return queued


async def test_a_delete_receipt_survives_a_crash_and_its_retry_never_deletes_again(
    db, store, hosts, monkeypatch
):
    r, allocation_id = await ready(store, hosts, monkeypatch)
    terminal = gone(r)
    r.provider.release.return_value = ProviderReceipt(
        outcome="confirmed", resource=terminal.ref, observation=terminal
    )
    observe = r.repo.observe_allocation
    crashed = []

    async def crash_once_after_delete(**kwargs):
        if kwargs["observation"]["observed_state"] == "terminated" and not crashed:
            crashed.append(True)
            raise RuntimeError("the process stopped after the DELETE answer")
        return await observe(**kwargs)

    monkeypatch.setattr(r.repo, "observe_allocation", crash_once_after_delete)
    retire = await queue(r, "retire", allocation_id)
    interrupted = await run_next(r)
    assert (interrupted["id"], interrupted["state"]) == (retire["id"], "unknown"), interrupted
    receipt = interrupted["result"]["release_evidence"]
    assert receipt == {
        "source": "delete_acknowledged",
        "external_id": terminal.ref.external_id,
        "observed_at": terminal.observed_at.isoformat(),
        "http_status": 204,
        "operation_id": retire["id"],
    }
    assert (await r.repo.get_allocation(allocation_id))["observed_state"] == "ready"
    reads = r.provider.inspect.await_count

    await r.repo.retry_operation(retire["id"])
    retried = await run_next(r)

    assert (retried["id"], retried["state"]) == (retire["id"], "succeeded"), retried
    r.provider.release.assert_awaited_once()
    assert r.provider.inspect.await_count == reads
    assert retried["result"]["release_evidence"] == receipt
    allocation = await r.repo.get_allocation(allocation_id)
    assert (allocation["observed_state"], allocation["provider_state"]) == (
        "terminated",
        "delete_acknowledged",
    )
    shown = public_operation(retried, release_committed=True)["result"]["release_evidence"]
    assert shown == {key: value for key, value in receipt.items() if key != "operation_id"}


async def test_a_lost_worker_generation_cannot_write_its_held_read(db, store, hosts, monkeypatch):
    """A read held across its lease's loss, a reclaim and a release changes nothing."""
    r, allocation_id = await ready(store, hosts, monkeypatch)
    inspect = await queue(r, "inspect", allocation_id)
    started, release_read = asyncio.Event(), asyncio.Event()
    live = r.observation
    stale = live.model_copy(update={"state": "pending", "raw_state": "terminating"})
    held = []

    async def read(ref):
        if not held:
            held.append(ref)
            started.set()
            await release_read.wait()
            return stale
        return r.provider.inspect.return_value

    r.provider.inspect.side_effect = read
    r.provider.inspect.return_value = live
    claimed = await r.repo.claim_operation(worker_id="stale-worker")
    assert claimed["id"] == inspect["id"]
    stale_run = asyncio.create_task(r.worker.execute_claimed(claimed))
    await started.wait()
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET lease_expires_at=NOW()-INTERVAL '1 second'"
            " WHERE id=$1",
            inspect["id"],
        )
    reclaimed = await run_next(r)
    assert (reclaimed["id"], reclaimed["state"]) == (inspect["id"], "succeeded")
    assert reclaimed["generation"] == claimed["generation"] + 1
    await released(r, allocation_id)
    before = await r.repo.get_allocation(allocation_id)

    release_read.set()
    await stale_run

    after = await r.repo.get_allocation(allocation_id)
    assert {key: after[key] for key in _KEPT} == {key: before[key] for key in _KEPT}
    assert (await r.repo.get_operation(inspect["id"]))["state"] == "succeeded"


@pytest.mark.parametrize("late", ["read_failure", "identity_mismatch"])
async def test_a_late_failed_or_foreign_read_does_not_taint_a_terminal_row(
    db, store, hosts, monkeypatch, late
):
    r, allocation_id = await ready(store, hosts, monkeypatch)
    inspect = await released(r, allocation_id, then="inspect")
    before = await r.repo.get_allocation(allocation_id)
    if late == "read_failure":
        # A 404 inside the lease proves nothing, so it is not even an error here.
        r.provider.inspect.side_effect = ProviderError("resource_visibility_unknown", "not found")
    else:
        foreign = r.observation.ref.model_copy(update={"external_id": str(uuid4())})
        r.provider.inspect.return_value = r.observation.model_copy(update={"ref": foreign})

    read = await run_next(r)

    assert read["id"] == inspect["id"]
    assert read["state"] != "succeeded"
    after = await r.repo.get_allocation(allocation_id)
    assert {key: after[key] for key in _KEPT} == {key: before[key] for key in _KEPT}
    assert after["external_id"] == before["external_id"]


async def test_absence_without_identity_yields_only_to_an_observation_naming_one(
    db, store, hosts, monkeypatch
):
    """Never-created absence is a local record, not proof about any resource."""
    r = await rig(store, hosts, monkeypatch)
    allocation_id = r.op["allocation_id"]
    await r.repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "absent", "provider_state": "not_created"},
    )
    unnamed = await r.repo.observe_allocation(
        allocation_id=allocation_id, observation={"observed_state": "unknown"}
    )
    assert (unnamed["observed_state"], unnamed["provider_state"]) == ("absent", "not_created")
    named = str(uuid4())
    found = await r.repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"external_id": named, "observed_state": "pending"},
    )
    assert (found["external_id"], found["observed_state"]) == (named, "pending")


async def test_a_released_server_not_yet_seen_gone_is_still_read_and_only_read(
    db, store, hosts, monkeypatch
):
    r, allocation_id = await ready(store, hosts, monkeypatch)
    live = r.observation
    terminating = live.model_copy(
        update={
            "state": "pending",
            "raw_state": "terminating",
            "nodes": (),
            "scope_complete": False,
        }
    )

    async def accept_without_proof(ref, intent):
        r.provider.inspect.return_value = terminating
        return ProviderReceipt(outcome="pending", resource=ref)

    r.provider.release.side_effect = accept_without_proof
    retire = await queue(r, "retire", allocation_id)
    waiting = await run_next(r)
    assert (waiting["id"], waiting["state"], waiting["step"]) == (
        retire["id"],
        "waiting",
        "confirming_release",
    )
    assert "release_evidence" not in waiting["result"]
    for mutation in ("extend", "rotate", "upgrade", "retire"):
        with pytest.raises(ValueError, match=r"^release_committed$"):
            await queue(r, mutation, allocation_id)
    async with db.acquire() as conn:
        # Due for a read, and close enough to its lease end that a live server renews.
        await conn.execute(
            """UPDATE agent_compute_allocations
               SET observed_at=NOW()-INTERVAL '1 hour',expires_at=NOW()+INTERVAL '10 minutes',
                   expiration_known=TRUE,policy=$2::jsonb WHERE id=$1""",
            allocation_id,
            json.dumps({"enabled": True, "reason": "keep it"}),
        )
    snapshot = await r.repo.scheduling_inventory()
    assert retire["id"] in {op["id"] for op in snapshot.operations[allocation_id]}
    reconciler = r.worker.reconciler
    monkeypatch.setattr(r.commands, "extend", AsyncMock())

    await reconciler.schedule()
    await reconciler.schedule()

    operations = await r.repo.list_operations(allocation_id=allocation_id)
    assert sorted(op["kind"] for op in operations) == ["acquire", "inspect", "retire"]
    r.commands.extend.assert_not_awaited()

    # The provider ends it; the waiting release finishes on that read.
    terminal = gone(r, status=200, raw_state="terminated")
    r.provider.inspect.return_value = terminal
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET next_attempt_at=NOW() WHERE allocation_id=$1",
            allocation_id,
        )
    finished = await run_next(r)
    assert (finished["id"], finished["state"]) == (retire["id"], "succeeded"), (
        finished["step"],
        finished["message"],
        finished["result"],
    )
    assert finished["result"]["release_evidence"] == {
        "source": "provider_terminal",
        "external_id": terminal.ref.external_id,
        "observed_at": terminal.observed_at.isoformat(),
        "http_status": 200,
    }
    r.provider.release.assert_awaited_once()
    read = await run_next(r)
    assert (read["kind"], read["state"]) == ("inspect", "succeeded")
    snapshot = await r.repo.scheduling_inventory()
    assert allocation_id not in snapshot.operations
    await reconciler.schedule()
    assert len(await r.repo.list_operations(allocation_id=allocation_id)) == 3


async def test_an_absence_past_its_lease_is_recorded_as_that_fallback(
    db, store, hosts, monkeypatch
):
    r, allocation_id = await ready(store, hosts, monkeypatch)
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_allocations
               SET expires_at=NOW()-INTERVAL '1 hour',expiration_known=TRUE WHERE id=$1""",
            allocation_id,
        )
    r.provider.inspect.side_effect = ProviderError("resource_visibility_unknown", "not found")
    retire = await queue(r, "retire", allocation_id)

    done = await run_next(r)

    assert (done["id"], done["state"]) == (retire["id"], "succeeded"), done
    evidence = done["result"]["release_evidence"]
    assert (evidence["source"], evidence["external_id"]) == (
        "expired_not_found",
        r.observation.ref.external_id,
    )
    assert "http_status" not in evidence
    r.provider.release.assert_not_awaited()


async def test_a_delete_not_found_inside_the_lease_never_completes_a_release(
    db, store, hosts, monkeypatch
):
    r, allocation_id = await ready(store, hosts, monkeypatch)

    async def not_found(ref, intent):
        r.provider.inspect.side_effect = ProviderError("resource_visibility_unknown", "not found")
        return ProviderReceipt(outcome="unknown", resource=ref, diagnostics={"http_status": 404})

    r.provider.release.side_effect = not_found
    retire = await queue(r, "retire", allocation_id)

    done = await run_next(r)

    assert done["id"] == retire["id"]
    assert done["state"] != "succeeded"
    assert "release_evidence" not in done["result"]
    assert (await r.repo.get_allocation(allocation_id))["observed_state"] != "terminated"


async def test_a_server_whose_provider_resource_was_never_created_says_so(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    r.provider.acquire.side_effect = ProviderError("invalid_configuration", "refused")
    failed = await run_next(r)
    assert failed["state"] in {"failed", "blocked"}, failed
    allocation = await r.repo.get_allocation(r.op["allocation_id"])
    assert allocation["external_id"] is None
    retire = await queue(r, "retire", allocation["id"])

    done = await run_next(r)

    assert (done["id"], done["state"]) == (retire["id"], "succeeded"), done
    assert done["result"]["release_evidence"] == {"source": "never_created"}
    assert public_operation(done)["result"]["release_evidence"] == {"source": "never_created"}
    r.provider.inspect.assert_not_awaited()
    r.provider.release.assert_not_awaited()
