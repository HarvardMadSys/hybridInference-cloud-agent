"""Checks, on Docker's own answers, that a group bridge and its peers belong to one chat."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent_host.container.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_NETWORK_GENERATION_LABEL,
    CHAT_NETWORK_LABEL,
    CHAT_NETWORK_SANDBOX_ID_LABEL,
    CHAT_NETWORK_SERVICES_LABEL,
)
from cloud_agent_host.container.sandbox import SandboxError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_host.container.chat_sandbox_models import ChatSandboxSpec


def owned_bridge_endpoints(
    network: Mapping[str, Any], *, namespace: str, spec: ChatSandboxSpec
) -> dict[str, Any]:
    """Return the endpoints of a bridge this chat generation owns, refusing any other."""
    labels = network.get("Labels") or {}
    if (
        network.get("Driver") != "bridge"
        or network.get("Internal") is not False
        or labels.get(CHAT_NETWORK_LABEL) != "true"
        or labels.get(CHAT_NAMESPACE_LABEL) != namespace
        or labels.get(CHAT_NETWORK_SANDBOX_ID_LABEL) != spec.sandbox_id
        or labels.get(CHAT_NETWORK_GENERATION_LABEL) != str(spec.generation)
    ):
        raise SandboxError("refusing to remove a network outside this chat/generation")
    containers = network.get("Containers") or {}
    if not isinstance(containers, dict):
        raise SandboxError("Docker returned invalid group-network endpoints")
    return containers


def require_service_peers(
    network_name: str,
    labels: Mapping[str, Any],
    peers: list[str],
    peer_aliases: Mapping[str, set[str]],
) -> None:
    """Refuse a bridge peer that answers to none of the relay aliases the bridge records."""
    recorded_aliases = {
        value.strip().lower().rstrip(".")
        for value in str(labels.get(CHAT_NETWORK_SERVICES_LABEL) or "").split(",")
        if value.strip()
    }
    for container_id in peers:
        endpoint_aliases = peer_aliases.get(container_id)
        if not endpoint_aliases or not endpoint_aliases.intersection(recorded_aliases):
            raise SandboxError(f"group network {network_name!r} contains an unexpected peer")


__all__ = ["owned_bridge_endpoints", "require_service_peers"]
