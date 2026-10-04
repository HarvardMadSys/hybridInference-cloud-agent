"""Read-only server inventory and lifecycle capabilities from database snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.schemas_compute import RenewalPolicy

if TYPE_CHECKING:
    from cloud_agent.compute_registry import ComputeProviderRegistry
    from cloud_agent.compute_secrets import ComputeSecretStore
    from cloud_agent.ports.compute_resources import ComputeResourceRepository


class ServerQueries:
    """Compose resource and Host observations without contacting any provider."""

    def __init__(
        self,
        repository: ComputeResourceRepository,
        registry: ComputeProviderRegistry,
        secrets: ComputeSecretStore,
        *,
        observation_seconds: int,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.secrets = secrets
        self.observation_seconds = observation_seconds

    async def servers(self) -> dict[str, Any]:
        """Read inventory from one database snapshot; never refresh cloud APIs here."""
        inventory = await self.repository.server_inventory()
        inventory["allocations"] = [self.allocation_view(row) for row in inventory["allocations"]]
        return inventory

    def allocation_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """Project supported actions and distinguish current resource observations."""
        entry = self.registry.entries.get(row["provider_id"])
        caps = entry.provider.capabilities() if entry else None
        reasons: list[dict[str, str]] = []
        actions: list[str] = []
        terminal = row.get("observed_state") in {"terminated", "absent"} or bool(
            row.get("release_committed_at")
        )
        if not entry:
            reasons.append(
                {"code": "provider_unavailable", "message": "Provider is not configured."}
            )
        if row.get("observation_error"):
            reasons.append(
                {"code": "observation_stale", "message": "Resource observation needs refresh."}
            )
        if not terminal and caps:
            if caps.can_extend:
                actions.append("policy")
            if row.get("management_mode") == "manage" and self.secrets.configured:
                if row.get("external_id") and caps.can_extend:
                    actions.append("extend")
                if caps.can_release and row.get("deployment_profile"):
                    actions.append("retire")
                elif caps.can_release:
                    reasons.append(
                        {
                            "code": "inventory_unavailable",
                            "message": "Attach a deployment profile and verified SSH connection before release.",
                        }
                    )
            else:
                reasons.append(
                    {"code": "unmanaged_scope", "message": "Resource lifecycle is not managed."}
                )
        hosts = row.get("hosts") or []
        nodes = [
            {
                "id": node["id"],
                "name": node["name"],
                "address": (node.get("connection") or {}).get("address"),
                "port": (node.get("connection") or {}).get("port"),
                "username": (node.get("connection") or {}).get("username"),
                "hosts": [
                    host["host"] for host in hosts if host.get("compute_node_id") == node["id"]
                ],
            }
            for node in row.get("nodes", [])
        ]
        return {
            "id": row["id"],
            "name": row["name"],
            "provider_id": row["provider_id"],
            # IDs link the admin-only credential editor; transport secrets and
            # the rest of provider configuration remain private.
            "connection_id": row.get("connection_id"),
            "ssh_connection_id": row.get("ssh_connection_id"),
            "state": row.get("observed_state", "unknown"),
            "external_id": row.get("external_id"),
            "expires_at": row.get("expires_at"),
            "expiration_known": row.get("expiration_known", False),
            "observed_at": row.get("observed_at"),
            "check_interval_seconds": (
                self.observation_seconds
                if not terminal and row.get("external_id") and entry and self.secrets.configured
                else None
            ),
            "policy": RenewalPolicy.model_validate(row.get("policy") or {}).model_dump(),
            "revision": row["revision"],
            "management_mode": row.get("management_mode", "observe"),
            "desired_state": row.get("desired_state", "present"),
            "allowed_actions": actions,
            "blocked_reasons": reasons,
            "nodes": nodes,
        }

    async def retirement_impact(self, allocation_id: str) -> dict[str, Any]:
        """Display the full affected scope without connection or credential metadata."""
        impact = await self.repository.retirement_impact(allocation_id)
        entry = self.registry.entries.get(impact.get("provider_id", ""))
        can_retire = (
            entry is not None
            and entry.provider.capabilities().can_release
            and impact.get("management_mode") == "manage"
            and impact.get("deployment_profile_configured")
            and not impact.get("release_committed")
            and self.secrets.configured
        )
        nodes = [
            {
                "id": node["id"],
                "name": node["name"],
                "hosts": [
                    host["host"]
                    for host in impact["hosts"]
                    if host.get("compute_node_id") == node["id"]
                ],
            }
            for node in impact["nodes"]
        ]
        return {
            "allocation_id": allocation_id,
            "revision": impact["revision"],
            "nodes": nodes,
            "hosts": [host["host"] for host in impact["hosts"]],
            "active_attempts": impact["active_attempts"],
            "retained_groups": len(impact["retained_groups"]),
            "global_active_host": impact["global_active_host"],
            "allowed_actions": ["retire"] if can_retire else [],
            "blocked_reasons": [
                {"code": code, "message": code.replace("_", " ")}
                for code in impact["blocked_reasons"]
            ],
        }
