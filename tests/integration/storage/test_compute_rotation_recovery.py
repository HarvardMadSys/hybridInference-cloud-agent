"""Crash and retry the Host move at real database and remote-effect boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.storage.test_compute_rotation_workflow import (
    ready_slot,
    rig,
    rotation_of,
    run_all,
    successor_of,
)
from tests.integration.storage.test_host_pool_store import db as db, hosts as hosts, store as store

from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.schemas_compute import RotateComputeRequest
from cloud_agent.workers.compute_resources import ComputeResourceWorker

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def queue_move(r, source):
    return await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )


async def restart_after_crash(r, db):
    """Lose one process, expire its lease, then recover in a fresh reconciler."""
    for _ in range(40):
        operation = await r.repo.claim_operation(worker_id="before-restart")
        if operation is None:
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_compute_operations SET next_attempt_at=NOW() WHERE state='waiting'"
                )
            continue
        try:
            await r.worker.reconciler.execute(operation)
        except asyncio.CancelledError:
            break
    else:
        raise AssertionError("the injected crash was never reached")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET lease_expires_at=NOW()-interval '1 second' "
            "WHERE state='running'"
        )
    r.worker = ComputeResourceWorker(
        ComputeReconciler(commands=r.commands, host_pool=r.hosts, bootstrap=r.nodes)
    )
    await run_all(r, db)


async def test_recover_candidate_created_before_its_acknowledgement(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    original = r.repo.create_rotation_candidate
    crashed = False

    async def create(**kwargs):
        nonlocal crashed
        result = await original(**kwargs)
        if not crashed:
            crashed = True
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(r.repo, "create_rotation_candidate", create)
    await queue_move(r, source)
    await restart_after_crash(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    assert len(await successor_of(db, r.allocation_id)) == 1
    assert r.cloud.created == ["first", "t1"]
    assert (await hosts.get_host(host=host))["status"] == "online"


async def test_partial_stop_is_restarted_before_discarding_the_candidate(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    original = r.nodes.stop
    failed = False

    async def stop(node, profile, intent):
        nonlocal failed
        await original(node, profile, intent)
        if node.display_name == "first-node" and not failed:
            failed = True
            raise ProviderError("host_stop_failed", "The stop lost its connection", retryable=True)

    monkeypatch.setattr(r.nodes, "stop", stop)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    calls = r.nodes.calls
    first_stop = calls.index(("stop", "first-node"))
    assert calls[first_stop + 1] == ("deploy", "first-node")


async def test_restart_during_rollback_retries_starting_the_original_node(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    r.nodes.fail_final_copy = True
    original = r.nodes.deploy
    crashed = False

    async def deploy(node, *args, **kwargs):
        nonlocal crashed
        if node.display_name == "first-node" and not crashed:
            crashed = True
            raise asyncio.CancelledError
        return await original(node, *args, **kwargs)

    monkeypatch.setattr(r.nodes, "deploy", deploy)
    await queue_move(r, source)
    await restart_after_crash(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    assert r.nodes.calls.count(("deploy", "first-node")) == 2


@pytest.mark.parametrize("code", ["ssh_connection_failed", "result_unknown"])
async def test_a_lost_connection_restarting_the_original_node_is_retried(
    db, store, hosts, monkeypatch, code
):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    r.nodes.fail_final_copy = True
    original = r.nodes.deploy
    failed = False

    async def deploy(node, *args, **kwargs):
        nonlocal failed
        if node.display_name == "first-node" and not failed:
            failed = True
            raise ProviderError(code, "The SSH session broke")
        return await original(node, *args, **kwargs)

    monkeypatch.setattr(r.nodes, "deploy", deploy)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert failed
    assert rotation["state"] == "succeeded", rotation
    # The first deployment, then the restart that succeeded on its retry.
    assert r.nodes.calls.count(("deploy", "first-node")) == 2


@pytest.mark.parametrize("code", ["remote_step_failed", "ssh_connection_failed"])
async def test_an_original_node_that_cannot_restart_alerts_and_keeps_claims_paused(
    db, store, hosts, monkeypatch, code
):
    """With the Host down on both nodes, the move stops for an administrator, urgently."""
    from cloud_agent.application import compute_rotation

    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    r.nodes.fail_final_copy = True
    alerts = []

    async def deliver(event, details):
        alerts.append((event, details["reason"]))

    monkeypatch.setattr(compute_rotation, "deliver_event", deliver)
    real_now = compute_rotation._now
    attempts = 0

    async def deploy(node, *args, **kwargs):
        nonlocal attempts
        if node.display_name != "first-node":
            raise AssertionError("only the original node is started in this test")
        attempts += 1
        if attempts == 2:
            # A connection that stays down outlasts the retry window.
            monkeypatch.setattr(
                compute_rotation, "_now", lambda: real_now() + timedelta(minutes=11)
            )
        raise ProviderError(code, "SSH verification or remote deployment step failed")

    monkeypatch.setattr(r.nodes, "deploy", deploy)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "blocked", rotation
    assert attempts == (1 if code == "remote_step_failed" else 2)
    assert rotation["result"]["blocked_reasons"][0]["code"] == "rotation_rollback_failed"
    assert ("compute_rotation_failed", "rotation_rollback_failed") in alerts
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT 1 FROM agent_host_admission_holds WHERE host=$1 AND purpose='rotation'", host
        )
    # A retry starts a fresh window and resumes the rollback where it stopped.
    monkeypatch.setattr(compute_rotation, "_now", real_now)
    monkeypatch.setattr(r.nodes, "deploy", type(r.nodes).deploy.__get__(r.nodes))
    await r.repo.retry_operation(rotation["id"])
    await run_all(r, db)
    assert (await rotation_of(r, r.allocation_id))["state"] == "succeeded"


async def test_an_unexpected_failure_before_the_stop_resumes_claims(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET expires_at=NOW()+interval '90 minutes' WHERE id=$1",
            r.allocation_id,
        )

    def refuse(mode, config):
        raise ProviderError("invalid_configuration", "The replacement request is invalid")

    monkeypatch.setattr(r.cloud, "validate_config", refuse)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "blocked", rotation
    assert rotation["result"]["blocked_reasons"][0]["code"] == "invalid_configuration"
    assert rotation["result"]["hold_set"] is False
    async with db.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT 1 FROM agent_host_admission_holds WHERE host=$1 AND purpose='rotation'", host
        )
    assert ("stop", "first-node") not in r.nodes.calls


async def test_an_unexpected_failure_after_takeover_only_retries_cleanup(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    original = r.repo.set_rotation_hold
    failed = False

    async def set_hold(**kwargs):
        nonlocal failed
        if kwargs["scope"] is None and kwargs["allocation_id"] != r.allocation_id and not failed:
            failed = True
            raise ValueError("host_not_found")
        return await original(**kwargs)

    monkeypatch.setattr(r.repo, "set_rotation_hold", set_hold)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert failed
    assert rotation["state"] == "succeeded", rotation
    # The verified replacement kept the Host: nothing was rolled back.
    assert r.nodes.calls.count(("deploy", "first-node")) == 1
    assert r.cloud.released == [source["external_id"]]


async def test_resuming_claims_is_durable_before_new_work_can_start(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    original = r.repo.set_rotation_hold
    crashed = False

    async def set_hold(**kwargs):
        nonlocal crashed
        await original(**kwargs)
        if kwargs["scope"] is None and kwargs["allocation_id"] != r.allocation_id and not crashed:
            rotation = await r.repo.get_operation(kwargs["operation_id"])
            # From here the new node can write state. Recovery must never copy
            # the old state again or roll back to it, even if readiness later fails.
            assert rotation["result"].get("verified") is True
            crashed = True
            raise asyncio.CancelledError

    monkeypatch.setattr(r.repo, "set_rotation_hold", set_hold)
    await queue_move(r, source)
    await restart_after_crash(r, db)
    assert (await rotation_of(r, r.allocation_id))["state"] == "succeeded"
    assert r.nodes.calls.count(("transfer-final", "first-node->t1-node")) == 1


async def test_discard_can_retry_after_committing_release(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    r.nodes.fail_final_copy = True
    original = r.cloud.release
    failed = False

    async def release(*args):
        nonlocal failed
        if not failed:
            failed = True
            raise ProviderError("provider_unavailable", "Temporary outage", retryable=True)
        return await original(*args)

    monkeypatch.setattr(r.cloud, "release", release)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    failed_node, _ = await successor_of(db, r.allocation_id)
    assert failed_node["external_id"] in r.cloud.released


@pytest.mark.parametrize(
    "effect", ["stop", "transfer", "rebind_slot_host", "deploy", "revoke_transfer", "wipe"]
)
async def test_restart_after_remote_effect_keeps_one_host_and_one_copy(
    db, store, hosts, monkeypatch, effect
):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    owner = r.repo if effect == "rebind_slot_host" else r.nodes
    original = getattr(owner, effect)
    crashed = False

    async def perform(*args, **kwargs):
        nonlocal crashed
        result = await original(*args, **kwargs)
        boundary = effect != "transfer" or kwargs.get("final")
        if boundary and not crashed:
            crashed = True
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(owner, effect, perform)
    await queue_move(r, source)
    await restart_after_crash(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    [replacement] = await successor_of(db, r.allocation_id)
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [host]
    assert r.cloud.released == [source["external_id"]]


async def test_retirement_request_is_recovered_after_revision_changes(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    original = r.commands.retire
    crashed = False

    async def retire(*args, **kwargs):
        nonlocal crashed
        result = await original(*args, **kwargs)
        if not crashed:
            crashed = True
            # An allocation may be edited while the worker is down. Its saved
            # retirement request must still be recovered with its original body.
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_compute_allocations SET revision=revision+1 WHERE id=$1",
                    r.allocation_id,
                )
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(r.commands, "retire", retire)
    await queue_move(r, source)
    await restart_after_crash(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    retirements = await r.repo.list_operations(request_id=f"retire:{rotation['id']}")
    assert len(retirements) == 1
    assert retirements[0]["state"] == "succeeded"
    assert r.cloud.released == [source["external_id"]]


@pytest.mark.parametrize("effect", ["revoke_transfer", "wipe"])
async def test_cleanup_failure_retries_without_rolling_back(db, store, hosts, monkeypatch, effect):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    original = getattr(r.nodes, effect)
    attempts = 0

    async def perform(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("ssh_connection_failed", "Temporary outage", retryable=True)
        return await original(*args)

    monkeypatch.setattr(r.nodes, effect, perform)
    await queue_move(r, source)
    await run_all(r, db)
    assert attempts == 2
    assert (await rotation_of(r, r.allocation_id))["state"] == "succeeded"
    assert r.nodes.calls.count(("deploy", "first-node")) == 1
    assert r.cloud.released == [source["external_id"]]


async def test_rollback_does_not_start_the_old_copy_until_the_target_is_stopped(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    r.nodes.fail_start_on = {"t1-node"}
    original = r.nodes.stop

    async def stop(node, *args):
        if node.display_name == "t1-node":
            raise ProviderError("ssh_connection_failed", "Cannot confirm target stopped")
        return await original(node, *args)

    monkeypatch.setattr(r.nodes, "stop", stop)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "blocked", rotation
    assert r.nodes.calls.count(("deploy-reenroll", "first-node")) == 0
    assert not r.cloud.released
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT 1 FROM agent_host_admission_holds WHERE host=$1 AND purpose='rotation'", host
        )


async def test_deadline_pauses_claims_before_the_replacement_is_ready(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET expires_at=NOW()+interval '90 minutes' WHERE id=$1",
            r.allocation_id,
        )
    await queue_move(r, source)
    operation = await r.repo.claim_operation(worker_id="deadline-test")
    await r.worker.reconciler.execute(operation)
    assert not any(call[0] == "stage" for call in r.nodes.calls)
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT scope FROM agent_host_admission_holds WHERE host=$1 AND purpose='rotation'",
                host,
            )
            == "all_claims"
        )


async def test_even_an_urgent_move_waits_for_running_turns(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    expires = datetime.now(UTC) + timedelta(minutes=15)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET expires_at=$2 WHERE id=$1",
            r.allocation_id,
            expires,
        )
    r.cloud.experiments[source["external_id"]] = r.cloud.experiments[
        source["external_id"]
    ].model_copy(update={"expires_at": expires})
    checks = 0

    async def running(*, host):
        nonlocal checks
        checks += 1
        return 1 if checks < 3 else 0

    original = r.nodes.stop

    async def stop(*args):
        assert checks >= 3, "migration stopped an active native turn"
        return await original(*args)

    monkeypatch.setattr(r.repo, "running_attempts", running)
    monkeypatch.setattr(r.nodes, "stop", stop)
    await queue_move(r, source)
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    assert "compute_rotation_drain_overdue" in rotation["result"]["alerts"]
