"""Retire a server: wait out its dependencies, prove its Hosts empty, commit the release.

Split from :mod:`cloud_agent.application.compute_reconcile`. This is the local
half of a retirement: the retention checks, the removal of containers moved
groups left behind, the inventory proof and the durable release commit. Once
that commit stands, :class:`~cloud_agent.application.compute_release.ComputeRelease`
releases the provider resource.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.application.compute_acquisition import registers_host_keys
from cloud_agent.application.compute_release import recorded_evidence, release_evidence
from cloud_agent.application.release_inventory import quiesce_and_scan
from cloud_agent.application.superseded_removal import superseded_refusal
from cloud_agent.domain.compute import provider_resource_absent
from cloud_agent.logging_utils import get_logger
from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.sandbox_manager.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager.sandbox_manager_routing import sandbox_manager_for_host

if TYPE_CHECKING:
    from cloud_agent.application.compute_reconcile import ComputeReconciler

# The reconciler's own logger: these events kept their name when the step moved.
logger = get_logger("cloud_agent.application.compute_reconcile")


class ComputeRetirement:
    """The retire workflow, executed by the compute reconciler."""

    def __init__(self, reconciler: ComputeReconciler) -> None:
        self.reconciler = reconciler
        self.commands = reconciler.commands
        self.repository = reconciler.repository
        self.bootstrap = reconciler.bootstrap

    async def settle_terminal_retirement(
        self, operation: dict[str, Any], allocation: dict[str, Any], evidence: dict[str, Any] | None
    ) -> None:
        """Commit local fencing after the provider is already terminal."""
        await self.repository.commit_release(
            allocation_id=allocation["id"], **self.reconciler._fence(operation)
        )
        await self.reconciler.release.released(operation, evidence)

    async def retire(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Commit the release once nothing depends on the server, then release its resource."""
        if not allocation.get("release_committed_at"):
            if allocation.get("desired_state") == "absent" and allocation.get("observed_state") in {
                "terminated",
                "absent",
            }:
                await self.settle_terminal_retirement(
                    operation, allocation, recorded_evidence(operation, allocation)
                )
                return
            self.commands._require_managed(allocation, "release")
            await self.repository.begin_retirement(
                allocation_id=allocation["id"], **self.reconciler._fence(operation)
            )
            allocation = await self.reconciler._allocation(allocation["id"])
            await self.reconciler._checkpoint(
                operation, "checking_retention", {"external_pending": False}
            )
            if allocation.get("external_id"):
                observation = await self.reconciler._inspect(operation, allocation)
                if observation.state == "terminated":
                    await self.settle_terminal_retirement(
                        operation, allocation, release_evidence(operation, observation)
                    )
                    return
                allocation = await self.reconciler._allocation(allocation["id"])
            impact = await self.repository.retirement_impact(allocation["id"])
            meaningful = [
                {"code": code, "message": code.replace("_", " ")}
                for code in impact["blocked_reasons"]
                if code not in {"inventory_unverified", "host_observation_stale"}
            ]
            if meaningful:
                await self.reconciler._finish(
                    operation,
                    "waiting",
                    "waiting_for_retention",
                    "Resource release is waiting for its dependencies to be cleared.",
                    result={"blocked_reasons": meaningful, "external_pending": False},
                    retry=30,
                )
                return
            # An empty node list cannot pass the inventory scan: that scan
            # refuses to call zero nodes "empty". There is no machine to scan.
            if not provider_resource_absent(allocation):
                await self.verify_empty_inventory(operation, allocation)
            committed = await self.repository.commit_release(
                allocation_id=allocation["id"], **self.reconciler._fence(operation)
            )
            if committed.get("blocked_reasons"):
                reasons = [
                    {"code": code, "message": code.replace("_", " ")}
                    for code in committed["blocked_reasons"]
                ]
                await self.reconciler._finish(
                    operation,
                    "waiting",
                    "waiting_for_retention",
                    "Resource dependencies changed during the release checks.",
                    result={"blocked_reasons": reasons, "external_pending": False},
                    retry=30,
                )
                return
            allocation = await self.reconciler._allocation(allocation["id"])
        if provider_resource_absent(allocation):
            await self.reconciler.release.absent_resource(operation, allocation)
            return
        await self.reconciler.release.committed(operation, allocation)

    async def remove_superseded(
        self, operation: dict[str, Any], host: dict[str, Any], sandbox_manager: Any
    ) -> list[dict[str, Any]]:
        """Remove the containers this Host's groups left behind; return what it still holds.

        A group handed over by its checkpoint, or restored elsewhere since,
        leaves its stopped container here, and release refuses a Host that
        holds one. Only those the control plane proves superseded go
        (``SUPERSEDED_CONTAINER_SQL``), whether or not the group's chats ever
        run again, and only once that proof is recorded under the group's row
        lock (``authorize_superseded_removals``): a group that became ``lost``
        first keeps its container. Each goes by its exact id through the
        Sandbox Manager's discard, which requires it stopped and idle under
        the reported generation, and records the request before Docker
        removes it. A retry sends what was authorized under the same request
        id, and one that finds the container gone completes. So does a removal
        the Host carried out but whose answer was lost: the container no
        longer shows, and its receipt is fetched again before release goes on.
        """
        held = await sandbox_manager.list_sandboxes()
        stopped = [
            row
            for row in held
            if row.get("running") is False
            and type(row.get("generation")) is int
            and all(
                isinstance(row.get(key), str) and row[key]
                for key in ("sandbox_id", "container_id", "origin_thread_id")
            )
        ]
        superseded = await self.repository.authorize_superseded_removals(
            host=str(host["host"]), containers=stopped, operation_id=str(operation["id"])
        )
        if not superseded:
            return held
        refusals: list[ProviderError] = []
        for container in superseded:
            request_id = container["request_id"]
            try:
                receipt = await sandbox_manager.discard_sandbox_deletion(
                    container["origin_thread_id"],
                    {
                        "request_id": request_id,
                        "generation": container["generation"],
                        "expected_container_id": container["container_id"],
                        "sandbox_id": container["sandbox_id"],
                    },
                )
            except SandboxManagerError as exc:
                refusals.append(
                    superseded_refusal(host, container, exc, operation_id=str(operation["id"]))
                )
                continue
            if (
                receipt.get("deleted") is not True
                or receipt.get("request_id") != request_id
                or receipt.get("container_id") != container["container_id"]
            ):
                refusals.append(
                    ProviderError(
                        "superseded_container_not_removed",
                        "The Host did not confirm removing a container its groups left behind",
                    )
                )
                continue
            await self.repository.record_superseded_removal(request_id=request_id)
            logger.info(
                "superseded_container_removed",
                extra={
                    "event": "superseded_container_removed",
                    "host": host["host"],
                    "sandbox_id": container["sandbox_id"],
                    "generation": container["generation"],
                },
            )
        if refusals:
            # One that waits for an administrator outranks one that may pass.
            raise next((item for item in refusals if not item.retryable), refusals[0])
        return await sandbox_manager.list_sandboxes()

    async def count_superseded(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Report how many left-behind containers this machine's Hosts confirmed removed.

        Read from the durable record, so a pass that stopped after some
        removals were confirmed loses none of them.
        """
        hosts = [str(host["host"]) for host in allocation.get("hosts", [])]
        removed = await self.repository.superseded_removals(hosts=hosts)
        if removed and operation.get("result", {}).get("superseded_removed") != removed:
            await self.reconciler._checkpoint(
                operation, "checking_retention", {"superseded_removed": removed}
            )

    async def verify_empty_inventory(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> None:
        """Prove every Host and node of this allocation empty, or refuse the release."""
        if not allocation.get("deployment_profile"):
            raise ProviderError(
                "inventory_unavailable",
                "Attach a deployment profile and configured SSH connection before releasing this resource",
            )
        profile = DeploymentProfile.model_validate(allocation["deployment_profile"])
        for host in allocation.get("hosts", []):
            sandbox_manager = sandbox_manager_for_host(host)
            if sandbox_manager is None:
                continue  # A trusted full-node scan below must prove this host empty.
            try:
                retained = await self.remove_superseded(operation, host, sandbox_manager)
            except SandboxManagerError:
                continue  # Never infer emptiness; require the full-node scan below.
            finally:
                await self.count_superseded(operation, allocation)
            if retained:
                raise ProviderError(
                    "retained_groups_present",
                    "The sandbox manager still reports retained sandbox state",
                )
        # This control plane reaches a registering node only through the key
        # its registration supplied, and binds a Host only after pinning it.
        # A node that never registered, with no key and no Host, never ran
        # anything of ours, and without a trusted key it cannot be scanned.
        never_registered = False
        if registers_host_keys(allocation):
            registration = await self.repository.node_registration(allocation_id=allocation["id"])
            never_registered = registration is None or not registration.get("registered_at")
        bound = {host.get("compute_node_id") for host in allocation.get("hosts", [])}
        scans: list[dict[str, Any]] = []
        for node in allocation["nodes"]:
            if (
                never_registered
                and not (node.get("connection") or {}).get("host_key")
                and node["id"] not in bound
            ):
                continue
            await quiesce_and_scan(
                self.bootstrap,
                lambda host: self.repository.recorded_host_state(host=host),
                lambda result: self.reconciler._checkpoint(operation, operation["step"], result),
                operation,
                self.reconciler._node(node),
                node["id"],
                profile,
                self.reconciler._bootstrap_intent(operation, allocation, node),
                scans,
            )
        recorded = await self.repository.record_inventory(
            allocation_id=allocation["id"],
            hosts=[h["host"] for h in allocation.get("hosts", [])],
            verified_node_ids=[node["id"] for node in allocation["nodes"]],
            empty=True,
            **self.reconciler._fence(operation),
        )
        if not recorded:
            raise ValueError("inventory_changed")
