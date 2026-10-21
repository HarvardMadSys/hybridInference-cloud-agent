"""What one released node's machine inventory showed, and whether release may go on.

The release workflow (:mod:`cloud_agent.application.compute_reconcile`) scans
each node after stopping its runner. These are the two decisions it makes on
a scan, kept apart so they can be read and tested without the workflow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.ports.external.compute_provider import ProviderError

if TYPE_CHECKING:
    from cloud_agent.ports.external.host_bootstrap import HostInventory


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
