"""Owner-owned MCP connector registration and connection routing.

A connector is the user's own machine, reached over an authenticated outbound
channel.  Registration, association and revocation have one owner authority and
one registration-secret lifecycle, so they form an independently testable API
boundary.  Routes here are mounted by the generic MCP compatibility router and
are never mounted on the MCP relay listener.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.api.mcp_connections_shared import (
    _bad_request,
    _conflict,
    _McpConnectionsStorage,
    _not_found,
    _require_store,
)
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
from cloud_agent.mcp.models import ExecutionLocation

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/agent/mcp", tags=["agent-mcp"])

#: What a connection bound to a connector can expect today.
CONNECTOR_RUNTIME_UNAVAILABLE = "unavailable"


class ConnectorView(BaseModel):
    """One connector, as its owner sees it. Never its registration secret."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    status: str
    mounts: list[str] = Field(default_factory=list)
    connection_ids: list[int] = Field(default_factory=list)
    #: Whether this deployment can actually run a connector. ``unavailable``
    #: means the configuration is accepted and nothing serves it yet.
    runtime: str = CONNECTOR_RUNTIME_UNAVAILABLE
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None


class RegisterConnectorRequest(BaseModel):
    """Register one machine as this user's connector."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    #: Paths on the user's machine the connector may reach. Advisory: the
    #: connector enforces its own policy, and this records what its owner said.
    mounts: list[str] = Field(default_factory=list, max_length=32)


class RegisterConnectorResponse(BaseModel):
    """The connector, and the one time its registration secret is shown."""

    connector: ConnectorView
    #: Returned exactly once, hashed at rest. It is what the connector presents
    #: to prove which machine it is, so a second read would be a second copy of
    #: a credential.
    registration_secret: str


class UpdateConnectorRequest(BaseModel):
    """Rename a connector or change the resources it may reach."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=120)
    mounts: list[str] | None = Field(default=None, max_length=32)


class BindConnectorRequest(BaseModel):
    """Route one connection through one connector."""

    model_config = ConfigDict(extra="forbid")

    connection_id: int


async def _connector_view(store: _McpConnectionsStorage, row: Mapping[str, Any]) -> ConnectorView:
    """Build the owner-facing view, with the connection ids it may serve."""
    connector_id = int(row["id"])
    return ConnectorView(
        id=connector_id,
        name=str(row["name"]),
        status=str(row["status"]),
        mounts=[str(mount) for mount in (row.get("mounts") or [])],
        connection_ids=await store.mcp.list_mcp_connector_connections(connector_id=connector_id),
        created_at=row.get("created_at"),
        last_seen_at=row.get("last_seen_at"),
        revoked_at=row.get("revoked_at"),
    )


async def _require_connector(
    store: _McpConnectionsStorage, *, connector_id: int, user_id: str
) -> dict[str, Any]:
    """Fetch one of this user's connectors, or refuse."""
    row = next(
        (
            item
            for item in await store.mcp.list_mcp_connectors(user_id=user_id)
            if int(item["id"]) == connector_id
        ),
        None,
    )
    if row is None:
        raise _not_found("no such connector")
    return row


@router.get("/connectors", response_model=list[ConnectorView])
async def list_connectors(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> list[ConnectorView]:
    """Every connector this user registered."""
    job_store = _require_store(store)
    return [
        await _connector_view(job_store, row)
        for row in await job_store.mcp.list_mcp_connectors(user_id=user["user_id"])
    ]


@router.post("/connectors", response_model=RegisterConnectorResponse, status_code=201)
async def register_connector(
    body: RegisterConnectorRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> RegisterConnectorResponse:
    """Register one machine, returning the secret it will authenticate with.

    The secret is hashed before it is stored and never returned again, which is
    the same rule the connection credentials follow: a value that can be read
    back is a value that can be read back by somebody else.
    """
    job_store = _require_store(store)
    secret = secrets.token_urlsafe(32)
    row = await job_store.mcp.create_mcp_connector(
        user_id=user["user_id"],
        name=body.name.strip(),
        registration_hash=hashlib.sha256(secret.encode()).hexdigest(),
    )
    if body.mounts:
        updated = await job_store.mcp.update_mcp_connector(
            connector_id=int(row["id"]), user_id=user["user_id"], mounts=body.mounts
        )
        row = updated or row
    logger.info(
        "agent_mcp_connector_registered",
        extra={"event": "agent_mcp_connector_registered", "connector_id": row["id"]},
    )
    return RegisterConnectorResponse(
        connector=await _connector_view(job_store, row), registration_secret=secret
    )


@router.patch("/connectors/{connector_id}", response_model=ConnectorView)
async def update_connector(
    connector_id: int,
    body: UpdateConnectorRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectorView:
    """Rename a connector or change the resources it may reach."""
    job_store = _require_store(store)
    updated = await job_store.mcp.update_mcp_connector(
        connector_id=connector_id,
        user_id=user["user_id"],
        name=body.name.strip() if body.name else None,
        mounts=body.mounts,
    )
    if updated is None:
        raise _not_found("no such connector")
    return await _connector_view(job_store, updated)


@router.delete("/connectors/{connector_id}", status_code=204)
async def revoke_connector(
    connector_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> Response:
    """Revoke a connector and detach everything that routed through it."""
    job_store = _require_store(store)
    if not await job_store.mcp.revoke_mcp_connector(
        connector_id=connector_id, user_id=user["user_id"]
    ):
        raise _not_found("no such connector")
    logger.info(
        "agent_mcp_connector_revoked",
        extra={"event": "agent_mcp_connector_revoked", "connector_id": connector_id},
    )
    return Response(status_code=204)


@router.post("/connectors/{connector_id}/connections", response_model=ConnectorView)
async def bind_connector_connection(
    connector_id: int,
    body: BindConnectorRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectorView:
    """Route one connection through this connector.

    Only a connection that runs on the user's own machine can be routed to it.
    A remote connection addressed through a connector would be a server the
    platform could reach directly, sent the long way round -- and a
    managed-worker connection belongs to a worker that is not the user's.
    """
    job_store = _require_store(store)
    await _require_connector(job_store, connector_id=connector_id, user_id=user["user_id"])
    row = await job_store.mcp.get_mcp_connection(
        connection_id=body.connection_id, user_id=user["user_id"]
    )
    if row is None:
        raise _not_found(f"no MCP connection with id {body.connection_id}")
    if str(row.get("location")) != ExecutionLocation.USER_CONNECTOR.value:
        raise _bad_request(
            "only a connection that runs on your own machine can use a connector; "
            f"this one runs at {row.get('location')!r}"
        )
    bound = await job_store.mcp.bind_mcp_connection_connector(
        connection_id=body.connection_id, user_id=user["user_id"], connector_id=connector_id
    )
    if bound is None:
        raise _conflict("that connector was revoked while you were binding to it")
    logger.info(
        "agent_mcp_connector_bound",
        extra={
            "event": "agent_mcp_connector_bound",
            "connector_id": connector_id,
            "connection_id": body.connection_id,
        },
    )
    return await _connector_view(
        job_store,
        await _require_connector(job_store, connector_id=connector_id, user_id=user["user_id"]),
    )


@router.delete("/connectors/{connector_id}/connections/{connection_id}", status_code=204)
async def unbind_connector_connection(
    connector_id: int,
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> Response:
    """Stop serving one connection through this connector."""
    job_store = _require_store(store)
    await _require_connector(job_store, connector_id=connector_id, user_id=user["user_id"])
    unbound = await job_store.mcp.unbind_mcp_connection_connector(
        connection_id=connection_id, user_id=user["user_id"], connector_id=connector_id
    )
    if unbound is None or unbound.get("connector_id") is not None:
        raise _not_found("that connection is not routed through this connector")
    logger.info(
        "agent_mcp_connector_unbound",
        extra={
            "event": "agent_mcp_connector_unbound",
            "connection_id": connection_id,
            "connector_id": connector_id,
        },
    )
    return Response(status_code=204)
