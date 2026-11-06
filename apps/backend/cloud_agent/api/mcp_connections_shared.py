"""Shared MCP owner-API contracts and exact HTTP error translation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException

from cloud_agent.mcp import github_oauth
from cloud_agent.mcp.models import CapabilityKind, ToolPolicy
from cloud_agent.mcp.net_policy import EgressPolicy, policy_from_environment
from cloud_agent.settings import public_api_url, setting_env

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent.ports import ConversationQueries, McpConnectionRepository

#: Where this deployment is reachable from a browser, for the OAuth callback.
#: Configured rather than derived from the request: a redirect URI is
#: registered with the authorization server in advance, so deriving it from
#: whatever host a request arrived on would produce a different value per
#: proxy and fail the registration check.
ENV_MCP_PUBLIC_BASE_URL = "AGENT_MCP_PUBLIC_BASE_URL"

#: Where each capability kind is kept in a stored snapshot, in display order.
_SNAPSHOT_SURFACES: tuple[tuple[str, CapabilityKind], ...] = (
    ("tools", CapabilityKind.TOOL),
    ("resources", CapabilityKind.RESOURCE),
    ("resource_templates", CapabilityKind.RESOURCE_TEMPLATE),
    ("prompts", CapabilityKind.PROMPT),
)


def _github_browser_client() -> tuple[str, str] | None:
    """Read the service's app identity, never a user's access token."""
    return github_oauth.client_credentials() if _public_base_url() else None


def _public_base_url() -> str:
    """Reuse the configured public API address unless MCP has an explicit override."""
    explicit = (setting_env(ENV_MCP_PUBLIC_BASE_URL) or "").strip().rstrip("/")
    if explicit:
        return explicit
    # The operator's public API address, including its proxy prefix; never
    # Host/Forwarded headers or the web origin (which may live elsewhere).
    return public_api_url()


def _policy() -> EgressPolicy:
    """The egress policy for user-added connections.

    Delegates to the shared reader so the relay host dials under exactly the
    policy this API validates against: two doors, one policy.
    """
    return policy_from_environment()


class _McpConnectionsStorage(Protocol):
    """The storage capabilities used by the MCP owner API."""

    mcp: McpConnectionRepository
    conversation_queries: ConversationQueries


def _conflict(message: str) -> HTTPException:
    """A state change that lost a race with another writer."""
    return HTTPException(
        status_code=409,
        detail={"error": {"type": "conflict", "message": message}},
    )


def _not_found(message: str) -> HTTPException:
    """A resource that does not exist, or is not this user's."""
    return HTTPException(
        status_code=404,
        detail={"error": {"type": "not_found", "message": message}},
    )


def _bad_request(message: str) -> HTTPException:
    """A request whose input cannot be turned into a connection."""
    return HTTPException(
        status_code=400,
        detail={"error": {"type": "invalid_request_error", "message": message}},
    )


def _capability_view(entry: Mapping[str, Any], kind: CapabilityKind) -> dict[str, Any]:
    """Render one discovered capability for the selection dialog."""
    return {
        "kind": kind.value,
        "name": str(entry.get("name") or ""),
        "description": str(entry.get("description") or ""),
        "input_schema": entry.get("input_schema"),
        "annotations": entry.get("annotations") or {},
    }


def _capability_payload(capability: Any) -> dict[str, Any]:
    """The stored form of one capability."""
    return {
        "name": capability.name,
        "description": capability.description,
        "input_schema": capability.input_schema,
        "annotations": dict(capability.annotations),
    }


def _parse_policy(value: str) -> ToolPolicy:
    """Read a capability policy."""
    try:
        return ToolPolicy(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ToolPolicy)
        raise _bad_request(f"unknown policy {value!r}; use one of: {allowed}") from exc


def _require_store(store: _McpConnectionsStorage | None) -> _McpConnectionsStorage:
    """Refuse a request that needs the database when there is none."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "server_error",
                    "message": "MCP needs a configured database.",
                }
            },
        )
    return store
