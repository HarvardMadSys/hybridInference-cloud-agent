"""Move a Host to a replacement node with the real worker, store and enrollment.

Provider and SSH effects are inert fakes that record what was asked of them;
everything the move decides — which node holds the Host's name, which claims
are paused, what is released — is read back from Postgres.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.integration.storage.test_host_pool_store import db as db, hosts as hosts, store as store

from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import ComputeConfiguration
from cloud_agent.compute_registry import ComputeProviderRegistration, ComputeProviderRegistry
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.host_pool import hash_secret
from cloud_agent.ports.external.compute_provider import (
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
    TransferResult,
)
from cloud_agent.schemas_compute import RenewalPolicy, RotateComputeRequest, UpdateComputePolicy
from cloud_agent.storage._sql.compute_admission import host_compute_admission
from cloud_agent.workers.compute_resources import ComputeResourceWorker

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

HOST_KEY = "ssh-ed25519 " + "A" * 68


def _profile(name: str) -> DeploymentProfile:
    return DeploymentProfile(
        id=name,
        label=name,
        source_bundle="/test/source.tar",
        source_sha256="a" * 64,
        deployment_sha="b" * 40,
        environment_ref="deployment:env",
        data_mount="/mydata",
    )


class FakeCloud:
    """Experiments keyed by ID; the node registers its key the moment it is created."""

    def __init__(self, repository) -> None:
        self.repository = repository
        self.experiments: dict[str, ResourceObservation] = {}
        self.failing_types: set[str] = set()
        self.created: list[str] = []
        self.released: list[str] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            acquisition_modes=("attach", "provision"), can_extend=True, can_release=True
        )

    def validate_config(self, mode, config):
        return dict(config)

    async def acquire(self, request, intent):
        external_id = str(uuid4())
        node_type = (request.config.get("bindings") or {}).get("phystype", "first")
        self.created.append(node_type)
        ref = ResourceRef(
            external_id=external_id, config={**request.config, "external_id": external_id}
        )
        if node_type in self.failing_types:
            observation = ResourceObservation(
                ref=ref,
                state="failed",
                raw_state="failed",
                observed_at=datetime.now(UTC),
                scope_complete=False,
            )
        else:
            node = NodeObservation(
                provider_node_id="node0",
                instance_identity=external_id + ":node0",
                display_name=f"{node_type}-node",
                connection=SshConnection(
                    address=f"{node_type}.example.org", username="agent", credential_ref="ssh"
                ),
            )
            observation = ResourceObservation(
                ref=ref,
                state="ready",
                raw_state="ready",
                observed_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=16),
                lease_known=True,
                nodes=(node,),
                scope_complete=True,
            )
            # The node's boot script registers its host key with the one-time token.
            assert request.registration is not None
            outcome = await self.repository.complete_node_registration(
                token_hash=hash_secret(request.registration.token.get_secret_value()),
                host_keys=[HOST_KEY],
            )
            assert outcome == "registered"
        self.experiments[external_id] = observation
        return ProviderReceipt(outcome="confirmed", resource=ref, observation=observation)

    async def inspect(self, ref):
        return self.experiments[ref.external_id]

    async def recover_acquire(self, intent):
        raise AssertionError("no acquisition is recovered in these tests")

    async def release(self, ref, intent):
        self.released.append(ref.external_id)
        gone = self.experiments[ref.external_id].model_copy(
            update={"state": "terminated", "raw_state": "terminated", "nodes": ()}
        )
        self.experiments[ref.external_id] = gone
        return ProviderReceipt(outcome="confirmed", resource=ref, observation=gone)

    async def extend(self, ref, request):
        raise AssertionError("no extension is requested in these tests")


class FakeNodes:
    """SSH effects: deployment enrolls the Host the way a real runner would."""

    def __init__(self, hosts) -> None:
        self.hosts = hosts
        self.calls: list[tuple[str, str]] = []
        self.fail_final_copy = False
        self.fail_start_on: set[str] = set()
        self.stopped: set[str] = set()

    async def preflight(self, node, profile):
        raise AssertionError("not used")

    async def deploy(self, node, profile, intent, token, *, prerequisites=None, reenroll=False):
        self.calls.append(("deploy-reenroll" if reenroll else "deploy", node.display_name))
        if node.display_name in self.fail_start_on:
            self.fail_start_on.discard(node.display_name)
            return BootstrapResult(
                ready=False,
                host=intent.host,
                deployment_sha=profile.deployment_sha,
                blocked_reasons=("host_deployment_failed",),
            )
        await self.hosts.register_host(
            host=intent.host,
            credential_hash=hash_secret("credential-" + node.display_name),
            slots_total=2,
        )
        await self.hosts.record_host_heartbeat(host=intent.host)
        self.stopped.discard(node.display_name)
        return BootstrapResult(ready=True, host=intent.host, deployment_sha=profile.deployment_sha)

    async def readiness(self, node, profile, intent):
        return BootstrapResult(
            ready=node.display_name not in self.stopped,
            host=intent.host,
            deployment_sha=profile.deployment_sha,
        )

    async def inventory(self, node, profile, intent):
        self.calls.append(("inventory", node.display_name))
        return HostInventory(complete=True)

    async def stage(self, node, profile, intent):
        self.calls.append(("stage", node.display_name))
        assert profile.data_mount == "/mydata"
        return BootstrapResult(ready=True, host=intent.host, deployment_sha=profile.deployment_sha)

    async def stop(self, node, profile, intent):
        self.calls.append(("stop", node.display_name))
        self.stopped.add(node.display_name)

    async def transfer(self, source, target, profile, intent, *, final, first=False):
        kind = "final" if final else ("first" if first else "again")
        self.calls.append((f"transfer-{kind}", f"{source.display_name}->{target.display_name}"))
        if final and self.fail_final_copy:
            self.fail_final_copy = False
            raise ProviderError("transfer_failed", "copy broke", retryable=True)
        return TransferResult(bytes_transferred=1024, seconds=1.5, streams=6)

    async def revoke_transfer(self, source, target):
        self.calls.append(("revoke", f"{source.display_name}->{target.display_name}"))

    async def wipe(self, node, profile, intent):
        self.calls.append(("wipe", node.display_name))


async def rig(store, hosts, monkeypatch):
    repository = store.compute_resources
    cloud = FakeCloud(repository)
    registry = ComputeProviderRegistry(
        [ComputeProviderRegistration(id="cloudlab", label="Fixture", provider=cloud, fields=())]
    )
    commands = ComputeCommands(
        repository=repository,
        registry=registry,
        secrets=ComputeSecretStore(repository, key="x" * 32),
        configuration=ComputeConfiguration(
            profiles={name: _profile(name) for name in ("slot-a", "slot-b")},
            registration_url="https://control.example.org/v1/compute/node-registrations",
        ),
    )
    nodes = FakeNodes(hosts)
    sandbox_manager = SimpleNamespace(
        health=AsyncMock(return_value={}), list_sandboxes=AsyncMock(return_value=[])
    )
    for module in ("compute_reconcile", "compute_rotation"):
        monkeypatch.setattr(
            f"cloud_agent.application.{module}.sandbox_manager_for_host",
            lambda host: sandbox_manager,
        )
    reconciler = ComputeReconciler(commands=commands, host_pool=hosts, bootstrap=nodes)
    request_id = str(uuid4())
    first = await repository.create_allocation_operation(
        request_id=request_id,
        fingerprint=request_id,
        kind="acquire",
        requested_by="admin",
        parameters={},
        allocation={
            "name": "lab",
            "provider_id": "cloudlab",
            "mode": "provision",
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
            "configuration": {
                "project": "lab",
                "host_key_source": "registration",
                "bindings": {"phystype": "first"},
            },
            "deployment_profile": _profile("slot-a").model_dump(mode="json"),
        },
    )
    r = SimpleNamespace(
        db=None,
        repo=repository,
        hosts=hosts,
        worker=ComputeResourceWorker(reconciler),
        commands=commands,
        cloud=cloud,
        nodes=nodes,
        allocation_id=first["allocation_id"],
    )
    return r


async def run_all(r, db, *, limit: int = 80) -> None:
    """Run due operations; only when none is due, let every wait elapse."""
    for _ in range(limit):
        claimed = await r.repo.claim_operation(worker_id="rotation-test")
        if claimed is None:
            async with db.acquire() as conn:
                waited = await conn.execute(
                    "UPDATE agent_compute_operations SET next_attempt_at=NOW() WHERE state='waiting'"
                )
            if waited == "UPDATE 0":
                return
            continue
        await r.worker.execute_claimed(claimed)
    raise AssertionError("operations did not settle")


async def ready_slot(r, db) -> tuple[dict, str]:
    """Bring the first server up and enable automatic moves for it."""
    await run_all(r, db)
    allocation = await r.repo.get_allocation(r.allocation_id)
    assert allocation["observed_state"] == "ready", allocation
    host = allocation["hosts"][0]["host"]
    policy = RenewalPolicy(
        enabled=True,
        reason="Keep the evaluation sandboxes",
        renew_before_seconds=3 * 3600,
        extend_to_horizon_seconds=8 * 3600,
        rotation_enabled=True,
        node_types=["t1", "t2"],
    )
    await r.commands.update_policy(
        r.allocation_id,
        UpdateComputePolicy(expected_revision=allocation["revision"], policy=policy),
    )
    return await r.repo.get_allocation(r.allocation_id), host


async def rotation_of(r, allocation_id: str) -> dict:
    rows = await r.repo.list_operations(allocation_id=allocation_id, kinds=["rotate"])
    assert rows, "no rotation"
    return rows[0]


async def successor_of(db, allocation_id: str) -> list[dict]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM agent_compute_allocations WHERE predecessor_id=$1 ORDER BY created_at",
            allocation_id,
        )
    return [dict(row) for row in rows]


async def test_host_moves_to_a_staged_replacement_and_the_old_node_is_released(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    source_node = source["nodes"][0]["id"]

    queued = await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    assert queued["kind"] == "rotate"
    await run_all(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    [replacement] = await successor_of(db, r.allocation_id)
    assert replacement["slot_host"] == host
    assert replacement["slot_namespace"] == "ca-" + source_node.replace("-", "").lower()
    target = await r.repo.get_allocation(replacement["id"])
    # The Host's name now belongs to the replacement node, enrolled again there.
    host_row = await hosts.get_host(host=host)
    assert host_row["enrolled"] and host_row["status"] == "online"
    assert [h["host"] for h in target["hosts"]] == [host]
    async with db.acquire() as conn:
        holds = await conn.fetch("SELECT * FROM agent_host_admission_holds WHERE host=$1", host)
        new_groups, claims = await host_compute_admission(conn, host)
    assert not [hold for hold in holds if hold["purpose"] == "rotation"]
    assert claims and new_groups
    # Order of effects: stage and copy while the old node serves, stop, final
    # copy, start on the new node, and only then clear the old node.
    assert r.nodes.calls == [
        ("deploy", "first-node"),
        ("stage", "t1-node"),
        ("transfer-first", "first-node->t1-node"),
        ("stop", "first-node"),
        ("transfer-final", "first-node->t1-node"),
        ("deploy", "t1-node"),
        ("revoke", "first-node->t1-node"),
        ("wipe", "first-node"),
        ("inventory", "first-node"),
    ]
    old = await r.repo.get_allocation(r.allocation_id)
    assert old["release_committed_at"] is not None and old["observed_state"] == "terminated"
    assert r.cloud.released == [old["external_id"]]
    assert target["release_committed_at"] is None


async def test_a_move_after_an_observation_reaches_each_node_by_its_pinned_key(
    db, store, hosts, monkeypatch
):
    """Observations never carry the key a node registered, so they must not drop it."""
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    unpinned = []

    def check(effect, *nodes):
        for node in nodes:
            if not (node.connection and node.connection.host_key == HOST_KEY):
                unpinned.append((effect, node.display_name))

    for effect in ("deploy", "readiness", "inventory", "stage", "stop", "wipe"):

        async def checked(node, *args, _effect=effect, _original=getattr(r.nodes, effect), **kw):
            check(_effect, node)
            return await _original(node, *args, **kw)

        monkeypatch.setattr(r.nodes, effect, checked)
    original_transfer = r.nodes.transfer

    async def transfer(source_node, target_node, *args, **kwargs):
        check("transfer", source_node, target_node)
        return await original_transfer(source_node, target_node, *args, **kwargs)

    monkeypatch.setattr(r.nodes, "transfer", transfer)
    # The scheduler observes every server periodically; observe the source once.
    await r.commands.queue_inspection(source, request_key="observe-source")
    await run_all(r, db)
    source = await r.repo.get_allocation(r.allocation_id)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    assert unpinned == []
    assert ("inventory", "first-node") in r.nodes.calls


async def test_autoscaling_ownership_blocks_rotation_even_with_a_compatible_spare(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state) VALUES ($1,'slot-a','active')""",
            r.allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ('slot-a',$1::jsonb,$2)""",
            json.dumps(_profile("slot-a").model_dump(mode="json")),
            r.allocation_id,
        )

    source = await r.repo.get_allocation(r.allocation_id)
    assert source["autoscaling_owned"] is True
    spare = await r.worker.reconciler.rotation._free_profile(exclude=source["deployment_profile"])
    assert spare is not None and spare.id == "slot-b"

    with pytest.raises(ComputeRefusal) as refused:
        await r.commands.update_policy(
            r.allocation_id,
            UpdateComputePolicy(expected_revision=source["revision"], policy=RenewalPolicy()),
        )
    assert refused.value.code == "autoscaling_managed"

    with pytest.raises(ComputeRefusal) as refused:
        await r.commands.rotate(
            r.allocation_id,
            RotateComputeRequest(request_id="owned-move", expected_revision=source["revision"]),
            actor="admin",
        )
    assert refused.value.code == "autoscaling_managed"
    assert not await r.repo.list_operations(allocation_id=r.allocation_id, kinds=["rotate"])

    parameters = {"allocation_id": r.allocation_id, "trigger": "manual"}
    await r.repo.create_operation(
        request_id="legacy-owned-move",
        fingerprint="legacy-owned-move",
        kind="rotate",
        allocation_id=r.allocation_id,
        parameters=parameters,
        requested_by="admin",
    )
    await run_all(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "blocked"
    assert rotation["result"]["blocked_reasons"][0]["code"] == "autoscaling_managed"
    assert await successor_of(db, r.allocation_id) == []
    assert r.cloud.created == ["first"]


async def test_failed_node_type_is_released_and_the_next_type_takes_over(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    r.cloud.failing_types = {"t1"}
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    await run_all(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    [attempt] = rotation["result"]["attempts"]
    assert attempt["node_type"] == "t1" and attempt["class"] == "no_capacity"
    failed, replacement = await successor_of(db, r.allocation_id)
    assert failed["release_committed_at"] is not None
    assert failed["observed_state"] == "terminated"
    assert r.cloud.created == ["first", "t1", "t2"]
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [host]


async def test_a_replacement_known_only_to_its_acquisition_is_still_released(
    db, store, hosts, monkeypatch
):
    """The provider answered its create, then could not show the experiment.

    The move gives that node up for the next type. Releasing it deletes the
    experiment its acquisition was given, though no observation recorded it.
    """
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    acquire, inspect = r.cloud.acquire, r.cloud.inspect
    unobserved: list[str] = []
    missed: list[str] = []

    async def acquire_without_observation(request, intent):
        receipt = await acquire(request, intent)
        if request.config["bindings"]["phystype"] != "t1":
            return receipt
        unobserved.append(receipt.resource.external_id)
        return ProviderReceipt(outcome="pending", resource=receipt.resource)

    async def inspect_after_one_miss(ref):
        if ref.external_id in unobserved and ref.external_id not in missed:
            missed.append(ref.external_id)
            raise ProviderError("resource_visibility_unknown", "not visible yet")
        return await inspect(ref)

    monkeypatch.setattr(r.cloud, "acquire", acquire_without_observation)
    monkeypatch.setattr(r.cloud, "inspect", inspect_after_one_miss)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    await run_all(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    assert r.cloud.created == ["first", "t1", "t2"]
    assert unobserved[0] in r.cloud.released
    abandoned, replacement = await successor_of(db, r.allocation_id)
    assert (abandoned["external_id"], abandoned["observed_state"]) == (unobserved[0], "terminated")
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [host]


async def test_a_failed_switch_rolls_back_to_the_original_node_first(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    r.nodes.fail_final_copy = True
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    await run_all(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    [attempt] = rotation["result"]["attempts"]
    assert attempt == {**attempt, "node_type": "t1", "class": "switch_failed"}
    calls = r.nodes.calls
    # The original node restarted with its own credential before anything else.
    rollback = calls.index(("deploy", "first-node"), 1)
    assert calls[rollback - 1] == ("transfer-final", "first-node->t1-node")
    assert calls[rollback + 1 :].count(("stage", "t2-node")) == 1
    failed, replacement = await successor_of(db, r.allocation_id)
    assert failed["release_committed_at"] is not None
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [host]


async def test_only_one_move_of_a_host_runs_at_a_time(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="first", expected_revision=source["revision"]),
        actor="admin",
    )
    from cloud_agent.domain.compute import ComputeRefusal

    with pytest.raises(ComputeRefusal) as refused:
        await r.commands.rotate(
            r.allocation_id,
            RotateComputeRequest(request_id="second", expected_revision=source["revision"]),
            actor="admin",
        )
    assert refused.value.code == "rotation_in_progress"


async def test_a_failed_start_hands_the_name_back_and_enrolls_the_original_again(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    r.nodes.fail_start_on = {"t1-node"}
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    await run_all(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    [attempt] = rotation["result"]["attempts"]
    assert (attempt["node_type"], attempt["class"]) == ("t1", "takeover_failed")
    calls = r.nodes.calls
    failed_start = calls.index(("deploy", "t1-node"))
    # The replacement is stopped and the original enrolls again with a fresh
    # token, because the replacement may already have taken the credential.
    assert calls[failed_start + 1 : failed_start + 3] == [
        ("stop", "t1-node"),
        ("deploy-reenroll", "first-node"),
    ]
    failed, replacement = await successor_of(db, r.allocation_id)
    assert failed["release_committed_at"] is not None
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [host]


async def test_running_turns_delay_the_switch_until_they_finish(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    running = [2, 1, 1, 0, 0]
    seen = []

    async def running_attempts(*, host):
        seen.append(running[0] if len(running) == 1 else running.pop(0))
        return seen[-1]

    monkeypatch.setattr(r.repo, "running_attempts", running_attempts)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    stopped_while_busy = []
    original_stop = r.nodes.stop

    async def stop(node, profile, intent):
        stopped_while_busy.append(seen[-1])
        await original_stop(node, profile, intent)

    r.nodes.stop = stop
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    assert stopped_while_busy == [0]
    assert seen[:3] == [2, 1, 1]


async def test_a_busy_host_is_paused_at_the_deadline_and_then_drained(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    # Two hours left: a whole turn, the switch and a margin no longer fit, so
    # claims pause at once while the running turn finishes.
    r.cloud.experiments[source["external_id"]] = r.cloud.experiments[
        source["external_id"]
    ].model_copy(update={"expires_at": datetime.now(UTC) + timedelta(hours=1, minutes=30)})
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET expires_at=NOW()+interval '90 minutes' WHERE id=$1",
            r.allocation_id,
        )
    running = [1, 1, 1, 0]
    held_while_running = []

    async def running_attempts(*, host):
        value = running.pop(0) if len(running) > 1 else running[0]
        if value:
            async with db.acquire() as conn:
                held_while_running.append(
                    await conn.fetchval(
                        "SELECT count(*) FROM agent_host_admission_holds WHERE host=$1 AND purpose='rotation'",
                        host,
                    )
                )
        return value

    monkeypatch.setattr(r.repo, "running_attempts", running_attempts)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    await run_all(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    # The deadline pauses claims before even preparing the replacement, so
    # all checks of running turns see the pause in place.
    assert held_while_running and set(held_while_running) == {1}
    assert "compute_rotation_lead_time_short" in rotation["result"]["alerts"]


async def test_store_guards_the_host_name_and_the_candidate_release(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, host = await ready_slot(r, db)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    # Run until the replacement is staged and the old node is about to stop.
    original_stop = r.nodes.stop

    async def refuse_stop(node, profile, intent):
        raise AssertionError("stop is not reached in this test")

    r.nodes.stop = refuse_stop
    for _ in range(10):
        claimed = await r.repo.claim_operation(worker_id="rotation-test")
        if claimed is None:
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_compute_operations SET next_attempt_at=NOW() WHERE state='waiting'"
                )
            continue
        if claimed["kind"] == "rotate" and claimed["step"] in {"draining", "waiting_for_idle"}:
            break
        if claimed["kind"] == "rotate" and claimed["result"].get("candidate"):
            candidate = await r.repo.get_allocation(claimed["result"]["candidate"]["id"])
            if candidate["observed_state"] == "ready" and candidate["nodes"]:
                break
        await r.worker.execute_claimed(claimed)
    r.nodes.stop = original_stop
    rotation = await rotation_of(r, r.allocation_id)
    [replacement] = await successor_of(db, r.allocation_id)
    target = await r.repo.get_allocation(replacement["id"])
    fence = {
        "operation_id": claimed["id"],
        "generation": claimed["generation"],
        "worker_id": "rotation-test",
    }

    # Only the rotation's own successor may take the name; nobody else's node.
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_compute_allocations (id,name,provider_id,mode)
            VALUES ('stranger','stranger','cloudlab','provision')"""
        )
        await conn.execute(
            """INSERT INTO agent_compute_nodes (id,allocation_id,provider_node_id,instance_identity,name)
            VALUES ('stranger-node','stranger','node0','stranger:node0','stranger')"""
        )
    with pytest.raises(ValueError, match="host_identity_conflict"):
        await r.repo.rebind_slot_host(
            host=host,
            from_node_id=source["nodes"][0]["id"],
            to_node_id="stranger-node",
            **fence,
        )
    # Hand it over, then the candidate can no longer be released without guards.
    await r.repo.rebind_slot_host(
        host=host,
        from_node_id=source["nodes"][0]["id"],
        to_node_id=target["nodes"][0]["id"],
        **fence,
    )
    discard = await r.repo.create_operation(
        request_id="discard-check",
        fingerprint="discard-check",
        kind="discard",
        allocation_id=replacement["id"],
        parameters={},
        requested_by="test",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state='running',worker_id='w',generation=7,lease_expires_at=NOW()+interval '1 minute' WHERE id=$1",
            discard["id"],
        )
    with pytest.raises(ValueError, match="candidate_bound"):
        await r.repo.commit_candidate_release(
            allocation_id=replacement["id"], operation_id=discard["id"], generation=7, worker_id="w"
        )
    # The operation's own allocation is fenced; an original node is no candidate.
    own = await r.repo.create_operation(
        request_id="discard-original",
        fingerprint="discard-original",
        kind="discard",
        allocation_id=r.allocation_id,
        parameters={},
        requested_by="test",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state='running',worker_id='w',generation=7,lease_expires_at=NOW()+interval '1 minute' WHERE id=$1",
            own["id"],
        )
    with pytest.raises(ValueError, match="operation_lease_lost"):
        await r.repo.commit_candidate_release(
            allocation_id=r.allocation_id, operation_id=discard["id"], generation=7, worker_id="w"
        )
    with pytest.raises(ValueError, match="not_a_candidate"):
        await r.repo.commit_candidate_release(
            allocation_id=r.allocation_id, operation_id=own["id"], generation=7, worker_id="w"
        )
    # Hand the name back (the rollback direction) so cancelling is possible.
    await r.repo.rebind_slot_host(
        host=host,
        from_node_id=target["nodes"][0]["id"],
        to_node_id=source["nodes"][0]["id"],
        **fence,
    )
    await r.repo.finish_operation(
        **fence, state="waiting", step="draining", message="", retry_after_seconds=60
    )
    # A candidate with work in flight cannot be abandoned safely.
    with pytest.raises(ValueError, match="operation_in_flight"):
        await r.repo.cancel_operation(rotation["id"])
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state='cancelled' WHERE request_id LIKE 'discard-%'"
        )
    cancelled = await r.repo.cancel_operation(rotation["id"])
    assert cancelled["state"] == "cancelled"
    async with db.acquire() as conn:
        holds = await conn.fetchval(
            "SELECT count(*) FROM agent_host_admission_holds WHERE purpose='rotation'"
        )
        queued = await conn.fetchval(
            "SELECT count(*) FROM agent_compute_operations WHERE kind='discard' AND allocation_id=$1 AND request_id=$2",
            replacement["id"],
            "discard:" + replacement["id"],
        )
    assert holds == 0 and queued == 1


async def test_a_move_cannot_be_cancelled_once_the_old_node_stopped(db, store, hosts, monkeypatch):
    r = await rig(store, hosts, monkeypatch)
    source, _ = await ready_slot(r, db)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )
    rotation = await rotation_of(r, r.allocation_id)
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_operations SET state='waiting',
               result=result || '{"source_stopped": true}'::jsonb WHERE id=$1""",
            rotation["id"],
        )
    with pytest.raises(ValueError, match="rotation_switching"):
        await r.repo.cancel_operation(rotation["id"])
