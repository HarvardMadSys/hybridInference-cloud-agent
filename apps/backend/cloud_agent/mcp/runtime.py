"""Process-local MCP host ownership and fenced upstream-session maintenance.

HTTP handlers request the lazy host here. Bootstrap starts maintenance and
closes it before the storage and HTTP clients it uses are released.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.host import McpHost, SessionKey
from cloud_agent.mcp.net_policy import policy_from_environment

if TYPE_CHECKING:
    from cloud_agent.ports import CredentialQueries, McpConnectionRepository

# Keep the deployment's existing log selection for MCP session maintenance.
logger = get_logger("cloud_agent.api.mcp_relay")


class McpRuntimeStorage(Protocol):
    """Capabilities needed to expire approvals and fence upstream sessions."""

    credential_queries: CredentialQueries
    mcp: McpConnectionRepository


def get_host() -> McpHost:
    """The process-wide protocol host, built once against the deployment policy."""
    global _HOST_INSTANCE
    if _HOST_INSTANCE is None:
        _HOST_INSTANCE = McpHost(policy=policy_from_environment())
    return _HOST_INSTANCE


_HOST_INSTANCE: McpHost | None = None
_HOST_MAINTENANCE_INTERVAL_S = 30.0


async def reap_host_sessions(store: McpRuntimeStorage) -> None:
    """Reclaim sockets whose job or selected connection is no longer live."""

    async def is_live(key: SessionKey) -> bool:
        job_id, attempt_id = key.job_attempt
        identity = await store.credential_queries.resolve_model_credential(
            job_id=job_id,
            attempt_id=int(attempt_id),
            lease_generation=key.owner_lease_generation,
        )
        if identity is None:
            return False
        binding = next(
            (
                row
                for row in identity.get("mcp_bindings") or []
                if str(row["connection_id"]) == key.connection_id
            ),
            None,
        )
        if (
            binding is None
            or not binding.get("enabled", True)
            or binding.get("deleted_at") is not None
            or binding.get("owner_id") != identity["user_id"]
        ):
            return False
        connection = await store.mcp.get_mcp_connection(connection_id=int(key.connection_id))
        return (
            connection is not None
            and connection.get("status") != "disabled"
            and connection.get("deleted_at") is None
        )

    if _HOST_INSTANCE is not None:
        await _HOST_INSTANCE.reap_inactive(is_live)


async def maintain_host_sessions(store: McpRuntimeStorage) -> None:
    """Maintain upstream sessions, expire approvals, and apply remembered permissions."""
    while True:
        await asyncio.sleep(_HOST_MAINTENANCE_INTERVAL_S)
        try:
            async with asyncio.timeout(5.0):
                await store.mcp.expire_mcp_invocations()
        except Exception:
            logger.warning("agent_mcp_invocation_expiry_failed", exc_info=True)
        try:
            async with asyncio.timeout(5.0):
                await store.mcp.apply_pending_mcp_allow_rules()
        except Exception:
            logger.warning("agent_mcp_remembered_approval_failed", exc_info=True)
        await reap_host_sessions(store)


async def close_host() -> None:
    """Release this listener's protocol host before its database pool closes."""
    global _HOST_INSTANCE
    instance, _HOST_INSTANCE = _HOST_INSTANCE, None
    if instance is not None:
        await instance.aclose()
