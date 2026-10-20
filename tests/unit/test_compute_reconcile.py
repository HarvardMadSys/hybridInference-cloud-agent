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
from cloud_agent.domain.compute import autoscaling_acquire_can_cancel, fingerprint, public_operation
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
    HostInventory,
    PreflightResult,
)
from cloud_agent.sandbox_manager_client import SandboxManagerError
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
        set_move_out_hold=AsyncMock(return_value=False),
        unmoved_groups=AsyncMock(return_value=0),
        move_out_report=AsyncMock(
            return_value={"moving": False, "blocker": None, "unmoved": 0, "unmovable": 0}
        ),
        lose_reclaimed_groups=AsyncMock(return_value=[]),
        prepare_bootstrap_binding=AsyncMock(),
        clear_bootstrap_hold=AsyncMock(return_value=True),
        renew_operation_lease=AsyncMock(return_value=True),
        begin_retirement=AsyncMock(),
        retirement_impact=AsyncMock(return_value={"blocked_reasons": []}),
        commit_release=AsyncMock(return_value={"blocked_reasons": []}),
        record_inventory=AsyncMock(return_value=True),
        authorize_superseded_removals=AsyncMock(return_value=[]),
        record_superseded_removal=AsyncMock(),
        superseded_removals=AsyncMock(return_value=0),
        recorded_host_state=AsyncMock(return_value={"checkpoints": {}, "removals": {}}),
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
    # Four hours from expiry the Host is not moving out.
    assert h.repository.set_move_out_hold.await_args.kwargs["due"] is False


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
@pytest.mark.parametrize("pending_receipt", [False, True])
async def test_an_extension_never_applied_stops_holding_the_allocation(
    monkeypatch, pending_receipt
):
    """Every other operation of the server waits for it: the wait must end."""
    h = harness(monkeypatch, kind="extend", identified=True)
    if pending_receipt:
        # Accepted, but the expiration never moves.
        h.provider.extend.return_value = ProviderReceipt(
            outcome="pending", resource=REF, observation=observed()
        )
    else:
        # The adapter's answer to an HTTP 5xx or a timeout on the request.
        h.provider.extend.return_value = ProviderReceipt(outcome="unknown", resource=REF)
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["result"]["external_pending"]) == ("waiting", True)
    monkeypatch.setattr(compute_reconcile, "_now", lambda: NOW + timedelta(minutes=9))
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["result"]["external_pending"]) == ("waiting", True)
    monkeypatch.setattr(compute_reconcile, "_now", lambda: NOW + timedelta(minutes=11))
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["step"]) == ("blocked", "extension_unconfirmed")
    assert h.operation["result"]["external_pending"] is False
    assert [reason["code"] for reason in h.operation["result"]["blocked_reasons"]] == [
        "extension_unconfirmed"
    ]
    h.provider.extend.assert_awaited_once()
    # An administrator's retry queries first, then sends the same absolute target.
    h.provider.extend.return_value = ProviderReceipt(
        outcome="confirmed", resource=REF, observation=observed(expires=TARGET)
    )
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert [call.args[1].target_expires_at for call in h.provider.extend.await_args_list] == [
        TARGET,
        TARGET,
    ]


@pytest.mark.asyncio
async def test_an_extension_recorded_without_its_send_time_is_timed_from_now(monkeypatch):
    h = harness(monkeypatch, kind="extend", identified=True)
    h.operation.update(
        created_at=NOW - timedelta(days=1),
        step="confirming_extension",
        result={
            "extension_requested": True,
            "external_pending": True,
            "extension_initial_expires_at": (NOW + timedelta(hours=4)).isoformat(),
        },
    )
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["step"]) == ("waiting", "confirming_extension")
    monkeypatch.setattr(compute_reconcile, "_now", lambda: NOW + timedelta(minutes=11))
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["step"]) == ("blocked", "extension_unconfirmed")
    h.provider.extend.assert_not_awaited()


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
async def test_an_expiration_kept_to_the_minute_is_the_full_extension(monkeypatch):
    """A provider that stores expirations coarsely has still granted the request."""
    target = (NOW + timedelta(hours=8)).replace(second=30, microsecond=500000)
    stored = target.replace(second=0, microsecond=0)
    h = harness(monkeypatch, kind="extend", identified=True)
    h.operation["parameters"]["target_expires_at"] = target.isoformat()
    h.provider.extend.return_value = ProviderReceipt(
        outcome="pending", resource=REF, observation=observed(expires=stored)
    )
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["step"]) == ("succeeded", "extended")
    assert h.operation["result"]["actual_expires_at"] == stored.isoformat()
    # A minute short of the requested minute remains a partial extension.
    h = harness(monkeypatch, kind="extend", identified=True)
    h.operation["parameters"]["target_expires_at"] = target.isoformat()
    short = stored - timedelta(minutes=1)
    h.provider.extend.return_value = ProviderReceipt(
        outcome="pending", resource=REF, observation=observed(expires=short)
    )
    h.provider.inspect.side_effect = [observed(), observed(expires=short)]
    await h.reconciler.execute(h.operation)
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["step"]) == ("blocked", "partial_extension")


@pytest.mark.asyncio
async def test_automatic_renewal_requests_a_whole_second(monkeypatch):
    h = harness(monkeypatch, kind="inspect", identified=True)
    monkeypatch.setattr(compute_reconcile, "_now", lambda: NOW.replace(microsecond=123456))
    h.allocation.update(
        expires_at=NOW + timedelta(minutes=10),
        policy=RenewalPolicy(enabled=True, reason="Keep the sandboxes").model_dump(),
    )
    await h.reconciler.schedule()
    target = h.commands.extend.await_args.args[1].target_expires_at
    assert target.microsecond == 0
    assert target == (NOW + timedelta(hours=4)).replace(microsecond=0)


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


def _left_behind_container():
    return {
        "sandbox_id": "asbx_moved",
        "generation": 1,
        "container_id": "container-old",
        "origin_thread_id": "athr_moved",
        "running": False,
    }


def _left_behind(h, *, removal=None):
    """A Host holding one stopped container its moved group left behind."""
    container = _left_behind_container()
    h.sandbox_manager.list_sandboxes = AsyncMock(side_effect=[[container], []])
    h.sandbox_manager.discard_sandbox_deletion = AsyncMock(side_effect=removal)
    authorized = {key: container[key] for key in container if key != "running"}
    h.repository.authorize_superseded_removals = AsyncMock(
        return_value=[{**authorized, "request_id": "superseded-recorded"}]
    )
    h.repository.record_superseded_removal = AsyncMock()
    # The durable count follows what was confirmed.
    h.repository.superseded_removals = AsyncMock(
        side_effect=lambda **_: h.repository.record_superseded_removal.await_count
    )
    h.bootstrap.inventory.return_value = HostInventory(complete=True)

    def receipt(_thread_id, payload):
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    if removal is None:
        h.sandbox_manager.discard_sandbox_deletion.side_effect = receipt
    return container


@pytest.mark.asyncio
async def test_release_removes_what_moved_groups_left_behind_then_releases(monkeypatch):
    """The Host still holds the stopped container of a group that moved away.

    Nobody has written to that chat since, so the group is still ``cold``:
    its container goes anyway, and the release goes on.
    """
    h = harness(monkeypatch, kind="retire", identified=True)
    container = _left_behind(h)

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    assert h.operation["result"]["superseded_removed"] == 1
    h.repository.authorize_superseded_removals.assert_awaited_once_with(
        host="compute-nodeid", containers=[container], operation_id=h.operation["id"]
    )
    thread_id, payload = h.sandbox_manager.discard_sandbox_deletion.await_args.args
    assert thread_id == "athr_moved"
    # Sent under the request id the authorization recorded.
    assert payload == {
        "request_id": "superseded-recorded",
        "generation": 1,
        "expected_container_id": "container-old",
        "sandbox_id": "asbx_moved",
    }
    h.repository.record_superseded_removal.assert_awaited_once_with(
        request_id="superseded-recorded"
    )
    h.bootstrap.inventory.assert_awaited_once()
    h.repository.commit_release.assert_awaited_once()
    h.provider.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_keeps_a_container_still_needed_and_stops_for_attention(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h)
    h.repository.authorize_superseded_removals.return_value = []

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "retained_groups_present"
    h.sandbox_manager.discard_sandbox_deletion.assert_not_awaited()
    h.repository.commit_release.assert_not_awaited()
    h.provider.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_never_offers_a_running_container_for_removal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    container = _left_behind(h)
    container["running"] = True
    h.repository.authorize_superseded_removals.return_value = []

    await h.reconciler.execute(h.operation)

    h.repository.authorize_superseded_removals.assert_awaited_once_with(
        host="compute-nodeid", containers=[], operation_id=h.operation["id"]
    )
    h.sandbox_manager.discard_sandbox_deletion.assert_not_awaited()
    assert h.operation["state"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 409, 429, 503])
async def test_a_removal_that_does_not_answer_is_retried_as_the_same_request(monkeypatch, status):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(status, "unavailable"))

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == (
        "superseded_container_not_removed"
    )
    h.repository.commit_release.assert_not_awaited()
    h.repository.record_superseded_removal.assert_not_awaited()
    first = h.sandbox_manager.discard_sandbox_deletion.await_args.args[1]["request_id"]
    assert first == "superseded-recorded"

    h.operation.update(state="running", step="pending")
    h.sandbox_manager.list_sandboxes.side_effect = None
    h.sandbox_manager.list_sandboxes.return_value = [_left_behind_container()]
    await h.reconciler.execute(h.operation)
    assert h.sandbox_manager.discard_sandbox_deletion.await_args.args[1]["request_id"] == first


@pytest.mark.asyncio
async def test_a_removal_whose_answer_was_lost_is_confirmed_before_release(monkeypatch):
    """The Host removed it, but the answer never arrived: nothing lists it any more."""
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(0, "unavailable"))

    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"

    def receipt(_thread_id, payload):
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    h.operation.update(state="running", step="pending")
    h.sandbox_manager.list_sandboxes.side_effect = None
    h.sandbox_manager.list_sandboxes.return_value = []
    h.sandbox_manager.discard_sandbox_deletion.side_effect = receipt
    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["result"]["superseded_removed"] == 1
    assert h.repository.authorize_superseded_removals.await_args.kwargs["containers"] == []
    first, second = h.sandbox_manager.discard_sandbox_deletion.await_args_list
    assert first.args[1]["request_id"] == second.args[1]["request_id"] == "superseded-recorded"
    h.repository.record_superseded_removal.assert_awaited_once_with(
        request_id="superseded-recorded"
    )


@pytest.mark.asyncio
async def test_removals_confirmed_before_another_fails_still_count(monkeypatch):
    """A of two is confirmed, B gets no answer; once B goes too, both count."""
    h = harness(monkeypatch, kind="retire", identified=True)
    first = _left_behind_container()
    second = {**first, "sandbox_id": "asbx_other", "container_id": "container-other"}
    _left_behind(h)
    h.repository.authorize_superseded_removals.return_value = [
        {
            **{k: v for k, v in c.items() if k != "running"},
            "request_id": "superseded-" + c["sandbox_id"],
        }
        for c in (first, second)
    ]
    answers = iter([None, SandboxManagerError(503, "busy")])

    def removal(_thread_id, payload):
        answer = next(answers, None)
        if answer is not None:
            raise answer
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    h.sandbox_manager.list_sandboxes = AsyncMock(return_value=[first, second])
    h.sandbox_manager.discard_sandbox_deletion = AsyncMock(side_effect=removal)

    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["superseded_removed"] == 1

    h.operation.update(state="running", step="pending")
    h.repository.authorize_superseded_removals.return_value = [
        h.repository.authorize_superseded_removals.return_value[1]
    ]
    h.sandbox_manager.list_sandboxes = AsyncMock(side_effect=[[second], []])
    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["result"]["superseded_removed"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 410, 422])
async def test_a_host_refusing_the_removal_stops_the_release_for_attention(monkeypatch, status):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(status, "refused"))

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        {"deleted": False},
        {"deleted": True, "container_id": "container-other"},
        {"deleted": True, "request_id": "another-request"},
    ],
)
async def test_an_unconfirmed_removal_does_not_count_as_one(monkeypatch, receipt):
    h = harness(monkeypatch, kind="retire", identified=True)

    def answer(_thread_id, payload):
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            **receipt,
        }

    _left_behind(h, removal=answer)

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    h.repository.record_superseded_removal.assert_not_awaited()
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_container_still_there_after_removal_still_blocks_the_release(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    container = _left_behind(h)
    h.sandbox_manager.list_sandboxes.side_effect = [[container], [container]]

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "retained_groups_present"
    h.repository.commit_release.assert_not_awaited()


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
        # Safety refusals of the deployment script are named, not opaque.
        (("deployment_identity_conflict",), "deployment_identity_conflict"),
        (("unsafe_deployment_path",), "unsafe_deployment_path"),
        (("invalid_deployment_archive",), "invalid_deployment_archive"),
    ],
)
async def test_bootstrap_refusal_reports_only_public_diagnostics(
    monkeypatch, reasons, expected_code
):
    """A failed deployment remains blocked and never renders arbitrary remote output."""
    h = harness(monkeypatch, identified=True)
    resource_ref = ResourceRef(
        external_id="f47eb2a4-a0ac-4656-80da-8a638d2d263c",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "one-node",
            "profile_project": "FAST25-AE",
            "bindings": {"cluster": "utah", "phystype": "r6615"},
        },
    )
    h.allocation["external_id"] = resource_ref.external_id
    h.allocation["configuration"] = resource_ref.config
    h.provider.inspect.return_value = observed().model_copy(
        update={
            "ref": resource_ref,
            "provider_http_status": 200,
            "provider_request_id": "d14b57a4-7e2b-4f49-8b4b-4ca3a9410a4d",
        }
    )
    h.bootstrap.deploy.return_value = BootstrapResult(
        ready=False,
        host="compute-nodeid",
        deployment_sha=PROFILE.deployment_sha,
        blocked_reasons=reasons,
    )
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert h.operation["step"] == "deploying"
    assert [item["code"] for item in h.operation["result"]["blocked_reasons"]] == [expected_code]
    assert "secret-looking-remote-output" not in str(h.operation["result"])
    diagnostic = h.operation["result"]["provider_diagnostic"]
    assert diagnostic == {
        "provider_operation": "experiments.read",
        "http_status": 200,
        "provider_reason_code": "experiment_ready",
        "provider_request_id": "d14b57a4-7e2b-4f49-8b4b-4ca3a9410a4d",
        "external_id": resource_ref.external_id,
        "requested_project": "research",
        "requested_profile": "one-node",
        "requested_profile_project": "FAST25-AE",
        "requested_site": "utah",
        "requested_hardware": "r6615",
        "provider_state": "ready",
        "provider_observed_at": NOW.isoformat(),
        "stage": "deploying",
        "operation_id": "operation-a",
        "allocation_id": "allocation-a",
        "operation_outcome": "blocked",
        "retryable": False,
        "recorded_at": NOW.isoformat(),
    }
    assert public_operation(h.operation)["result"]["provider_diagnostic"] == diagnostic
    h.bootstrap.readiness.assert_not_awaited()
    h.repository.clear_bootstrap_hold.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_provisioning_is_released_and_never_deployed(monkeypatch):
    h = harness(monkeypatch)
    failed_ref = REF.model_copy(update={"external_id": "f47eb2a4-a0ac-4656-80da-8a638d2d263c"})
    h.provider.acquire.return_value = ProviderReceipt(outcome="pending", resource=failed_ref)
    # CloudLab marks an experiment failed when it cannot set it up; it holds
    # no Host, so the acquisition deletes it instead of waiting forever.
    h.provider.inspect.return_value = ResourceObservation(
        ref=failed_ref,
        state="failed",
        raw_state="failed",
        observed_at=NOW,
        provider_http_status=200,
        lease_known=True,
    )
    await h.reconciler.execute(h.operation)
    h.provider.release.assert_awaited_once()
    assert h.provider.release.await_args.args[0].external_id == failed_ref.external_id
    assert h.allocation["observed_state"] == "terminated"
    assert h.operation["state"] == "failed"
    assert h.repository.finish_operation.await_args.kwargs["state"] == "failed"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "resource_failed"
    diagnostic = h.operation["result"]["provider_diagnostic"]
    assert diagnostic["provider_operation"] == "experiments.read"
    assert diagnostic["http_status"] == 200
    assert diagnostic["provider_reason_code"] == "experiment_failed_no_provider_reason"
    assert diagnostic["external_id"] == failed_ref.external_id
    assert diagnostic["provider_state"] == "failed"
    assert diagnostic["provider_observed_at"] == NOW.isoformat()
    assert diagnostic["stage"] == "failed_resource_released"
    assert diagnostic["operation_outcome"] == "failed"
    assert diagnostic["recorded_at"] == NOW.isoformat()
    assert public_operation(h.operation)["result"]["provider_diagnostic"] == diagnostic
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
async def test_a_retry_after_a_missed_registration_reopens_the_same_one(monkeypatch):
    h = registering(monkeypatch)
    h.repository.node_registration.return_value = {
        "registered_at": None,
        "expired": True,
        "host_keys": [],
    }
    h.repository.reopen_node_registration = AsyncMock(return_value=True)
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "registration_expired"
    h.repository.reopen_node_registration.assert_not_awaited()
    # The administrator retries: the node's own token gets another window.
    await h.reconciler.execute(h.operation)
    h.repository.reopen_node_registration.assert_awaited_once()
    reopened = h.repository.reopen_node_registration.await_args.kwargs
    assert reopened["allocation_id"] == h.allocation["id"]
    assert (h.operation["state"], h.operation["step"]) == ("waiting", "waiting_for_registration")
    assert h.operation["result"]["blocked_reasons"] == []
    # Restarted, the node posts that token; no second experiment or token exists.
    h.repository.node_registration.return_value = {
        "registered_at": NOW,
        "expired": False,
        "host_keys": [REGISTERED_KEY],
    }
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    h.provider.acquire.assert_awaited_once()
    h.repository.create_node_registration.assert_awaited_once()
    assert h.bootstrap.deploy.await_args.args[0].connection.host_key == REGISTERED_KEY


@pytest.mark.asyncio
async def test_a_node_that_never_registered_is_released_without_an_ssh_scan(monkeypatch):
    """No Host ever ran there, and without a pinned key it cannot be reached."""
    h = harness(monkeypatch, kind="retire", identified=True)
    # The adapter echoes the full validated configuration in each observation.
    ref = REF.model_copy(update={"config": {**REF.config, "host_key_source": "registration"}})
    h.allocation["configuration"] = ref.config
    h.provider.inspect.return_value = observed().model_copy(update={"ref": ref})
    h.allocation["hosts"] = []
    h.repository.node_registration = AsyncMock(
        return_value={"registered_at": None, "expired": True, "host_keys": []}
    )
    await h.reconciler.execute(h.operation)
    assert (h.operation["state"], h.operation["step"]) == ("succeeded", "released")
    h.bootstrap.inventory.assert_not_awaited()
    recorded = h.repository.record_inventory.await_args.kwargs
    assert recorded["verified_node_ids"] == ["node-id"] and recorded["hosts"] == []
    h.repository.commit_release.assert_awaited_once()
    h.provider.release.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reached", ["pinned_key", "bound_host", "registered"])
async def test_a_registering_node_this_control_plane_reached_is_still_scanned(monkeypatch, reached):
    h = harness(monkeypatch, kind="retire", identified=True)
    ref = REF.model_copy(update={"config": {**REF.config, "host_key_source": "registration"}})
    h.allocation["configuration"] = ref.config
    h.provider.inspect.return_value = observed().model_copy(update={"ref": ref})
    h.repository.node_registration = AsyncMock(
        return_value={"registered_at": None, "expired": True, "host_keys": []}
    )
    if reached == "registered":
        # A key an earlier observation cleared does not make the node unreached.
        h.allocation["hosts"] = []
        h.repository.node_registration.return_value = {
            "registered_at": NOW,
            "expired": False,
            "host_keys": [REGISTERED_KEY],
        }
    elif reached == "pinned_key":
        h.allocation["hosts"] = []
        pinned = SshConnection(
            address="node.example.org",
            username="agent",
            credential_ref="ssh-1",
            host_key=REGISTERED_KEY,
        )
        node = NODE.model_copy(update={"connection": pinned})
        h.provider.inspect.return_value = observed().model_copy(
            update={"ref": ref, "nodes": (node,)}
        )
    else:
        h.allocation["hosts"][0]["compute_node_id"] = "node-id"
    h.bootstrap.inventory.return_value = HostInventory(complete=True)
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    h.bootstrap.inventory.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_accepted_create_correlation_survives_followup_read_failure(monkeypatch):
    h = harness(monkeypatch)
    cloudlab_ref = ResourceRef(
        external_id="f47eb2a4-a0ac-4656-80da-8a638d2d263c",
        config={"credential_ref": "account-1", "project": "research"},
    )
    submission_id = "d14b57a4-7e2b-4f49-8b4b-4ca3a9410a4d"
    followup_id = "a2cd14af-4c77-49c0-85cf-3a58e0dfbb2b"
    h.provider.acquire.return_value = ProviderReceipt(
        outcome="pending",
        resource=cloudlab_ref,
        diagnostics={
            "provider_operation": "experiments.create",
            "http_status": 201,
            "provider_reason_code": "request_accepted",
            "provider_request_id": submission_id,
            "external_id": cloudlab_ref.external_id,
        },
    )
    h.provider.inspect.side_effect = ProviderError(
        "temporarily_unavailable",
        "CloudLab could not be reached",
        retryable=True,
        diagnostics={
            "provider_operation": "experiments.read",
            "provider_reason_code": "request_failed",
            "provider_request_id": followup_id,
            "external_id": cloudlab_ref.external_id,
        },
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    diagnostic = h.operation["result"]["provider_diagnostic"]
    assert diagnostic["provider_operation"] == "experiments.read"
    assert diagnostic["provider_request_id"] == followup_id
    assert diagnostic["submission_request_id"] == submission_id
    assert diagnostic["submission_http_status"] == 201
    assert diagnostic["external_id"] == cloudlab_ref.external_id
    assert public_operation(h.operation)["result"]["provider_diagnostic"] == diagnostic


@pytest.mark.asyncio
async def test_definite_create_rejection_reports_original_stage_after_retry_reset(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.side_effect = ProviderError(
        "request_rejected",
        "CloudLab rejected the request",
        diagnostics={
            "provider_operation": "experiments.create",
            "http_status": 400,
            "provider_reason_code": "invalid_profile_binding",
        },
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert h.operation["step"] == "pending"
    diagnostic = h.operation["result"]["provider_diagnostic"]
    assert diagnostic["stage"] == "acquiring"
    assert diagnostic["provider_operation"] == "experiments.create"
    assert diagnostic["http_status"] == 400
    assert diagnostic["provider_reason_code"] == "invalid_profile_binding"
    assert public_operation(h.operation)["result"]["provider_diagnostic"] == diagnostic
    h.provider.acquire.assert_awaited_once()


def _recorded_events(monkeypatch, h) -> list:
    events = []

    async def notify(event, *, host, reason):
        events.append((event, host, reason))

    monkeypatch.setattr(h.reconciler.rotation, "_notify", notify)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (
            {"moving": False, "blocker": "host_without_checkpoints", "unmoved": 2, "unmovable": 2},
            "host_without_checkpoints; 2 groups would stay",
        ),
        (
            {"moving": True, "blocker": None, "unmoved": 3, "unmovable": 1},
            "1 of 3 groups cannot move",
        ),
    ],
)
async def test_groups_that_will_stay_are_reported_once_when_the_window_opens(
    monkeypatch, report, reason
):
    """Early enough to act, unlike the reminder just before the lease ends."""
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.allocation["expires_at"] = NOW + timedelta(minutes=110)
    h.repository.move_out_report.return_value = report
    events = _recorded_events(monkeypatch, h)

    await h.reconciler.schedule()
    await h.reconciler.schedule()

    assert events == [("compute_move_out_blocked", "compute-nodeid", reason)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expires_in", "report"),
    [
        # Before the window opens nothing is asked.
        (timedelta(hours=3), {"moving": False, "blocker": "no_restore_target", "unmoved": 2}),
        # Every group can move.
        (timedelta(minutes=90), {"moving": True, "blocker": None, "unmoved": 2, "unmovable": 0}),
        # Nothing left to move.
        (timedelta(minutes=90), {"moving": False, "blocker": "no_restore_target", "unmoved": 0}),
    ],
)
async def test_a_move_out_that_can_finish_is_not_reported(monkeypatch, expires_in, report):
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.allocation["expires_at"] = NOW + expires_in
    h.repository.move_out_report.return_value = {"unmovable": 0, **report}
    events = _recorded_events(monkeypatch, h)

    await h.reconciler.schedule()

    assert [event for event in events if event[0] == "compute_move_out_blocked"] == []


@pytest.mark.asyncio
async def test_a_reclaimed_machines_stranded_groups_are_lost_and_reported(monkeypatch):
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.allocation["observed_state"] = "terminated"
    h.repository.lose_reclaimed_groups.return_value = ["asbx_a", "asbx_b"]
    events = _recorded_events(monkeypatch, h)

    await h.reconciler.schedule()

    h.repository.lose_reclaimed_groups.assert_awaited_once_with(
        allocation_id=h.allocation["id"], quiet_seconds=600
    )
    assert events == [("compute_groups_lost_with_server", "compute-nodeid", "2 groups lost")]
    # A terminated machine is otherwise left alone.
    h.commands.queue_inspection.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_live_machine_loses_no_groups(monkeypatch):
    h = harness(monkeypatch, kind="inspect", identified=True)

    await h.reconciler.schedule()

    h.repository.lose_reclaimed_groups.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_drain_fence_left_on_a_host_off_the_release_is_reported_once(monkeypatch):
    """It keeps the Host from taking work and from ever being rolled forward."""
    h = harness(monkeypatch, identified=True)
    h.allocation["observed_state"] = "ready"
    h.allocation["hosts"][0]["drain_fence_active"] = True
    h.repository.create_operation = AsyncMock()
    events = _recorded_events(monkeypatch, h)
    row = h.allocation

    await h.reconciler._queue_host_release(row, [], RELEASE, NOW)
    await h.reconciler._queue_host_release(row, [], RELEASE, NOW + timedelta(minutes=29))
    assert events == []
    await h.reconciler._queue_host_release(row, [], RELEASE, NOW + timedelta(minutes=31))
    await h.reconciler._queue_host_release(row, [], RELEASE, NOW + timedelta(minutes=45))

    assert events == [
        ("compute_host_release_blocked", "compute-nodeid", "deployment drain fence active")
    ]
    h.repository.create_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_fence_that_is_lifted_in_time_is_never_reported(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation["observed_state"] = "ready"
    h.repository.create_operation = AsyncMock()
    events = _recorded_events(monkeypatch, h)
    row = h.allocation

    row["hosts"][0]["drain_fence_active"] = True
    await h.reconciler._queue_host_release(row, [], RELEASE, NOW)
    row["hosts"][0]["drain_fence_active"] = False
    await h.reconciler._queue_host_release(row, [], RELEASE, NOW + timedelta(minutes=20))
    row["hosts"][0]["drain_fence_active"] = True
    await h.reconciler._queue_host_release(row, [], RELEASE, NOW + timedelta(minutes=40))

    assert events == []


@pytest.mark.asyncio
async def test_a_fenced_host_already_on_the_release_is_not_reported(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation["observed_state"] = "ready"
    h.allocation["deployment_profile"].update(
        deployment_sha=RELEASE.sha, source_sha256=RELEASE.digest
    )
    h.allocation["hosts"][0].update(
        drain_fence_active=True, capabilities=[shared_deployment_capability(RELEASE.sha)]
    )
    events = _recorded_events(monkeypatch, h)
    row = h.allocation

    await h.reconciler._queue_host_release(row, [], RELEASE, NOW)
    await h.reconciler._queue_host_release(row, [], RELEASE, NOW + timedelta(hours=2))

    assert events == []


@pytest.mark.asyncio
async def test_incomplete_ready_cloudlab_scope_is_not_reported_as_usable(monkeypatch):
    h = harness(monkeypatch)
    resource_ref = ResourceRef(
        external_id="f47eb2a4-a0ac-4656-80da-8a638d2d263c",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "one-node",
            "profile_project": "FAST25-AE",
            "bindings": {"cluster": "utah", "phystype": "r6615"},
        },
    )
    submission_id = "d14b57a4-7e2b-4f49-8b4b-4ca3a9410a4d"
    observation_id = "a2cd14af-4c77-49c0-85cf-3a58e0dfbb2b"
    h.provider.acquire.return_value = ProviderReceipt(
        outcome="pending",
        resource=resource_ref,
        diagnostics={
            "provider_operation": "experiments.create",
            "http_status": 201,
            "provider_reason_code": "request_accepted",
            "provider_request_id": submission_id,
            "external_id": resource_ref.external_id,
        },
    )
    h.provider.inspect.return_value = observed().model_copy(
        update={
            "ref": resource_ref,
            "scope_complete": False,
            "provider_http_status": 200,
            "provider_request_id": observation_id,
        }
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert h.operation["step"] == "resource_identified"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "resource_scope_incomplete"
    diagnostic = h.operation["result"]["provider_diagnostic"]
    assert diagnostic["provider_operation"] == "experiments.read"
    assert diagnostic["provider_reason_code"] == "incomplete_resource_scope"
    assert diagnostic["provider_request_id"] == observation_id
    assert diagnostic["submission_request_id"] == submission_id
    assert diagnostic["submission_http_status"] == 201
    assert diagnostic["requested_project"] == "research"
    assert diagnostic["requested_site"] == "utah"
    assert diagnostic["requested_hardware"] == "r6615"
    assert public_operation(h.operation)["result"]["provider_diagnostic"] == diagnostic
    h.repository.prepare_bootstrap_binding.assert_not_awaited()
    h.bootstrap.deploy.assert_not_awaited()
    h.bootstrap.readiness.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_diagnostics_drop_untrusted_values_that_look_like_secrets(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.side_effect = ProviderError(
        "request_rejected",
        "CloudLab rejected the request",
        diagnostics={
            "provider_operation": "experiments.create",
            "http_status": 400,
            "provider_reason_code": "private-token-value",
            "provider_request_id": "private-token-value",
            "requested_hardware": "private-token-value",
        },
    )

    await h.reconciler.execute(h.operation)

    diagnostic = h.operation["result"]["provider_diagnostic"]
    assert diagnostic["provider_operation"] == "experiments.create"
    assert diagnostic["http_status"] == 400
    assert "provider_reason_code" not in diagnostic
    assert "provider_request_id" not in diagnostic
    assert "requested_hardware" not in diagnostic
    assert "private-token-value" not in str(h.operation["result"])


@pytest.mark.asyncio
async def test_provider_readiness_change_before_bootstrap_waits_then_deploys_once(monkeypatch):
    h = harness(monkeypatch)
    h.provider.inspect.side_effect = [
        observed(state="pending"),
        observed(state="pending"),
        observed(),
        observed(),
    ]

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.repository.prepare_bootstrap_binding.await_count == 0
    h.bootstrap.deploy.assert_not_awaited()

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.repository.prepare_bootstrap_binding.await_count == 0
    h.bootstrap.deploy.assert_not_awaited()

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.repository.prepare_bootstrap_binding.await_count == 1
    h.bootstrap.deploy.assert_awaited_once()
    assert h.provider.inspect.await_count == 4


@pytest.mark.asyncio
async def test_stale_provider_observation_cannot_authorize_bootstrap(monkeypatch):
    h = harness(monkeypatch)
    stale = observed().model_copy(update={"observed_at": NOW - timedelta(minutes=10)})
    h.provider.inspect.return_value = stale

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.operation["step"] == "waiting_for_resource"
    h.repository.observe_allocation.assert_awaited_once()
    h.repository.prepare_bootstrap_binding.assert_not_awaited()
    h.bootstrap.deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_acquisition_receipt_keeps_safe_provider_diagnostics(monkeypatch):
    h = harness(monkeypatch)
    h.provider.acquire.return_value = ProviderReceipt(
        outcome="unknown",
        diagnostics={
            "provider_operation": "experiments.create",
            "http_status": 503,
            "provider_reason_code": "provider_server_error",
            "requested_profile": "one-node",
        },
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "unknown"
    diagnostic = h.operation["result"]["provider_diagnostic"]
    assert diagnostic["provider_operation"] == "experiments.create"
    assert diagnostic["http_status"] == 503
    assert diagnostic["provider_reason_code"] == "provider_server_error"
    assert diagnostic["requested_profile"] == "one-node"
    assert diagnostic["operation_outcome"] == "unknown"
    assert public_operation(h.operation)["result"]["provider_diagnostic"] == diagnostic
    h.repository.prepare_bootstrap_binding.assert_not_awaited()
    h.bootstrap.deploy.assert_not_awaited()
    h.host_pool.issue_enroll_token.assert_not_awaited()
    await h.reconciler.execute(h.operation)
    h.provider.acquire.assert_awaited_once()
    h.repository.prepare_bootstrap_binding.assert_not_awaited()
    h.bootstrap.deploy.assert_not_awaited()
    h.host_pool.issue_enroll_token.assert_not_awaited()
    assert h.operation["state"] == "unknown"


def _move_out_hold(h):
    h.repository.set_move_out_hold.assert_awaited_once()
    return h.repository.set_move_out_hold.await_args.kwargs


@pytest.mark.asyncio
async def test_a_host_in_its_move_out_window_asks_to_hold_every_claim(monkeypatch):
    """Each chat's next turn is then claimed elsewhere, from its group's checkpoint.

    Whether the hold is set is the repository's decision: it needs working
    checkpoints and a Host that would take the groups.
    """
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.provider.inspect.return_value = observed(expires=NOW + timedelta(minutes=90))

    await h.reconciler.execute(h.operation)

    hold = _move_out_hold(h)
    assert (hold["allocation_id"], hold["due"]) == ("allocation-a", True)
    assert hold["operation_id"] == "operation-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        RenewalPolicy(move_out_before_seconds=0),
        RenewalPolicy(
            enabled=True, reason="Keep the sandboxes", rotation_enabled=True, node_types=["m510"]
        ),
    ],
)
async def test_a_host_that_does_not_move_out_never_holds_its_claims(monkeypatch, policy):
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.allocation["policy"] = policy.model_dump()
    h.provider.inspect.return_value = observed(expires=NOW + timedelta(minutes=30))

    await h.reconciler.execute(h.operation)

    assert _move_out_hold(h)["due"] is False


@pytest.mark.asyncio
async def test_renewal_is_tried_before_the_move_out_window_opens(monkeypatch):
    """A granted extension keeps the groups where they are."""
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.allocation.update(
        expires_at=NOW + timedelta(minutes=110),
        policy=RenewalPolicy(enabled=True, reason="Keep the sandboxes").model_dump(),
    )

    await h.reconciler.schedule()

    h.commands.extend.assert_awaited_once()


@pytest.mark.asyncio
async def test_groups_still_unmoved_near_the_lease_end_alert_once(monkeypatch):
    h = harness(monkeypatch, kind="inspect", identified=True)
    h.allocation["expires_at"] = NOW + timedelta(minutes=4)
    h.repository.unmoved_groups.return_value = 2
    events = []

    async def notify(event, *, host, reason):
        events.append((event, host, reason))

    monkeypatch.setattr(h.reconciler.rotation, "_notify", notify)

    await h.reconciler.schedule()
    await h.reconciler.schedule()

    assert events == [("compute_move_out_unfinished", "compute-nodeid", "2 groups not moved")]
