"""Stop a released node's runner, read what the node still holds, and decide.

The release workflow (:mod:`cloud_agent.application.compute_reconcile`) runs
:func:`quiesce_and_scan` for each node it must prove empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.ports.external.host_bootstrap import RecordedHostState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloud_agent.ports.external.compute_provider import NodeObservation
    from cloud_agent.ports.external.host_bootstrap import (
        BootstrapIntent,
        DeploymentProfile,
        HostBootstrap,
        HostInventory,
    )

#: Set before the first runner is stopped, and never cleared. From then on the
#: release cannot be cancelled: cancelling reopens the Host to new work, and
#: nothing would start its runner again. Set first, so a stop whose answer was
#: lost still counts.
RUNNERS_STOPPING = "runners_stopping"


async def quiesce_and_scan(
    bootstrap: HostBootstrap,
    recorded_host_state: Callable[[str], Awaitable[dict[str, Any]]],
    checkpoint: Callable[[dict[str, Any]], Awaitable[None]],
    operation: dict[str, Any],
    node: NodeObservation,
    node_id: str,
    profile: DeploymentProfile,
    intent: BootstrapIntent,
    scans: list[dict[str, Any]],
) -> None:
    """Stop the node's runner, scan it, keep the scan, and refuse unless it is empty."""
    if not (operation.get("result") or {}).get(RUNNERS_STOPPING):
        await checkpoint({RUNNERS_STOPPING: True})
    # A runner left to its restart policy keeps starting and creating
    # self-test sandboxes, which the inventory would count.
    probes_removed = await bootstrap.quiesce(node, profile, intent)
    # Only receipts of what the control plane recorded are not retained state.
    receipts = RecordedHostState.model_validate(await recorded_host_state(intent.host))
    proof = await bootstrap.inventory(node, profile, intent, receipts)
    scans.append(scan_record(node_id, probes_removed, proof))
    # Kept with the operation, so a refusal says which node refused and why.
    await checkpoint({"inventory": scans})
    require_empty(proof)


def scan_record(node_id: str, probes_removed: int, proof: HostInventory) -> dict[str, Any]:
    """One node's scan as the operation keeps it, so a refusal says who refused and why."""
    return {
        "node_id": node_id,
        "probes_removed": probes_removed,
        "complete": proof.complete,
        "retained_containers": proof.retained_containers,
        "unknown_containers": proof.unknown_containers,
        "orphan_state": proof.orphan_state,
        "blocked_reasons": list(proof.blocked_reasons),
    }


def require_empty(proof: HostInventory) -> None:
    """Refuse release while the node holds anything, or could not be read."""
    if (
        proof.complete
        and not proof.retained_containers
        and not proof.unknown_containers
        and not proof.orphan_state
    ):
        return
    raise ProviderError(
        "inventory_not_empty",
        "Stopped containers, unrecognized containers, or retained files prevent release",
        # Only a read that failed, with nothing found, is worth repeating.
        retryable=proof.blocked_reasons == ("host_inventory_unavailable",),
    )
