"""Bounded Docker endpoint removal shared by group network cleanup paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import RetainedChatLost
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec
    from cloud_agent_host.chat_sandbox_network import ChatSandboxNetwork


def disconnect_endpoint(
    owner: ChatSandboxNetwork,
    network: str,
    container_id: str,
    *,
    network_name: str | None = None,
) -> None:
    """Disconnect one endpoint, allowing an already-removed immutable network."""
    disconnected = owner._containers._run(
        [owner.backend.docker_binary, "network", "disconnect", "--force", network, container_id],
        timeout=30,
    )
    if disconnected.returncode == 0:
        return
    if owner._inspect_network(network) is None:
        # An id-pinned cleanup can lose to another process that already
        # removed the old bridge. A same-name replacement is unrelated.
        return
    aliases = owner._container_network_aliases(container_id, network_name or network)
    if aliases is not None:
        raise SandboxError(
            f"container {container_id[:12]} could not leave network "
            f"{(network_name or network)!r}: " + disconnected.stderr.strip()[:300]
        )


def stopped_network_record(
    owner: ChatSandboxNetwork,
    spec: ChatSandboxSpec,
    network_name: str,
    network_id: str | None,
    container_id: str,
) -> bool:
    """Prove one exact stopped container still holds the expected network intent."""
    current = owner._containers._require_exact(spec, container_id)
    if current.running:
        raise SandboxError("chat sandbox restarted during network release")
    named = owner._containers.inspect(spec.container_name)
    if named is not None and named.container_id != container_id:
        raise RetainedChatLost("sandbox identity changed during network release")
    if network_name not in current.networks:
        return False
    if not network_id or dict(current.network_ids).get(network_name) != network_id:
        raise SandboxError("stopped chat sandbox network identity changed during release")
    return True


def forget_stopped_network(
    owner: ChatSandboxNetwork,
    spec: ChatSandboxSpec,
    network_name: str,
    network_id: str | None,
    container_id: str,
) -> None:
    """Clear only exact stopped-container intent for an absent network.

    Docker's stopped-container branch removes a saved setting by name, even
    after its network is gone; an absent network ID cannot address that key.
    Do not resolve a same-name replacement into authority to alter it.
    """
    if not stopped_network_record(owner, spec, network_name, network_id, container_id):
        return
    if owner._inspect_network(network_name) is not None:
        raise SandboxError("group network reappeared during stopped intent cleanup")
    owner._disconnect_endpoint(network_name, container_id)
    released = owner._containers._require_exact(spec, container_id)
    if released.running:
        raise SandboxError("chat sandbox restarted during network release")
    if network_name in released.networks:
        raise SandboxError("stopped chat sandbox network intent could not be cleared")
