"""Recovery semantics across real reconciler steps and worker lease loss.

External ports are controlled so tests can stop exactly between a provider
mutation, its durable receipt, and the next observation. Assertions are on
observable requests and admission outcomes, not implementation-only counters.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud_agent.application import compute_reconcile
from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.ports.external.compute_provider import (
    AcquisitionResolution,
    NodeObservation,
    ProviderError,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
)
from cloud_agent.ports.external.host_bootstrap import BootstrapResult, DeploymentProfile
from cloud_agent.schemas_compute import RenewalPolicy
from cloud_agent.workers.compute_resources import ComputeResourceWorker

NOW = datetime.now(UTC)
TARGET = NOW + timedelta(hours=8)
REF = ResourceRef(external_id="experiment-immutable-id", config={"project": "research"})
NODE = NodeObservation(
    provider_node_id="node-1", instance_identity="boot-identity", display_name="Machine A"
)
PROFILE = DeploymentProfile(
    id="kata",
    label="Kata",
    source_bundle="/operator/release.tar",
    source_sha256="a" * 64,
    deployment_sha="b" * 40,
    environment_ref="deployment:host",
)


def observed(*, state="ready", expires=NOW + timedelta(hours=4)):
    return ResourceObservation(
        ref=REF,
        state=state,
        raw_state=state,
        observed_at=NOW,
        expires_at=expires,
        lease_known=True,
        nodes=(NODE,),
        scope_complete=True,
    )


def harness(monkeypatch, *, kind="acquire", identified=False):
    operation = {
        "id": "operation-a",
        "allocation_id": "allocation-a",
        "kind": kind,
        "request_id": "request-a",
        "fingerprint": "f" * 64,
        "state": "running",
        "step": "pending",
        "created_at": NOW,
        "worker_id": "worker-a",
        "generation": 1,
        "result": {},
        "parameters": {"target_expires_at": TARGET.isoformat(), "reason": "Finish evaluation"},
        "requested_by": "admin:a",
    }
    allocation = {
        "id": "allocation-a",
        "provider_id": "cloudlab",
        "mode": "provision",
        "external_id": REF.external_id if identified else None,
        "configuration": REF.config,
        "management_mode": "manage",
        "deployment_profile": PROFILE.model_dump(mode="json"),
        "desired_state": "present",
        "scope_complete": True,
        "observed_at": NOW,
        "expires_at": NOW + timedelta(hours=4),
        "expiration_known": True,
        "revision": 1,
        "policy": RenewalPolicy().model_dump(),
        "nodes": [
            {
                "id": "node-id",
                "name": NODE.display_name,
                "provider_node_id": NODE.provider_node_id,
                "instance_identity": NODE.instance_identity,
            }
        ],
        "hosts": [
            {
                "host": "compute-nodeid",
                "status": "online",
                "enrolled": True,
                "last_heartbeat_at": NOW,
            }
        ],
    }

    async def save_observation(**kwargs):
        observation = kwargs["observation"]
        allocation.update({key: value for key, value in observation.items() if key != "nodes"})
        # Persistence keeps durable node IDs when atomically merging provider
        # facts; the provider descriptor itself never chooses those IDs.
        nodes = {node["provider_node_id"]: node for node in allocation["nodes"]}
        if not allocation.get("release_committed_at") and observation.get("observed_state") not in {
            "terminated",
            "absent",
        }:
            for fact in observation.get("nodes", []):
                existing = nodes.get(fact["provider_node_id"], {})
                nodes[fact["provider_node_id"]] = {
                    "id": existing.get("id", "fixture-" + fact["provider_node_id"]),
                    **existing,
                    **fact,
                }
        allocation["nodes"] = list(nodes.values())
        allocation["observed_at"] = NOW
        return allocation

    async def save_finish(**kwargs):
        operation.update(state=kwargs["state"], step=kwargs["step"])
        operation["result"].update(kwargs.get("result") or {})
        return True

    repository = SimpleNamespace(
        get_allocation=AsyncMock(side_effect=lambda _id: allocation),
        list_allocations=AsyncMock(return_value=[allocation]),
        list_operations=AsyncMock(return_value=[]),
        observe_allocation=AsyncMock(side_effect=save_observation),
        upsert_node=AsyncMock(return_value=allocation["nodes"][0]),
        checkpoint_operation=AsyncMock(return_value=True),
        finish_operation=AsyncMock(side_effect=save_finish),
        record_observation_error=AsyncMock(),
        set_admission_hold=AsyncMock(),
        prepare_bootstrap_binding=AsyncMock(),
        clear_bootstrap_hold=AsyncMock(return_value=True),
        renew_operation_lease=AsyncMock(return_value=True),
        begin_retirement=AsyncMock(),
        retirement_impact=AsyncMock(return_value={"blocked_reasons": []}),
        commit_release=AsyncMock(return_value={"blocked_reasons": []}),
        record_inventory=AsyncMock(return_value=True),
    )
    provider = SimpleNamespace(
        acquire=AsyncMock(
            return_value=ProviderReceipt(outcome="confirmed", resource=REF, observation=observed())
        ),
        inspect=AsyncMock(return_value=observed()),
        recover_acquire=AsyncMock(return_value=AcquisitionResolution(outcome="unknown")),
        extend=AsyncMock(
            return_value=ProviderReceipt(
                outcome="confirmed", resource=REF, observation=observed(expires=TARGET)
            )
        ),
        release=AsyncMock(
            return_value=ProviderReceipt(
                outcome="confirmed", resource=REF, observation=observed(state="terminated")
            )
        ),
    )
    commands = SimpleNamespace(
        repository=repository,
        registry=SimpleNamespace(get=Mock(return_value=SimpleNamespace(provider=provider))),
        _require_managed=Mock(),
        queue_inspection=AsyncMock(),
        extend=AsyncMock(),
        configuration=SimpleNamespace(poll_seconds=30, observation_seconds=120),
    )
    bootstrap = SimpleNamespace(
        deploy=AsyncMock(
            return_value=BootstrapResult(
                ready=True, host="compute-nodeid", deployment_sha=PROFILE.deployment_sha
            )
        ),
        readiness=AsyncMock(
            return_value=BootstrapResult(
                ready=True, host="compute-nodeid", deployment_sha=PROFILE.deployment_sha
            )
        ),
        inventory=AsyncMock(),
    )
    host_pool = SimpleNamespace(
        get_host=AsyncMock(return_value=allocation["hosts"][0]), issue_enroll_token=AsyncMock()
    )
    broker = SimpleNamespace(health=AsyncMock(), list_sandboxes=AsyncMock(return_value=[]))
    monkeypatch.setattr(compute_reconcile, "broker_for_host", lambda _host: broker)
    reconciler = ComputeReconciler(commands=commands, host_pool=host_pool, bootstrap=bootstrap)
    return SimpleNamespace(
        operation=operation,
        allocation=allocation,
        repository=repository,
        provider=provider,
        commands=commands,
        bootstrap=bootstrap,
        host_pool=host_pool,
        broker=broker,
        reconciler=reconciler,
    )


@pytest.mark.asyncio
async def test_ready_host_uses_public_enrolled_fact_and_opens_only_its_bootstrap_hold(monkeypatch):
    h = harness(monkeypatch)
    assert "credential_hash" not in h.allocation["hosts"][0]
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "ready"
    h.broker.health.assert_awaited_once()
    h.host_pool.issue_enroll_token.assert_not_awaited()
    h.repository.clear_bootstrap_hold.assert_awaited_once_with(
        node_id="node-id",
        host="compute-nodeid",
        operation_id="operation-a",
        generation=1,
        worker_id="worker-a",
    )
    assert all(
        call.kwargs["purpose"] == "lease_deadline"
        for call in h.repository.set_admission_hold.await_args_list
    )


@pytest.mark.asyncio
async def test_identified_acquisition_receipt_survives_failed_followup_get(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.return_value = ProviderReceipt(outcome="pending", resource=REF)
    h.provider.inspect.side_effect = [
        ProviderError("result_unknown", "Observation interrupted"),
        observed(),
    ]
    await h.reconciler.execute(h.operation)
    assert h.operation["result"]["resource_ref"]["external_id"] == REF.external_id
    assert h.operation["state"] == "waiting"
    assert h.allocation["external_id"] is None  # GET failed before the inventory could be saved.
    await h.reconciler.execute(h.operation)
    h.provider.acquire.assert_awaited_once()
    h.provider.recover_acquire.assert_not_awaited()
    assert h.operation["state"] == "succeeded"
    assert all(
        call.args[0].external_id == REF.external_id for call in h.provider.inspect.await_args_list
    )


@pytest.mark.asyncio
async def test_unknown_acquisition_is_never_blindly_repeated(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.side_effect = ProviderError("result_unknown", "Submission response was lost")
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "unknown"
    await h.reconciler.execute(h.operation)
    h.provider.acquire.assert_awaited_once()
    h.provider.recover_acquire.assert_awaited_once()
    assert h.operation["state"] == "unknown"
    h.bootstrap.deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_deployment_timeout_retries_same_resource_and_binding(monkeypatch):
    h = harness(monkeypatch)
    h.bootstrap.deploy.side_effect = [
        ProviderError("result_unknown", "SSH result interrupted"),
        BootstrapResult(ready=True, host="compute-nodeid", deployment_sha=PROFILE.deployment_sha),
    ]
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    assert h.operation["step"] == "deploying"
    await h.reconciler.execute(h.operation)
    h.provider.acquire.assert_awaited_once()
    assert h.operation["state"] == "succeeded"
    first, second = h.bootstrap.deploy.await_args_list
    assert first.args[2] == second.args[2]  # one deployment/Host identity, not another server


@pytest.mark.asyncio
async def test_definite_extension_rejection_allows_repair_with_same_absolute_target(monkeypatch):
    h = harness(monkeypatch, kind="extend", identified=True)
    h.provider.extend.side_effect = [
        ProviderError("credential_rejected", "Credential needs replacement"),
        ProviderReceipt(outcome="confirmed", resource=REF, observation=observed(expires=TARGET)),
    ]
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["extension_requested"] is False
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert len(h.provider.extend.await_args_list) == 2
    assert all(
        call.args[1].target_expires_at == TARGET for call in h.provider.extend.await_args_list
    )
    assert all(
        call.args[0].external_id == REF.external_id for call in h.provider.extend.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_receipt", [False, True])
async def test_unknown_or_pending_extension_is_only_queried_until_confirmed(
    monkeypatch, pending_receipt
):
    h = harness(monkeypatch, kind="extend", identified=True)
    if pending_receipt:
        h.provider.extend.return_value = ProviderReceipt(
            outcome="pending", resource=REF, observation=observed()
        )
    else:
        h.provider.extend.side_effect = ProviderError(
            "result_unknown", "Extension response was lost"
        )
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["extension_requested"] is True
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    h.provider.extend.assert_awaited_once()
    h.provider.inspect.return_value = observed(expires=TARGET)
    await h.reconciler.execute(h.operation)
    h.provider.extend.assert_awaited_once()
    assert h.operation["state"] == "succeeded"


@pytest.mark.asyncio
async def test_partial_extension_is_not_reported_as_success(monkeypatch):
    h = harness(monkeypatch, kind="extend", identified=True)
    h.provider.extend.return_value = ProviderReceipt(
        outcome="confirmed", resource=REF, observation=observed()
    )
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert h.operation["step"] == "partial_extension"
    h.provider.extend.assert_awaited_once()


@pytest.mark.asyncio
async def test_committed_release_queries_before_replaying_same_immutable_resource(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="retiring")
    h.operation["result"]["release_requested"] = True
    events = []

    async def inspect(ref):
        events.append(("inspect", ref.external_id))
        return observed()

    async def release(ref, _intent):
        events.append(("release", ref.external_id))
        return ProviderReceipt(
            outcome="confirmed", resource=ref, observation=observed(state="terminated")
        )

    h.provider.inspect.side_effect = inspect
    h.provider.release.side_effect = release
    await h.reconciler.execute(h.operation)
    assert events == [("inspect", REF.external_id), ("release", REF.external_id)]
    assert h.operation["state"] == "succeeded"
    h.repository.begin_retirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_release_waits_if_provider_already_processing_it(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="retiring")
    h.operation["result"]["release_requested"] = True
    h.provider.inspect.return_value = observed(state="pending")
    await h.reconciler.execute(h.operation)
    h.provider.release.assert_not_awaited()
    assert h.operation["state"] == "waiting"


@pytest.mark.asyncio
async def test_committed_release_polls_terminating_without_manifest_until_terminal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="absent")
    h.operation["result"]["release_requested"] = True
    original_nodes = deepcopy(h.allocation["nodes"])
    # CloudLab loses a complete manifest while the experiment is terminating.
    h.provider.inspect.side_effect = [
        observed(state="pending").model_copy(update={"scope_complete": False, "nodes": ()}),
        observed(state="terminated").model_copy(update={"scope_complete": False, "nodes": ()}),
    ]
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["external_pending"]
    assert h.allocation["nodes"] == original_nodes
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert not h.operation["result"]["external_pending"]
    h.provider.release.assert_not_awaited()
    h.repository.begin_retirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_retirement_waiting_does_not_stop_previously_enabled_renewal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(
        desired_state="retiring",
        expires_at=NOW + timedelta(minutes=10),
        policy=RenewalPolicy(enabled=True, reason="Preserve retained sessions").model_dump(),
    )
    h.repository.list_operations.return_value = [
        {**h.operation, "state": "waiting", "step": "waiting_for_retention"}
    ]
    await h.reconciler.schedule()
    h.commands.extend.assert_awaited_once()
    args = h.commands.extend.await_args
    assert args.args[0] == h.allocation["id"]
    assert args.args[1].reason == "Preserve retained sessions"
    assert args.kwargs["actor"] == "compute-renewal"


@pytest.mark.asyncio
async def test_worker_journals_unexpected_error_without_remote_output_or_propagation(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.side_effect = RuntimeError("credential-canary in private remote output")
    worker = ComputeResourceWorker(h.reconciler)
    await worker.execute_claimed(h.operation)
    assert h.operation["state"] == "unknown"
    finish = h.repository.finish_operation.await_args.kwargs
    assert finish["step"] == "acquiring"
    assert "credential-canary" not in finish["message"]
    assert finish["generation"] == 1
    assert finish["worker_id"] == "worker-a"


@pytest.mark.asyncio
async def test_worker_cancels_local_external_work_when_lease_is_lost(monkeypatch):
    h = harness(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_acquire(*_args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def immediate_heartbeat(_seconds):
        await started.wait()

    h.provider.acquire.side_effect = blocked_acquire
    h.repository.renew_operation_lease.return_value = False
    # Patch the worker's module binding, not the process-wide asyncio sleep.
    from cloud_agent.workers import compute_resources as worker_module

    monkeypatch.setattr(
        worker_module,
        "asyncio",
        SimpleNamespace(
            **{
                name: getattr(asyncio, name)
                for name in ("create_task", "current_task", "CancelledError", "gather")
            },
            sleep=immediate_heartbeat,
        ),
    )
    await asyncio.wait_for(
        ComputeResourceWorker(h.reconciler).execute_claimed(h.operation), timeout=1
    )
    assert cancelled.is_set()
    h.repository.finish_operation.assert_not_awaited()
    h.bootstrap.deploy.assert_not_awaited()
    h.repository.renew_operation_lease.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_checkpoint_prevents_starting_an_external_acquisition(monkeypatch):
    h = harness(monkeypatch)
    h.repository.checkpoint_operation.return_value = False
    before = deepcopy(h.operation)
    await h.reconciler.execute(h.operation)
    h.provider.acquire.assert_not_awaited()
    h.repository.finish_operation.assert_not_awaited()
    assert h.operation == before


@pytest.mark.asyncio
async def test_pending_extension_with_increased_but_short_deadline_requires_review(monkeypatch):
    h = harness(monkeypatch, kind="extend", identified=True)
    shorter = TARGET - timedelta(hours=1)
    h.provider.extend.return_value = ProviderReceipt(
        outcome="pending", resource=REF, observation=observed(expires=shorter)
    )
    h.provider.inspect.side_effect = [observed(), observed(expires=shorter)]
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert h.operation["step"] == "partial_extension"
    assert h.operation["result"]["actual_expires_at"] == shorter.isoformat()
    h.provider.extend.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasons", "expected_code"),
    [
        (("kvm", "secret-looking-remote-output"), "kvm"),
        (("secret-looking-remote-output",), "bootstrap_not_ready"),
    ],
)
async def test_bootstrap_refusal_reports_only_public_diagnostics(
    monkeypatch, reasons, expected_code
):
    """A failed deployment remains blocked and never renders arbitrary remote output."""
    h = harness(monkeypatch, identified=True)
    h.bootstrap.deploy.return_value = BootstrapResult(
        ready=False,
        host="compute-nodeid",
        deployment_sha=PROFILE.deployment_sha,
        blocked_reasons=reasons,
    )
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert [item["code"] for item in h.operation["result"]["blocked_reasons"]] == [expected_code]
    assert "secret-looking-remote-output" not in str(h.operation["result"])
    h.bootstrap.readiness.assert_not_awaited()
    h.repository.clear_bootstrap_hold.assert_not_awaited()
