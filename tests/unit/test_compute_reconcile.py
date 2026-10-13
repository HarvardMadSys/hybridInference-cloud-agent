"""Recovery semantics across real reconciler steps and worker lease loss.

External ports are controlled so tests can stop exactly between a provider
mutation, its durable receipt, and the next observation. Assertions are on
observable requests and admission outcomes, not implementation-only counters.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud_agent.application import compute_reconcile
from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.application.compute_setup import HostRelease
from cloud_agent.domain.compute import autoscaling_acquire_can_cancel, fingerprint
from cloud_agent.host_pool import hash_secret
from cloud_agent.ports.external.compute_provider import (
    AcquisitionResolution,
    NodeObservation,
    ProviderCapabilities,
    ProviderError,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
    SshConnection,
)
from cloud_agent.ports.external.host_bootstrap import (
    BootstrapResult,
    DeploymentProfile,
    PreflightResult,
)
from cloud_agent.schemas_compute import RenewalPolicy
from cloud_agent.workers.compute_resources import ComputeResourceWorker
from cloud_agent_shared.shared_protocol import shared_deployment_capability

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
    # Fixture timestamps and readiness checks share a clock even when a full
    # suite reaches these tests long after module collection.
    monkeypatch.setattr(compute_reconcile, "_now", lambda: NOW)
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
        assert_autoscale_acquire_connections_verified=AsyncMock(),
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
        capabilities=Mock(
            return_value=ProviderCapabilities(
                acquisition_modes=("attach", "provision"), can_extend=True, can_release=True
            )
        ),
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
    sandbox_manager = SimpleNamespace(health=AsyncMock(), list_sandboxes=AsyncMock(return_value=[]))
    monkeypatch.setattr(
        compute_reconcile, "sandbox_manager_for_host", lambda _host: sandbox_manager
    )
    reconciler = ComputeReconciler(commands=commands, host_pool=host_pool, bootstrap=bootstrap)
    return SimpleNamespace(
        operation=operation,
        allocation=allocation,
        repository=repository,
        provider=provider,
        commands=commands,
        bootstrap=bootstrap,
        host_pool=host_pool,
        sandbox_manager=sandbox_manager,
        reconciler=reconciler,
    )


@pytest.mark.asyncio
async def test_ready_host_uses_public_enrolled_fact_and_opens_only_its_bootstrap_hold(monkeypatch):
    h = harness(monkeypatch)
    assert "credential_hash" not in h.allocation["hosts"][0]
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "ready"
    h.sandbox_manager.health.assert_awaited_once()
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


@pytest.mark.parametrize("blocked", [None, "data_mount_unavailable", "data_mount_conflict"])
async def test_attach_preflight_reports_selected_storage_evidence(monkeypatch, blocked):
    h = harness(monkeypatch, kind="preflight")
    h.operation["parameters"] = {
        "provider_id": "cloudlab",
        "mode": "attach",
        "validated_configuration": REF.config,
        "profile": PROFILE.model_copy(update={"sandbox_storage_path": "/mydata"}).model_dump(
            mode="json"
        ),
    }
    h.bootstrap.preflight = AsyncMock(
        return_value=PreflightResult(
            ready=blocked is None,
            machine_identity="machine",
            operating_system="ubuntu:24.04",
            architecture="x86_64",
            checks={
                "data_mount_available": blocked != "data_mount_unavailable",
                "data_mount_conflict_free": blocked is None,
                "private_remote_detail": True,
            },
            blocked_reasons=() if blocked is None else (blocked,),
        )
    )
    await h.reconciler.execute(h.operation)
    if blocked:
        assert h.operation["state"] == "blocked"
        reasons = h.operation["result"]["blocked_reasons"]
        assert [reason["code"] for reason in reasons] == [blocked]
        assert "Sandbox storage" in reasons[0]["message"]
    else:
        assert h.operation["state"] == "succeeded"
        assert h.operation["result"]["checks"] == {
            "data_mount_available": True,
            "data_mount_conflict_free": True,
        }
    h.bootstrap.deploy.assert_not_awaited()
    h.host_pool.issue_enroll_token.assert_not_awaited()
    h.repository.clear_bootstrap_hold.assert_not_awaited()


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
async def test_rotated_autoscale_connection_blocks_before_provider_acquire(monkeypatch):
    h = harness(monkeypatch)
    h.allocation.update(
        autoscaling_owned=True,
        nodes=[],
        hosts=[],
        scope_complete=False,
    )
    h.repository.assert_autoscale_acquire_connections_verified.side_effect = ValueError(
        "autoscaling_connection_unverified"
    )

    await h.reconciler.execute(h.operation)

    h.provider.acquire.assert_not_awaited()
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["external_pending"] is False
    assert autoscaling_acquire_can_cancel(h.operation, h.allocation)


@pytest.mark.asyncio
async def test_registration_refusal_is_checkpointed_as_cancellable_before_provider(monkeypatch):
    h = registering(monkeypatch)
    h.allocation.update(
        autoscaling_owned=True,
        nodes=[],
        hosts=[],
        scope_complete=False,
    )
    h.repository.create_node_registration.side_effect = ProviderError(
        "registration_unavailable", "Registration is unavailable"
    )

    await h.reconciler.execute(h.operation)

    h.provider.acquire.assert_not_awaited()
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["external_pending"] is False
    assert autoscaling_acquire_can_cancel(h.operation, h.allocation)


@pytest.mark.asyncio
async def test_retryable_capacity_rejection_remains_cancellable(monkeypatch):
    h = harness(monkeypatch)
    h.allocation.update(
        autoscaling_owned=True,
        nodes=[],
        hosts=[],
        scope_complete=False,
    )
    h.provider.acquire.side_effect = ProviderError(
        "capacity_unavailable",
        "CloudLab has no matching capacity",
        retryable=True,
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["external_pending"] is False
    assert autoscaling_acquire_can_cancel(h.operation, h.allocation)
    h.provider.acquire.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_acquisition_finishes_failed_for_profile_recycling(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.return_value = ProviderReceipt(
        outcome="confirmed", resource=REF, observation=observed(state="terminated")
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "failed"
    assert h.allocation["observed_state"] == "terminated"
    assert h.repository.finish_operation.await_args.kwargs["state"] == "failed"
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
async def test_release_of_a_server_the_provider_never_created_finishes_locally(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=False)
    h.allocation.update(
        external_id=None,
        nodes=[],
        hosts=[],
        scope_complete=False,
        observed_at=None,
        observed_state="unknown",
        desired_state="present",
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    assert h.allocation["observed_state"] == "absent"
    assert h.allocation["provider_state"] == "not_created"
    h.provider.release.assert_not_awaited()
    h.provider.inspect.assert_not_awaited()
    h.repository.record_inventory.assert_not_awaited()
    h.repository.commit_release.assert_awaited_once()
    observation = h.repository.observe_allocation.await_args.kwargs["observation"]
    assert observation["observed_state"] == "absent"
    assert observation["scope_complete"] is True


@pytest.mark.asyncio
async def test_incomplete_scope_still_blocks_release_of_an_identified_server(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.repository.retirement_impact.return_value = {"blocked_reasons": ["unmanaged_scope"]}

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.operation["step"] == "waiting_for_retention"
    h.repository.commit_release.assert_not_awaited()
    h.provider.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_precommit_terminal_retire_skips_impossible_retention_wait(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.provider.inspect.return_value = observed(state="terminated")
    h.repository.retirement_impact.return_value = {"blocked_reasons": ["retained_groups_present"]}

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    assert h.operation["result"]["external_pending"] is False
    h.repository.begin_retirement.assert_awaited_once()
    h.repository.retirement_impact.assert_not_awaited()
    h.repository.record_inventory.assert_not_awaited()
    h.repository.commit_release.assert_awaited_once()
    h.provider.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_settles_retirement_already_observed_terminal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(desired_state="absent", observed_state="terminated")

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    h.commands._require_managed.assert_not_called()
    h.repository.begin_retirement.assert_not_awaited()
    h.repository.commit_release.assert_awaited_once()
    h.provider.inspect.assert_not_awaited()
    h.provider.release.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_failed_provisioning_is_released_and_never_deployed(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.return_value = ProviderReceipt(outcome="pending", resource=REF)
    # CloudLab marks an experiment failed when it cannot set it up; it holds
    # no Host, so the acquisition deletes it instead of waiting forever.
    h.provider.inspect.return_value = ResourceObservation(
        ref=REF, state="failed", raw_state="failed", observed_at=NOW, lease_known=True
    )
    await h.reconciler.execute(h.operation)
    h.provider.release.assert_awaited_once()
    assert h.provider.release.await_args.args[0].external_id == REF.external_id
    assert h.allocation["observed_state"] == "terminated"
    assert h.operation["state"] == "failed"
    assert h.repository.finish_operation.await_args.kwargs["state"] == "failed"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "resource_failed"
    h.repository.prepare_bootstrap_binding.assert_not_awaited()
    h.bootstrap.deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_attachment_is_reported_but_never_released(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation["mode"] = "attach"
    h.provider.inspect.return_value = ResourceObservation(
        ref=REF, state="failed", raw_state="failed", observed_at=NOW, lease_known=True
    )
    await h.reconciler.execute(h.operation)
    h.provider.release.assert_not_awaited()
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "resource_failed"


@pytest.mark.asyncio
async def test_missing_resource_past_its_recorded_lease_has_ended(monkeypatch):
    h = harness(monkeypatch, kind="inspect", identified=True)
    expired = NOW - timedelta(hours=1)
    h.allocation["expires_at"] = expired
    h.provider.inspect.side_effect = ProviderError(
        "resource_visibility_unknown", "Experiment is not visible"
    )
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert h.allocation["observed_state"] == "terminated"
    assert h.allocation["provider_state"] == "expired_not_found"
    assert h.allocation["expires_at"] == expired


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", [timedelta(hours=1), timedelta(minutes=-5)])
async def test_missing_resource_within_its_lease_is_never_assumed_deleted(monkeypatch, expires_in):
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.allocation["expires_at"] = NOW + expires_in
    h.provider.inspect.side_effect = ProviderError(
        "resource_visibility_unknown", "Experiment is not visible"
    )
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert h.allocation.get("observed_state") != "terminated"
    h.repository.record_observation_error.assert_awaited_once()


REGISTERED_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdO7VBdfUlnQkwf4yfnmrk1h3G625rktRv0VgZa5awA"


def registering(monkeypatch):
    """A provisioned node that must register its host keys before any SSH."""
    h = harness(monkeypatch)
    # The adapter echoes the full validated configuration in each observation.
    ref = REF.model_copy(update={"config": {**REF.config, "host_key_source": "registration"}})
    h.allocation["configuration"] = ref.config
    h.commands.configuration.registration_url = "https://example.org/v1/compute/node-registrations"
    connection = SshConnection(address="node.example.org", username="agent", credential_ref="ssh-1")
    node = NODE.model_copy(update={"connection": connection})
    ready = observed().model_copy(update={"ref": ref, "nodes": (node,)})
    h.provider.acquire.return_value = ProviderReceipt(
        outcome="confirmed", resource=ref, observation=ready
    )
    h.provider.inspect.return_value = ready
    h.repository.create_node_registration = AsyncMock(return_value={"id": "registration-a"})
    h.repository.node_registration = AsyncMock(
        return_value={"registered_at": None, "expired": False, "host_keys": []}
    )
    h.repository.record_host_key = AsyncMock()
    return h


@pytest.mark.asyncio
async def test_a_registering_node_gets_a_token_whose_hash_alone_is_kept(monkeypatch):
    h = registering(monkeypatch)
    await h.reconciler.execute(h.operation)
    request = h.provider.acquire.await_args.args[0]
    token = request.registration.token.get_secret_value()
    assert request.registration.url == "https://example.org/v1/compute/node-registrations"
    stored = h.repository.create_node_registration.await_args.kwargs
    assert stored["token_hash"] == hash_secret(token) and token not in str(stored)
    assert "registration" not in request.config and token not in str(h.allocation)
    # Ready, but unregistered: nothing has connected to the node yet.
    assert (h.operation["state"], h.operation["step"]) == ("waiting", "waiting_for_registration")
    h.bootstrap.deploy.assert_not_awaited()
    h.repository.prepare_bootstrap_binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_registered_key_is_pinned_before_the_first_ssh_connection(monkeypatch):
    h = registering(monkeypatch)
    rsa_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7"
    h.repository.node_registration.return_value = {
        "registered_at": NOW,
        "expired": False,
        "host_keys": [rsa_key, REGISTERED_KEY],
    }
    await h.reconciler.execute(h.operation)
    h.repository.record_host_key.assert_awaited_once()
    assert h.repository.record_host_key.await_args.kwargs["host_key"] == REGISTERED_KEY
    deployed_node = h.bootstrap.deploy.await_args.args[0]
    assert deployed_node.connection.host_key == REGISTERED_KEY
    assert deployed_node.connection.known_hosts_ref is None
    assert h.operation["state"] == "succeeded"


@pytest.mark.asyncio
async def test_a_node_that_never_registers_blocks_instead_of_being_trusted(monkeypatch):
    h = registering(monkeypatch)
    h.repository.node_registration.return_value = {
        "registered_at": None,
        "expired": True,
        "host_keys": [],
    }
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "registration_expired"
    h.bootstrap.deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_a_public_address_no_registering_node_is_created(monkeypatch):
    h = registering(monkeypatch)
    h.commands.configuration.registration_url = ""
    await h.reconciler.execute(h.operation)
    h.provider.acquire.assert_not_awaited()
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "registration_unavailable"


RELEASE = HostRelease(
    sha="c" * 40,
    digest="d" * 64,
    bundle=b"host-bundle",
    path=Path("/app/compute-host-release/source.tar.gz"),
)


def _host_view(*, capabilities: list[str], **overrides: object) -> dict:
    view = {
        "host": "compute-nodeid",
        "status": "online",
        "enrolled": True,
        "last_heartbeat_at": NOW,
        "capabilities": capabilities,
    }
    view.update(overrides)
    return view


def _arm_upgrade(monkeypatch, h, release: HostRelease = RELEASE) -> HostRelease:
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", lambda: release)
    h.operation.update(
        kind="upgrade",
        parameters={"allocation_id": h.allocation["id"], "deployment_sha": release.sha},
        result={},
    )
    h.repository.advance_host_release = AsyncMock()
    return release


@pytest.mark.asyncio
async def test_a_managed_host_off_the_control_plane_commit_is_queued(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation["observed_state"] = "ready"
    h.repository.create_operation = AsyncMock()
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", lambda: RELEASE)
    await h.reconciler.schedule()
    queued = h.repository.create_operation.await_args.kwargs
    parameters = {"allocation_id": h.allocation["id"], "deployment_sha": RELEASE.sha}
    assert queued == {
        "request_id": f"host-release:{h.allocation['id']}:{RELEASE.sha}",
        "fingerprint": fingerprint("upgrade", parameters),
        "kind": "upgrade",
        "allocation_id": h.allocation["id"],
        "parameters": parameters,
        "requested_by": "compute-host-release",
    }


@pytest.mark.asyncio
async def test_no_host_release_is_queued_without_a_control_plane_bundle(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation["observed_state"] = "ready"
    h.repository.create_operation = AsyncMock()
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", lambda: None)
    await h.reconciler.schedule()
    h.repository.create_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_host_already_on_the_release_is_not_queued_again(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation["observed_state"] = "ready"
    h.allocation["deployment_profile"].update(
        deployment_sha=RELEASE.sha, source_sha256=RELEASE.digest
    )
    h.allocation["hosts"][0]["capabilities"] = [shared_deployment_capability(RELEASE.sha)]
    h.repository.create_operation = AsyncMock()
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", lambda: RELEASE)
    await h.reconciler.schedule()
    h.repository.create_operation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["drain", "acquire", "duplicate"])
async def test_host_release_waits_for_an_exclusive_machine(monkeypatch, blocker):
    h = harness(monkeypatch, identified=True)
    h.allocation["observed_state"] = "ready"
    h.repository.create_operation = AsyncMock()
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", lambda: RELEASE)
    if blocker == "drain":
        h.allocation["hosts"][0]["drain_fence_active"] = True
    elif blocker == "acquire":
        h.repository.list_operations.return_value = [
            {"kind": "acquire", "state": "running", "request_id": "busy"}
        ]
    else:
        h.repository.list_operations.return_value = [
            {
                "kind": "upgrade",
                "state": "succeeded",
                "request_id": f"host-release:{h.allocation['id']}:{RELEASE.sha}",
            }
        ]
    await h.reconciler.schedule()
    h.repository.create_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_upgrade_deploys_the_release_then_records_it(monkeypatch):
    h = harness(monkeypatch, identified=True)
    release = _arm_upgrade(monkeypatch, h)
    old = shared_deployment_capability(PROFILE.deployment_sha)
    new = shared_deployment_capability(release.sha)
    h.host_pool.get_host = AsyncMock(
        side_effect=[_host_view(capabilities=[old]), _host_view(capabilities=[new])]
    )
    await h.reconciler.execute(h.operation)
    deployed = h.bootstrap.deploy.await_args
    assert deployed.args[1].deployment_sha == release.sha
    assert deployed.args[1].source_sha256 == release.digest
    assert deployed.args[1].source_bundle == str(release.path)
    assert deployed.args[3] is None
    assert deployed.kwargs == {"prerequisites": False, "reenroll": False}
    h.repository.advance_host_release.assert_awaited_once_with(
        allocation_id=h.allocation["id"],
        previous_sha=PROFILE.deployment_sha,
        previous_digest=PROFILE.source_sha256,
        deployment_sha=release.sha,
        source_sha256=release.digest,
        source_bundle=release.bundle,
        release_bundle_path=str(release.path),
        operation_id=h.operation["id"],
        generation=1,
        worker_id="worker-a",
    )
    assert h.operation["state"] == "succeeded"
    assert h.operation["result"]["deployment_sha"] == release.sha
    h.repository.record_observation_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_upgrade_records_a_host_that_already_advertises_the_release(monkeypatch):
    h = harness(monkeypatch, identified=True)
    release = _arm_upgrade(monkeypatch, h)
    h.host_pool.get_host = AsyncMock(
        return_value=_host_view(capabilities=[shared_deployment_capability(release.sha)])
    )
    await h.reconciler.execute(h.operation)
    h.bootstrap.deploy.assert_not_awaited()
    h.repository.advance_host_release.assert_awaited_once()
    assert h.operation["state"] == "succeeded"


@pytest.mark.asyncio
async def test_a_dark_host_is_restored_to_the_previous_bundle(monkeypatch):
    h = harness(monkeypatch, identified=True)
    release = _arm_upgrade(monkeypatch, h)
    failed = BootstrapResult(
        ready=False,
        host="compute-nodeid",
        deployment_sha=release.sha,
        blocked_reasons=("host_deployment_failed",),
        advertising=False,
    )
    restored = BootstrapResult(
        ready=True, host="compute-nodeid", deployment_sha=PROFILE.deployment_sha
    )
    h.bootstrap.deploy = AsyncMock(side_effect=[failed, restored])
    h.host_pool.get_host = AsyncMock(
        return_value=_host_view(capabilities=[shared_deployment_capability(PROFILE.deployment_sha)])
    )
    await h.reconciler.execute(h.operation)
    assert h.bootstrap.deploy.await_count == 2
    rollback = h.bootstrap.deploy.await_args_list[1]
    assert rollback.args[1].deployment_sha == PROFILE.deployment_sha
    assert rollback.args[1].source_sha256 == PROFILE.source_sha256
    assert rollback.args[3] is None
    assert rollback.kwargs == {"prerequisites": False, "reenroll": False}
    h.repository.advance_host_release.assert_not_awaited()
    assert h.operation["state"] == "blocked"
    assert h.operation["step"] == "restored"
    assert h.operation["result"]["restored"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("advertising", [True, None])
async def test_a_rollout_that_may_still_be_serving_is_not_restored(monkeypatch, advertising):
    h = harness(monkeypatch, identified=True)
    release = _arm_upgrade(monkeypatch, h)
    h.operation["result"] = {"release_attempts": 2} if advertising is True else {}
    h.bootstrap.deploy = AsyncMock(
        return_value=BootstrapResult(
            ready=False,
            host="compute-nodeid",
            deployment_sha=release.sha,
            blocked_reasons=("host_deployment_failed",),
            advertising=advertising,
        )
    )
    h.host_pool.get_host = AsyncMock(
        return_value=_host_view(capabilities=[shared_deployment_capability(PROFILE.deployment_sha)])
    )
    await h.reconciler.execute(h.operation)
    h.bootstrap.deploy.assert_awaited_once()
    h.repository.advance_host_release.assert_not_awaited()
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "host_deployment_failed"
    assert "restored" not in h.operation["result"]


@pytest.mark.asyncio
async def test_a_still_advertising_rollout_retries_before_it_blocks(monkeypatch):
    h = harness(monkeypatch, identified=True)
    release = _arm_upgrade(monkeypatch, h)
    h.bootstrap.deploy = AsyncMock(
        return_value=BootstrapResult(
            ready=False,
            host="compute-nodeid",
            deployment_sha=release.sha,
            blocked_reasons=("host_deployment_failed",),
            advertising=True,
        )
    )
    h.host_pool.get_host = AsyncMock(
        return_value=_host_view(capabilities=[shared_deployment_capability(PROFILE.deployment_sha)])
    )
    await h.reconciler.execute(h.operation)
    finished = h.repository.finish_operation.await_args.kwargs
    assert finished["state"] == "waiting"
    assert finished["result"] == {"release_attempts": 1}
    assert finished["retry_after_seconds"] == 300
    h.bootstrap.deploy.assert_awaited_once()


@pytest.mark.asyncio
async def test_upgrade_failure_does_not_mark_the_allocation_unhealthy(monkeypatch):
    h = harness(monkeypatch, identified=True)
    _arm_upgrade(monkeypatch, h)
    h.bootstrap.deploy = AsyncMock(
        side_effect=ProviderError("result_unknown", "The connection dropped", retryable=True)
    )
    h.host_pool.get_host = AsyncMock(
        return_value=_host_view(capabilities=[shared_deployment_capability(PROFILE.deployment_sha)])
    )
    await h.reconciler.execute(h.operation)
    h.repository.record_observation_error.assert_not_awaited()
    assert h.operation["state"] == "waiting"
    assert h.operation["step"] == "deploying"


@pytest.mark.asyncio
async def test_an_active_upgrade_renews_the_lease_and_does_not_rotate(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation.update(
        observed_state="ready",
        created_at=NOW - timedelta(days=8),
        expires_at=NOW + timedelta(minutes=30),
        policy=RenewalPolicy(
            enabled=True,
            reason="Keep the evaluation sandboxes",
            renew_before_seconds=3600,
            extend_to_horizon_seconds=8 * 3600,
            rotation_enabled=True,
            node_types=["r6615"],
        ).model_dump(),
    )
    upgrade = {
        "kind": "upgrade",
        "state": "running",
        "request_id": f"host-release:{h.allocation['id']}:{RELEASE.sha}",
        "parameters": {"allocation_id": h.allocation["id"], "deployment_sha": RELEASE.sha},
    }
    h.repository.list_operations.return_value = [upgrade]
    h.repository.create_operation = AsyncMock()
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", lambda: RELEASE)
    await h.reconciler.schedule()
    h.repository.create_operation.assert_not_awaited()
    h.commands.extend.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_finished_upgrade_lets_an_old_node_rotate(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation.update(
        observed_state="ready",
        created_at=NOW - timedelta(days=8),
        expires_at=NOW + timedelta(minutes=30),
        policy=RenewalPolicy(
            enabled=True,
            reason="Keep the evaluation sandboxes",
            renew_before_seconds=3600,
            extend_to_horizon_seconds=8 * 3600,
            rotation_enabled=True,
            node_types=["r6615"],
        ).model_dump(),
    )
    h.repository.list_operations.return_value = [
        {
            "kind": "upgrade",
            "state": "succeeded",
            "request_id": f"host-release:{h.allocation['id']}:{RELEASE.sha}",
            "parameters": {"deployment_sha": RELEASE.sha},
        }
    ]
    h.repository.create_operation = AsyncMock()
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", lambda: RELEASE)
    await h.reconciler.schedule()
    h.commands.extend.assert_not_awaited()
    assert h.repository.create_operation.await_args.kwargs["kind"] == "rotate"
