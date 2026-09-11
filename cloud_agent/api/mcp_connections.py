"""The control plane's generic MCP surface: connections, discovery and decisions.

This is where a person adds a server, proves the platform can reach it, sees
what it offers, and approves what a session may use. It is mounted on the
control plane only. The relay's listener deliberately does not contain it,
because a sandbox must not be able to add a connection or approve a call --
reachability is a stronger control than a scope check on each route.

**Two rules run through every handler.**

Secrets are write-only. A request may carry a token; no response ever contains
one. That is enforced by there being no field for it: the response models below
carry a masked hint and a boolean, so a future edit cannot leak a credential by
adding a field to a dictionary.

Every state change is conditional on a version the caller read. Editing a
connection, writing a credential and deciding a call all take the revision they
were made against and fail with a conflict when it has moved. A UI that raced
another tab gets a clear error rather than silently overwriting a decision
somebody else made.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api.deps import get_agent_job_store

# The owner guard lives beside the other agent routes. Imported rather than
# reimplemented, and deliberately one-directional: this module depends on
# ``routes`` and never the reverse, so there is no cycle to reason about.
from cloud_agent.api.routes import require_agent_owner
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import config_import, launch, oauth
from cloud_agent.mcp.authorization import binding_from_selection
from cloud_agent.mcp.availability import DISABLED_MESSAGE, mcp_enabled
from cloud_agent.mcp.host import HostError, McpHost, SessionKey
from cloud_agent.mcp.launch import LaunchPlanError, ManagedLaunchPolicy, build_launch_plan
from cloud_agent.mcp.models import (
    ALIAS_PATTERN,
    AuthKind,
    CapabilityKind,
    ConnectionErrorKind,
    ConnectionStatus,
    ExecutionLocation,
    InvocationStatus,
    LaunchSpec,
    ToolPolicy,
    Transport,
)
from cloud_agent.mcp.net_policy import (
    EgressPolicy,
    EndpointRejected,
    policy_from_environment,
    validate_endpoint,
)
from cloud_agent.mcp.refresh import RefreshRefused, ensure_fresh
from cloud_agent.mcp.registry import (
    McpRegistryError,
    connection_from_row,
    mcp_cipher,
    oauth_cipher,
    resolve_connection,
)
from cloud_agent.source_control import SourceControlError

# Imported at runtime, not only for typing: FastAPI resolves every route
# handler's annotations to build its dependency graph, so moving this behind
# ``TYPE_CHECKING`` turns an import-time warning into a startup failure.
from cloud_agent.storage.store import AgentJobStore  # noqa: TC001

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/agent/mcp", tags=["agent-mcp"])

#: Where this deployment is reachable from a browser, for the OAuth callback.
#: Configured rather than derived from the request: a redirect URI is
#: registered with the authorization server in advance, so deriving it from
#: whatever host a request arrived on would produce a different value per
#: proxy and fail the registration check.
ENV_MCP_PUBLIC_BASE_URL = "AGENT_MCP_PUBLIC_BASE_URL"

#: Which template simplifications this deployment offers. Templates are a
#: convenience, never a gate: a server with no template is added the same way.
_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "name": "github",
        "label": "GitHub",
        "description": (
            "Read issues, pull requests, code and releases across the repositories your "
            "GitHub account can see."
        ),
        "url": "https://api.githubcopilot.com/mcp/readonly",
        "transport": Transport.STREAMABLE_HTTP.value,
        "auth_kind": AuthKind.OAUTH.value,
        "notes": [
            "GitHub does not register clients on behalf of an MCP host, so a client id is "
            "required before the browser flow can start.",
            "The /readonly variant is a GitHub-specific convenience, not a general rule.",
        ],
    },
)


def _public_base_url() -> str:
    """The deployment's browser-facing base URL, or an empty string."""
    return os.environ.get(ENV_MCP_PUBLIC_BASE_URL, "").rstrip("/")


def _policy() -> EgressPolicy:
    """The egress policy for user-added connections.

    Delegates to the shared reader so the relay host dials under exactly the
    policy this API validates against: two doors, one policy.
    """
    return policy_from_environment()


def _require_store(store: AgentJobStore | None) -> AgentJobStore:
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


# --------------------------------------------------------------------------
# Request and response models
# --------------------------------------------------------------------------


class ConnectionView(BaseModel):
    """One connection, as its owner sees it.

    No field here can hold a secret. ``has_credential`` and ``secret_hint`` are
    the whole disclosure: enough to tell a configured connection from an
    unconfigured one, and not enough to use either.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    label: str
    template: str | None = None
    transport: str
    location: str
    url: str | None = None
    argv: list[str] = Field(default_factory=list)
    auth_kind: str
    status: str
    error_kind: str = ConnectionErrorKind.NONE.value
    error_detail: str | None = None
    account_label: str | None = None
    connector_id: int | None = None
    has_credential: bool = False
    secret_hint: str | None = None
    expires_at: datetime | None = None
    config_revision: int = 1
    authorization_epoch: int = 1
    capability_revision: int = 1
    request_timeout_s: float | None = None
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None
    tool_count: int = 0
    resource_count: int = 0
    prompt_count: int = 0
    support: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ConnectionsResponse(BaseModel):
    """Every connection this user owns."""

    connections: list[ConnectionView] = Field(default_factory=list)
    templates: list[dict[str, Any]] = Field(default_factory=list)


class CreateConnectionRequest(BaseModel):
    """Add one connection by hand."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    transport: str = Transport.STREAMABLE_HTTP.value
    location: str = ExecutionLocation.REMOTE.value
    url: str | None = None
    launch: dict[str, Any] | None = None
    auth_kind: str = AuthKind.NONE.value
    template: str | None = None
    connector_id: int | None = None
    #: Seconds one call to this server may take. Omitted means the relay's
    #: default, which is what every connection had before this was a choice.
    request_timeout_s: float | None = Field(default=None, ge=5, le=900)
    #: The static credential, when the auth kind is ``static``. Write-only.
    secret: str | None = None
    header_name: str | None = None
    header_scheme: str | None = None
    #: Secrets a stdio launch spec references by name.
    env_secrets: dict[str, str] = Field(default_factory=dict)
    account_label: str | None = None


class ImportRequest(BaseModel):
    """Parse a pasted configuration, and optionally save what it produced."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    format_hint: str | None = None
    save: bool = False
    #: Per-draft overrides, keyed by the draft's label: the run location the
    #: user chose after seeing what the configuration implied.
    locations: dict[str, str] = Field(default_factory=dict)
    env_secrets: dict[str, str] = Field(default_factory=dict)


class ImportPreview(BaseModel):
    """What a paste turned into, and what was created if it was saved.

    ``drafts`` is always the preview: what the configuration *would* create,
    with commands as argument vectors and secrets masked. ``saved`` holds the
    connections an explicit save produced. They are separate fields because
    they are different shapes, and reusing one field for both is how a client
    ends up guessing which of two documents it is holding.
    """

    drafts: list[dict[str, Any]] = Field(default_factory=list)
    saved: list[ConnectionView] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)
    dialect: str = "unknown"


class UpdateConnectionRequest(BaseModel):
    """Edit a connection, conditional on the revision that was read."""

    model_config = ConfigDict(extra="forbid")

    expected_config_revision: int = Field(ge=1)
    label: str | None = Field(default=None, max_length=120)
    url: str | None = None
    launch: dict[str, Any] | None = None
    auth_kind: str | None = None
    account_label: str | None = None
    request_timeout_s: float | None = Field(default=None, ge=5, le=900)


class SecretRequest(BaseModel):
    """Set or replace a connection's static credential."""

    model_config = ConfigDict(extra="forbid")

    expected_authorization_epoch: int = Field(ge=1)
    secret: str = Field(min_length=1)
    header_name: str | None = None
    header_scheme: str | None = None
    #: Whether this replaces the identity behind the credential. A replacement
    #: invalidates every approval made against the old one.
    replaces_identity: bool = True


class TestResult(BaseModel):
    """What a staged diagnostic found."""

    ok: bool
    error_kind: str = ConnectionErrorKind.NONE.value
    message: str = ""
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    tool_count: int = 0
    resource_count: int = 0
    prompt_count: int = 0
    support: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[dict[str, Any]] = Field(default_factory=list)


class AuthorizeRequest(BaseModel):
    """The client to authorize as, when the server cannot register one for us.

    Optional, and it exists because some servers expect the user to bring a
    client id: refusing to accept one would leave the browser flow permanently
    unavailable on exactly the servers that document it as the way in.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = Field(default=None, max_length=512)


class AuthorizeResponse(BaseModel):
    """Where to send the browser, and what the flow is waiting for."""

    authorization_url: str | None = None
    needs_client_id: bool = False
    message: str = ""


class BindingRequest(BaseModel):
    """One connection's selection for a session."""

    model_config = ConfigDict(extra="forbid")

    connection_id: int
    alias: str | None = None
    enabled: bool = True
    #: Tool name to policy. ``deny`` is also what an omitted name means.
    tools: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)


class BindingResponse(BaseModel):
    """What one session is currently allowed to use."""

    connection_id: int
    connection_label: str
    alias: str
    enabled: bool
    selection_pending: bool
    available: bool
    error: str | None = None
    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    tools: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)
    config_revision: int = 1
    authorization_epoch: int = 1
    capability_revision: int = 1
    policy_revision: int = 1


class BindingsResponse(BaseModel):
    """Every connection a session may use."""

    bindings: list[BindingResponse] = Field(default_factory=list)


class ReplaceBindingsRequest(BaseModel):
    """Replace a session's selection wholesale."""

    model_config = ConfigDict(extra="forbid")

    bindings: list[BindingRequest] = Field(default_factory=list)


class InvocationView(BaseModel):
    """One call that needed a decision."""

    id: int
    job_id: str
    thread_id: str
    connection_id: int
    connection_label: str | None = None
    capability_name: str
    status: str
    arguments_digest: str
    arguments: dict[str, Any] | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    result_summary: str | None = None
    guidance: str = ""


class InvocationsResponse(BaseModel):
    """The calls waiting on this user, or the history of one job."""

    invocations: list[InvocationView] = Field(default_factory=list)


class DecisionRequest(BaseModel):
    """Approve or deny one pending call."""

    model_config = ConfigDict(extra="forbid")

    approve: bool


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------


def _render_hint(stored: Any) -> str | None:
    """Render the stored four-character hint, or ``None`` when there is none.

    The stored value is two leading and two trailing characters of the
    credential. Rendering it as ``ab****yz`` is what lets somebody recognise
    *which* credential is stored; the page never holds a usable value, and the
    response model has no field one could travel in.
    """
    if not isinstance(stored, str) or len(stored) < 4:
        return None
    return f"{stored[:2]}{'*' * 4}{stored[-2:]}"


async def _view(store: AgentJobStore, row: Mapping[str, Any]) -> ConnectionView:
    """Build the owner-facing view of one connection row."""
    connection = connection_from_row(row)
    snapshot = await store.get_mcp_capabilities(connection_id=int(row["id"]))
    config = row.get("auth_config")
    settings = config if isinstance(config, Mapping) else {}
    return ConnectionView(
        id=int(row["id"]),
        label=connection.label,
        template=connection.template,
        transport=connection.config.transport.value,
        location=connection.config.location.value,
        url=connection.config.url,
        argv=list(connection.config.launch.argv()) if connection.config.launch else [],
        auth_kind=connection.auth.kind.value,
        status=connection.status.value,
        error_kind=connection.error_kind.value,
        error_detail=connection.error_detail,
        account_label=row.get("account_label"),
        connector_id=int(row["connector_id"]) if row.get("connector_id") else None,
        has_credential=bool(
            row.get("access_token_ciphertext") or row.get("client_secret_ciphertext")
        ),
        # The stored hint is already two characters and two more; rendering it
        # here rather than masking again keeps it recognisable without ever
        # having held the whole value.
        secret_hint=_render_hint(settings.get("hint")),
        expires_at=row.get("expires_at"),
        config_revision=connection.config_revision,
        authorization_epoch=connection.authorization_epoch,
        capability_revision=connection.capability_revision,
        request_timeout_s=connection.request_timeout_s,
        server_name=str(snapshot.get("server_name") or "") or None if snapshot else None,
        server_version=str(snapshot.get("server_version") or "") or None if snapshot else None,
        protocol_version=str(snapshot.get("protocol_version") or "") or None if snapshot else None,
        tool_count=len(snapshot.get("tools") or []) if snapshot else 0,
        resource_count=len(snapshot.get("resources") or []) if snapshot else 0,
        prompt_count=len(snapshot.get("prompts") or []) if snapshot else 0,
        support=dict(snapshot.get("support") or {}) if snapshot else {},
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
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


def _require_mcp_enabled() -> None:
    """Refuse to *use* MCP when the deployment has withdrawn it.

    Called only by the endpoints that use it -- starting an authorization,
    writing a credential, binding a session or a connector -- and deliberately
    not by the ones that read. The design asks the switch to cover
    authorization, binding and the relay while leaving inspection and
    disconnection open, because an operator who turns a feature off still has to
    be able to see what is configured and take it away.
    """
    if not mcp_enabled():
        raise HTTPException(
            status_code=503,
            detail={"error": {"type": "server_error", "message": DISABLED_MESSAGE}},
        )


def _bad_request(message: str) -> HTTPException:
    """A request whose input cannot be turned into a connection."""
    return HTTPException(
        status_code=400,
        detail={"error": {"type": "invalid_request_error", "message": message}},
    )


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


@router.get("/templates", response_model=list[dict[str, Any]])
async def list_templates(
    user: dict[str, Any] = Depends(require_agent_owner),
) -> list[dict[str, Any]]:
    """Presets that simplify adding a well-known server.

    A preset fills in a URL and a note. It never decides whether a connection
    is allowed, which is why this endpoint is a convenience rather than a
    registry: adding a server with no template is the same code path.
    """
    del user
    return [dict(template) for template in _TEMPLATES]


@router.get("/connections", response_model=ConnectionsResponse)
async def list_connections(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ConnectionsResponse:
    """Every MCP connection this user has added."""
    job_store = _require_store(store)
    rows = await job_store.list_mcp_connections(user_id=user["user_id"])
    views = [await _view(job_store, row) for row in rows]
    return ConnectionsResponse(
        connections=views, templates=[dict(template) for template in _TEMPLATES]
    )


@router.post("/connections", response_model=ConnectionView, status_code=201)
async def create_connection(
    body: CreateConnectionRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ConnectionView:
    """Add a connection, validating its address and credentials before saving."""
    job_store = _require_store(store)
    transport = _parse_transport(body.transport)
    location = _parse_location(body.location)
    url, launch_spec = _validate_target(
        transport=transport, location=location, url=body.url, launch=body.launch
    )
    auth_kind, auth_config = _build_auth(body.auth_kind, body.header_name, body.header_scheme)
    template = body.template if body.template else None
    if template is not None and template not in {item["name"] for item in _TEMPLATES}:
        # An unknown template is refused rather than stored as free text: the
        # column means "which preset this came from", and a value that names
        # nothing makes the provenance unreadable.
        raise _bad_request(f"unknown template: {template!r}")

    row = await job_store.create_mcp_connection(
        user_id=user["user_id"],
        label=body.label.strip(),
        transport=transport.value,
        location=location.value,
        url=url,
        launch_spec=launch_spec,
        auth_kind=auth_kind.value,
        auth_config=auth_config,
        template=template,
        connector_id=body.connector_id,
        account_label=body.account_label,
        request_timeout_s=body.request_timeout_s,
    )
    if body.secret:
        await _store_static_secret(
            job_store,
            row=row,
            secret=body.secret,
            header_name=body.header_name,
            header_scheme=body.header_scheme,
            replaces_identity=False,
        )
    if body.env_secrets:
        await _store_env_secrets(job_store, row=row, secrets=body.env_secrets)
    logger.info(
        "agent_mcp_connection_created",
        extra={"event": "agent_mcp_connection_created", "connection_id": row["id"]},
    )
    refreshed = await job_store.get_mcp_connection(
        connection_id=int(row["id"]), user_id=user["user_id"]
    )
    return await _view(job_store, refreshed or row)


@router.post("/connections/import", response_model=ImportPreview)
async def import_connections(
    body: ImportRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ImportPreview:
    """Parse a pasted MCP configuration, and save it only when asked.

    Pasting shows what the configuration *would* create -- including the
    command it would run and the secrets it carries -- because the alternative
    is a user discovering afterwards that importing a colleague's snippet
    started a program on their machine.
    """
    job_store = _require_store(store)
    try:
        result = config_import.parse_import(body.text, format_hint=body.format_hint)
    except config_import.ImportError_ as exc:
        raise _bad_request(str(exc)) from exc

    preview = [draft.summary() for draft in result.drafts]
    if not body.save:
        return ImportPreview(
            drafts=preview,
            rejected=[{"name": name, "reason": reason} for name, reason in result.rejected],
            dialect=result.dialect,
        )

    saved: list[ConnectionView] = []
    for draft in result.drafts:
        location = body.locations.get(draft.label, draft.location)
        parsed_location = _parse_location(location)
        launch_spec = None
        if draft.launch is not None:
            launch_spec = {
                "command": draft.launch.command,
                "args": list(draft.launch.args),
                "env": dict(draft.launch.env),
                "cwd": draft.launch.cwd,
                "mounts": [
                    {"source": mount.source, "target": mount.target, "read_only": mount.read_only}
                    for mount in config_import.mounts_for(draft.paths)
                ]
                if parsed_location is ExecutionLocation.MANAGED_WORKER
                else [],
            }
        auth_kind = draft.auth_kind
        auth_config: dict[str, Any] = {}
        if auth_kind is AuthKind.STATIC and draft.secrets:
            header_name = draft.secrets[0].name
            auth_config = {
                "header_name": header_name,
                "scheme": draft.header_auth.scheme
                if header_name == draft.header_auth.header_name
                else "",
            }
        row = await job_store.create_mcp_connection(
            user_id=user["user_id"],
            label=draft.label,
            transport=draft.transport.value,
            location=parsed_location.value,
            url=draft.url,
            launch_spec=launch_spec,
            auth_kind=auth_kind.value,
            auth_config=auth_config,
            template=None,
        )
        secrets = {secret.name: secret.value for secret in draft.secrets if secret.value}
        for name, value in body.env_secrets.items():
            secrets.setdefault(name, value)
        if secrets:
            await _store_import_secrets(job_store, row=row, secrets=secrets)
        refreshed = await job_store.get_mcp_connection(
            connection_id=int(row["id"]), user_id=user["user_id"]
        )
        saved.append(await _view(job_store, refreshed or row))
    return ImportPreview(
        drafts=preview,
        saved=saved,
        rejected=[{"name": name, "reason": reason} for name, reason in result.rejected],
        dialect=result.dialect,
    )


@router.get("/connections/{connection_id}", response_model=ConnectionView)
async def get_connection(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ConnectionView:
    """One connection, with what discovery last found."""
    job_store = _require_store(store)
    row = await job_store.get_mcp_connection(connection_id=connection_id, user_id=user["user_id"])
    if row is None:
        raise _not_found("no such MCP connection")
    return await _view(job_store, row)


@router.patch("/connections/{connection_id}", response_model=ConnectionView)
async def update_connection(
    connection_id: int,
    body: UpdateConnectionRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ConnectionView:
    """Edit a connection. A target change invalidates approvals made against it."""
    job_store = _require_store(store)
    existing = await job_store.get_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
    if existing is None:
        raise _not_found("no such MCP connection")

    url = body.url
    if url is not None:
        transport = Transport(str(existing["transport"]))
        url, _unused = _validate_target(
            transport=transport,
            location=ExecutionLocation(str(existing["location"])),
            url=url,
            launch=None,
        )
    auth_kind = body.auth_kind
    if auth_kind is not None:
        auth_kind = _parse_auth_kind(auth_kind).value

    updated = await job_store.update_mcp_connection(
        connection_id=connection_id,
        user_id=user["user_id"],
        expected_config_revision=body.expected_config_revision,
        label=body.label.strip() if body.label else None,
        url=url,
        launch_spec=body.launch,
        auth_kind=auth_kind,
        account_label=body.account_label,
        request_timeout_s=body.request_timeout_s,
    )
    if updated is None:
        raise _conflict("this connection changed since you loaded it; reload and try again")
    if url is not None or body.launch is not None or auth_kind is not None:
        # The target moved, so the old snapshot describes a different server.
        await job_store.delete_mcp_capabilities(connection_id=connection_id)
        await job_store.delete_mcp_oauth_flows(connection_id=connection_id)
    logger.info(
        "agent_mcp_connection_updated",
        extra={"event": "agent_mcp_connection_updated", "connection_id": connection_id},
    )
    return await _view(job_store, updated)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Response:
    """Disconnect a connection, keeping a tombstone and clearing its secret.

    Running jobs are not cancelled. Their next tool call resolves the owner's
    bindings per call rather than at job start, so the disconnect takes effect
    immediately without anything having to hunt down live jobs.
    """
    job_store = _require_store(store)
    removed = await job_store.disconnect_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
    if not removed:
        raise _not_found("no such MCP connection")
    logger.info(
        "agent_mcp_disconnected",
        extra={"event": "agent_mcp_disconnected", "connection_id": connection_id},
    )
    return Response(status_code=204)


@router.post("/connections/{connection_id}/secret", response_model=ConnectionView)
async def set_connection_secret(
    connection_id: int,
    body: SecretRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ConnectionView:
    """Set or replace a static credential."""
    _require_mcp_enabled()
    job_store = _require_store(store)
    row = await job_store.get_mcp_connection(connection_id=connection_id, user_id=user["user_id"])
    if row is None:
        raise _not_found("no such MCP connection")
    if str(row["auth_kind"]) != AuthKind.STATIC.value:
        raise _bad_request("this connection does not use a static credential")
    written = await _store_static_secret(
        job_store,
        row=row,
        secret=body.secret,
        header_name=body.header_name,
        header_scheme=body.header_scheme,
        replaces_identity=body.replaces_identity,
        expected_epoch=body.expected_authorization_epoch,
    )
    if not written:
        raise _conflict("this connection changed since you loaded it; reload and try again")
    refreshed = await job_store.get_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
    return await _view(job_store, refreshed or row)


# --------------------------------------------------------------------------
# Diagnosis and discovery
# --------------------------------------------------------------------------


@router.post("/connections/{connection_id}/test", response_model=TestResult)
async def test_connection(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> TestResult:
    """Handshake, authenticate and read what the server offers. Nothing else.

    No business tool is called, and the result is recorded on the connection so
    the Integrations page can say what is wrong without the user pressing
    anything twice.
    """
    return await _diagnose(connection_id=connection_id, user=user, store=store, record=True)


@router.post("/connections/{connection_id}/discover", response_model=TestResult)
async def discover_connection(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> TestResult:
    """Alias for :func:`test_connection`, kept because the design names both.

    They are the same operation: a discovery that does not handshake is not a
    discovery, and a test that does not read the catalogue cannot tell a
    working server from one that will fail at its first tool call.
    """
    return await _diagnose(connection_id=connection_id, user=user, store=store, record=True)


@router.get("/connections/{connection_id}/capabilities", response_model=list[dict[str, Any]])
async def get_capabilities(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> list[dict[str, Any]]:
    """The stored snapshot: tools, resources, templates and prompts."""
    job_store = _require_store(store)
    row = await job_store.get_mcp_connection(connection_id=connection_id, user_id=user["user_id"])
    if row is None:
        raise _not_found("no such MCP connection")
    snapshot = await job_store.get_mcp_capabilities(connection_id=connection_id)
    if snapshot is None:
        return []
    rendered: list[dict[str, Any]] = []
    for key, kind in (
        ("tools", CapabilityKind.TOOL),
        ("resources", CapabilityKind.RESOURCE),
        ("resource_templates", CapabilityKind.RESOURCE_TEMPLATE),
        ("prompts", CapabilityKind.PROMPT),
    ):
        rendered.extend(_capability_view(item, kind) for item in snapshot.get(key) or [])
    return rendered


async def _diagnose(
    *,
    connection_id: int,
    user: Mapping[str, Any],
    store: AgentJobStore | None,
    record: bool,
) -> TestResult:
    """Run one staged diagnostic and optionally record its outcome."""
    job_store = _require_store(store)
    row = await job_store.get_mcp_connection(connection_id=connection_id, user_id=user["user_id"])
    if row is None:
        raise _not_found("no such MCP connection")

    try:
        row = await ensure_fresh(job_store, row=row, policy=_policy())
    except RefreshRefused as exc:
        if record:
            await job_store.set_mcp_connection_status(
                connection_id=connection_id,
                status=ConnectionStatus.ERROR.value,
                error_kind=exc.kind,
                error_detail=str(exc),
            )
        return TestResult(ok=False, error_kind=exc.kind, message=str(exc))
    resolved = await resolve_connection(job_store, row=row, load_snapshot=False)
    if not resolved.usable:
        if record:
            await job_store.set_mcp_connection_status(
                connection_id=connection_id,
                status=ConnectionStatus.ERROR.value,
                error_kind=resolved.error_kind.value,
                error_detail=resolved.error,
            )
        return TestResult(
            ok=False,
            error_kind=resolved.error_kind.value,
            message=resolved.error or "this connection is not usable",
        )

    mcp_host = McpHost(policy=_policy())
    key = SessionKey(connection_id=str(connection_id), member="diagnostic")
    try:
        _session, snapshot = await mcp_host.discover(
            resolved.connection,
            secret=resolved.credential.get("secret"),
            key=key,
            capability_revision=resolved.connection.capability_revision + 1,
        )
    except (HostError, McpRegistryError, SourceControlError) as exc:
        kind = getattr(exc, "kind", ConnectionErrorKind.UNREACHABLE.value)
        if record:
            await job_store.set_mcp_connection_status(
                connection_id=connection_id,
                status=ConnectionStatus.ERROR.value,
                error_kind=kind,
                error_detail=str(exc),
            )
        return TestResult(ok=False, error_kind=kind, message=str(exc))

    revision = await job_store.save_mcp_capabilities(
        connection_id=connection_id,
        config_revision=resolved.connection.config_revision,
        protocol_version=snapshot.protocol_version,
        server_name=snapshot.server_name,
        server_version=snapshot.server_version,
        tools=[_capability_payload(item) for item in snapshot.tools],
        resources=[_capability_payload(item) for item in snapshot.resources],
        resource_templates=[_capability_payload(item) for item in snapshot.resource_templates],
        prompts=[_capability_payload(item) for item in snapshot.prompts],
        support=dict(snapshot.support),
    )
    del revision
    if record:
        await job_store.set_mcp_connection_status(
            connection_id=connection_id,
            status=ConnectionStatus.ACTIVE.value,
            error_kind=ConnectionErrorKind.NONE.value,
        )
    return TestResult(
        ok=True,
        protocol_version=snapshot.protocol_version,
        server_name=snapshot.server_name,
        server_version=snapshot.server_version,
        tool_count=len(snapshot.tools),
        resource_count=len(snapshot.resources),
        prompt_count=len(snapshot.prompts),
        support=dict(snapshot.support),
        capabilities=[
            _capability_view(_capability_payload(item), item.kind)
            for item in snapshot.all_capabilities()
        ],
    )


def _capability_payload(capability: Any) -> dict[str, Any]:
    """The stored form of one capability."""
    return {
        "name": capability.name,
        "description": capability.description,
        "input_schema": capability.input_schema,
        "annotations": dict(capability.annotations),
    }


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------


@router.post("/connections/{connection_id}/authorize", response_model=AuthorizeResponse)
async def authorize_connection(
    connection_id: int,
    body: AuthorizeRequest | None = None,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AuthorizeResponse:
    """Start a browser authorization for an OAuth connection.

    Discovery, registration and the challenge all happen here, so the browser
    is sent to a URL this service built rather than one a caller supplied.
    """
    from cloud_agent.mcp.transports import HttpMetadataFetcher, TransportError

    _require_mcp_enabled()
    job_store = _require_store(store)
    row = await job_store.get_mcp_connection(connection_id=connection_id, user_id=user["user_id"])
    if row is None:
        raise _not_found("no such MCP connection")
    if body is not None and body.client_id:
        # Stored before discovery, so the registration choice below sees it and
        # the flow it starts is the one this client will complete.
        secret_ciphertext = None
        if body.client_secret:
            try:
                secret_ciphertext = oauth_cipher().encrypt(body.client_secret)
            except SourceControlError as exc:
                raise _no_encryption_key() from exc
        stored = await job_store.set_mcp_client_registration(
            connection_id=connection_id,
            user_id=user["user_id"],
            client_id=body.client_id.strip(),
            client_secret_ciphertext=secret_ciphertext,
        )
        if not stored:
            raise _conflict("this connection changed while you were authorizing it")
        refreshed = await job_store.get_mcp_connection(
            connection_id=connection_id, user_id=user["user_id"]
        )
        row = refreshed or row
    connection = connection_from_row(row)
    if connection.auth.kind is not AuthKind.OAUTH:
        raise _bad_request("this connection does not use OAuth")
    if not connection.config.url:
        raise _bad_request("this connection has no address to authorize against")
    base_url = _public_base_url()
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "server_error",
                    "message": (
                        f"this deployment has no {ENV_MCP_PUBLIC_BASE_URL}, so it cannot "
                        "complete a browser authorization"
                    ),
                }
            },
        )

    policy = _policy()
    fetcher = HttpMetadataFetcher()
    try:
        resource_metadata, metadata = await oauth.discover_authorization_server(
            fetcher,
            resource_url=connection.config.url,
            policy=policy,
            preregistered_issuer=connection.auth.oauth.issuer if connection.auth.oauth else None,
        )
    except (oauth.OAuthError, TransportError, EndpointRejected) as exc:
        raise _bad_request(str(exc)) from exc

    settings = row.get("auth_config")
    configured = settings if isinstance(settings, Mapping) else {}
    client_id = str(configured.get("client_id") or "") or None
    try:
        secret = None
        if row.get("client_secret_ciphertext"):
            secret = oauth_cipher().decrypt(str(row["client_secret_ciphertext"]))
        identity = oauth.choose_registration(
            metadata, preregistered_client_id=client_id, preregistered_client_secret=secret
        )
    except oauth.OAuthError as exc:
        if exc.kind == "client_registration_required":
            return AuthorizeResponse(needs_client_id=True, message=str(exc))
        raise _bad_request(str(exc)) from exc

    if identity.registration == "dcr":
        identity = await _register_client(
            fetcher, metadata, policy=policy, base_url=base_url, connection_id=connection_id
        )

    scopes = oauth.default_scopes(
        resource_metadata, metadata, connection.auth.oauth.scopes if connection.auth.oauth else ()
    )
    flow = oauth.OAuthFlow(
        versions=oauth.FlowVersions(
            connection_id=str(connection_id),
            config_revision=connection.config_revision,
            authorization_epoch=connection.authorization_epoch,
            oauth_generation=connection.oauth_generation,
        ),
        state=oauth.new_state(),
        pkce=oauth.PkcePair.generate(),
        client=identity,
        metadata=metadata,
        resource=connection.config.url,
        redirect_uri=_callback_url(base_url, connection_id),
        scopes=scopes,
    )
    cipher = oauth_cipher()
    await job_store.create_mcp_oauth_flow(
        state_hash=oauth.state_hash(flow.state),
        user_id=user["user_id"],
        connection_id=connection_id,
        config_revision=connection.config_revision,
        authorization_epoch=connection.authorization_epoch,
        oauth_generation=connection.oauth_generation + 1,
        code_verifier_ciphertext=cipher.encrypt(flow.pkce.verifier),
        client_id=identity.client_id,
        client_secret_ciphertext=(
            cipher.encrypt(identity.client_secret) if identity.client_secret else None
        ),
        client_auth_method=identity.auth_method,
        registration=identity.registration,
        issuer=metadata.issuer,
        resource=flow.resource,
        redirect_uri=flow.redirect_uri,
        scopes=list(scopes),
        authorization_endpoint=metadata.authorization_endpoint,
        token_endpoint=metadata.token_endpoint,
        revocation_endpoint=metadata.revocation_endpoint,
        registration_endpoint=metadata.registration_endpoint,
        expires_at=datetime.now(timezone.utc) + oauth.FLOW_LIFETIME,
    )
    return AuthorizeResponse(authorization_url=flow.authorization_url())


async def _register_client(
    fetcher: Any,
    metadata: oauth.AuthorizationServerMetadata,
    *,
    policy: EgressPolicy,
    base_url: str,
    connection_id: int,
) -> oauth.ClientIdentity:
    """Register dynamically, when the server supports nothing better."""
    from cloud_agent.mcp.transports import TransportError

    if not metadata.registration_endpoint:
        raise _bad_request("this authorization server has no registration endpoint")
    body = oauth.registration_request_body(
        client_name="HybridInference Cloud Agent",
        # The URI the redirect will actually use, computed the same way, not
        # the bare path. A server that checks the registered value against the
        # one in the authorization request -- which the specification requires
        # it to -- refuses the two that differ, and the mismatch is invisible
        # until somebody tries to connect.
        redirect_uris=[_callback_url(base_url, connection_id)],
        scopes=metadata.scopes_supported,
    )
    try:
        payload = await _post_json(fetcher, metadata.registration_endpoint, body, policy=policy)
    except TransportError as exc:
        raise _bad_request(f"client registration failed: {exc}") from exc
    return oauth.parse_registration_response(payload)


async def _post_json(
    fetcher: Any, url: str, body: Mapping[str, Any], *, policy: EgressPolicy
) -> Mapping[str, Any]:
    """POST a JSON body under the same egress policy as everything else.

    Every failure leaves here as a request error the caller can report. A raw
    ``httpx`` exception or a policy refusal escaping this helper becomes a 500
    with no diagnosis -- from a URL the *authorization server's* metadata chose,
    which is the one input in this path the user did not supply.
    """
    import httpx

    from cloud_agent.mcp.net_policy import EndpointRejected, check_addresses, validate_endpoint

    try:
        endpoint = validate_endpoint(url, policy)
        loop_addresses = await _resolve(endpoint.host, endpoint.port)
        check_addresses(endpoint.host, loop_addresses, policy)
    except EndpointRejected as exc:
        raise _bad_request(str(exc)) from exc
    except OSError as exc:
        raise _bad_request(f"that address could not be resolved: {exc}") from exc
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=False) as client:
            response = await client.post(
                endpoint.url, json=dict(body), headers={"accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        raise _bad_request(f"the authorization server could not be reached: {exc}") from exc
    if response.status_code >= 400:
        raise _bad_request(
            f"the authorization server answered {response.status_code} to registration"
        )
    try:
        document = response.json()
    except ValueError as exc:
        raise _bad_request("the registration response is not JSON") from exc
    if not isinstance(document, Mapping):
        raise _bad_request("the registration response is not an object")
    del fetcher
    return document


async def _resolve(host: str, port: int) -> list[str]:
    """Resolve a host to its addresses, for the egress check."""
    import asyncio

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=0)
    return [info[4][0] for info in infos]


@router.get("/oauth/callback/{provider}", response_model=None)
async def oauth_callback(
    provider: str,
    request: Request,
    state: str = Query(""),
    code: str = Query(""),
    error: str = Query(""),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Response:
    """Complete a browser authorization, fenced to the flow that started it.

    Unauthenticated by design: the browser arriving here carries a session
    cookie it may or may not still have, and the *state* is the credential that
    identifies the flow. The user is taken from the flow row, which was written
    by an authenticated request, rather than from whatever this request claims.
    """
    job_store = _require_store(store)
    if error:
        return _callback_redirect(provider, ok=False, message=error)
    if not state or not code:
        return _callback_redirect(provider, ok=False, message="missing state or code")

    flow = await job_store.consume_mcp_oauth_flow(state_hash=oauth.state_hash(state))
    if flow is None:
        return _callback_redirect(
            provider, ok=False, message="this authorization expired or was already used"
        )
    if str(flow["connection_id"]) != str(provider):
        # A state minted for one connection cannot complete another's flow.
        return _callback_redirect(
            provider, ok=False, message="this authorization is not for that connection"
        )

    row = await job_store.get_mcp_connection(connection_id=int(flow["connection_id"]))
    if row is None or row.get("deleted_at") is not None:
        return _callback_redirect(provider, ok=False, message="that connection was removed")
    for key, message in (
        ("config_revision", "the connection was reconfigured"),
        ("authorization_epoch", "the connection was reconnected"),
        ("oauth_generation", "a newer authorization replaced this one"),
    ):
        if int(row.get(key) or 0) != int(flow[key]):
            # Per-item CAS: each version is compared on its own so the message
            # names what changed rather than a generic failure.
            return _callback_redirect(provider, ok=False, message=message)

    cipher = oauth_cipher()
    try:
        verifier = cipher.decrypt(str(flow["code_verifier_ciphertext"]))
    except SourceControlError:
        return _callback_redirect(provider, ok=False, message="this flow could not be read")

    from cloud_agent.mcp.transports import TransportError

    metadata = oauth.AuthorizationServerMetadata(
        issuer=str(flow["issuer"]),
        authorization_endpoint=str(flow["authorization_endpoint"]),
        token_endpoint=str(flow["token_endpoint"]),
        revocation_endpoint=flow.get("revocation_endpoint"),
        registration_endpoint=flow.get("registration_endpoint"),
    )
    identity = oauth.ClientIdentity(
        client_id=str(flow["client_id"]),
        registration="dcr",  # type: ignore[arg-type]
        client_secret=(
            cipher.decrypt(str(flow["client_secret_ciphertext"]))
            if flow.get("client_secret_ciphertext")
            else None
        ),
        auth_method=str(flow["client_auth_method"]),
    )
    token_request = oauth.TokenRequest(
        url=metadata.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(flow["redirect_uri"]),
            "client_id": identity.client_id,
            "code_verifier": verifier,
            "resource": str(flow["resource"]),
        },
        client=identity,
    )
    try:
        payload = await _post_form(token_request, policy=_policy())
        tokens = oauth.parse_token_response(payload)
    except (TransportError, oauth.OAuthError) as exc:
        logger.warning("agent_mcp_oauth_exchange_failed", exc_info=True)
        return _callback_redirect(provider, ok=False, message=str(exc))

    credential_cipher = mcp_cipher()
    written = await job_store.set_mcp_credential(
        connection_id=int(flow["connection_id"]),
        expected_authorization_epoch=int(flow["authorization_epoch"]),
        access_token_ciphertext=credential_cipher.encrypt(tokens.access_token),
        refresh_token_ciphertext=(
            credential_cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None
        ),
        expires_at=tokens.expires_at,
        scopes=list(tokens.scopes) or list(flow.get("scopes") or []),
        issuer=metadata.issuer,
        resource=str(flow["resource"]),
        refresh_rotates=bool(tokens.refresh_token),
        client_auth_method=identity.auth_method,
        registration=identity.registration,
        # The client this authorization actually used, written with the
        # credential it obtained. A dynamic registration is decided while the
        # browser is being sent away and lives only in the flow row until here,
        # so without this the connection keeps a token it has no client id to
        # renew: the first refresh sends an empty one, and the authorization
        # server refuses a grant it never issued to "nobody".
        #
        # Merged into ``auth_config`` inside the fenced write rather than
        # recorded through ``set_mcp_client_registration``, which is the manual
        # path: that one bumps the OAuth generation to invalidate a flow already
        # in flight, and this *is* the flow. Bumping it here would refuse the
        # write this same call is making.
        auth_config_patch={"client_id": identity.client_id},
        # The flow's own ciphertext, under the OAuth domain it was written with.
        # For a public client it is absent and any earlier secret is left alone:
        # the authentication method written above is what decides whether one is
        # sent, so a leftover secret cannot be used by accident.
        client_secret_ciphertext=flow.get("client_secret_ciphertext"),
        token_endpoint=metadata.token_endpoint,
        revocation_endpoint=metadata.revocation_endpoint,
        registration_endpoint=metadata.registration_endpoint,
        # The previous refresh token is dead -- this is an authorization, so
        # whatever was there before belonged to a different grant. The one in
        # this response is not: it is what the connection renews with, and the
        # store keeps it.
        clear_refresh=True,
        # Re-checked here rather than only before the exchange. The token
        # request took real time, and a newer flow may have completed inside it:
        # the epoch would not catch that, because re-authorizing the same
        # connection is not a change of target.
        expected_oauth_generation=int(flow["oauth_generation"])
        if flow.get("oauth_generation") is not None
        else None,
        expected_config_revision=int(flow["config_revision"])
        if flow.get("config_revision") is not None
        else None,
    )
    if not written:
        return _callback_redirect(
            provider, ok=False, message="the connection changed during authorization"
        )
    await job_store.set_mcp_connection_status(
        connection_id=int(flow["connection_id"]), status=ConnectionStatus.DRAFT.value
    )
    logger.info(
        "agent_mcp_authorized",
        extra={"event": "agent_mcp_authorized", "connection_id": flow["connection_id"]},
    )
    return _callback_redirect(provider, ok=True, message="")


async def _post_form(request: oauth.TokenRequest, *, policy: EgressPolicy) -> Mapping[str, Any]:
    """POST a token request under the egress policy.

    Raises only the caller's own refusal type. A policy refusal, a name that
    does not resolve, a transport failure or an answer that is not JSON all end
    as one, because the callback's job is to take the browser back to a page
    that says what happened -- not to raise a 500 out of a redirect it cannot
    send.
    """
    import httpx

    from cloud_agent.mcp.net_policy import EndpointRejected, check_addresses, validate_endpoint
    from cloud_agent.mcp.transports import TransportError

    try:
        endpoint = validate_endpoint(request.url, policy)
        check_addresses(endpoint.host, await _resolve(endpoint.host, endpoint.port), policy)
    except EndpointRejected as exc:
        raise TransportError(str(exc), kind=exc.kind) from exc
    except OSError as exc:
        raise TransportError(f"the token endpoint could not be resolved: {exc}") from exc

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
            response = await client.post(
                endpoint.url, data=request.form(), headers=request.headers()
            )
    except httpx.HTTPError as exc:
        # Normalized here rather than left to escape: this is the second HTTP
        # client in the OAuth path, and the fetcher above already made this
        # conversion. A raw transport error would leave the browser at a 500
        # with no diagnosis, which is a worse version of the failure the caller
        # already knows how to report.
        raise TransportError(f"the token endpoint could not be reached: {exc}") from exc
    if response.status_code >= 400:
        raise TransportError(
            f"the token endpoint answered {response.status_code}", kind="needs_authorization"
        )
    try:
        document = response.json()
    except ValueError as exc:
        raise TransportError("the token response is not JSON") from exc
    if not isinstance(document, Mapping):
        raise TransportError("the token response is not an object")
    return document


def _callback_url(base_url: str, connection_id: int) -> str:
    """The one redirect URI for a connection, used for register, authorize and redeem.

    Defined once because the three have to agree. A server compares the
    ``redirect_uri`` in the authorization request against the one registered
    for the client -- the specification requires it to -- so a registration that
    used a different string, even a prefix of the right one, fails the flow at
    the browser with nothing in this service's logs.
    """
    return oauth.redirect_uri(base_url, str(connection_id))


def _callback_redirect(provider: str, *, ok: bool, message: str) -> Response:
    """Send the browser back to the app with a result it can display."""
    base = _public_base_url()
    query = f"mcp_authorized={'1' if ok else '0'}"
    if message:
        from urllib.parse import quote

        query += f"&mcp_message={quote(message)}"
    target = f"{base}/settings/integrations?{query}" if base else f"/settings/integrations?{query}"
    del provider
    return RedirectResponse(url=target, status_code=302)


# --------------------------------------------------------------------------
# Session bindings
# --------------------------------------------------------------------------


@router.get("/threads/{thread_id}/bindings", response_model=BindingsResponse)
async def get_thread_bindings(
    thread_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> BindingsResponse:
    """What one session may use, and what each connection offers."""
    job_store = _require_store(store)
    await _require_thread(job_store, thread_id=thread_id, user_id=user["user_id"])
    return BindingsResponse(bindings=await _binding_views(job_store, thread_id=thread_id))


@router.put("/threads/{thread_id}/bindings", response_model=BindingsResponse)
async def replace_thread_bindings(
    thread_id: str,
    body: ReplaceBindingsRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> BindingsResponse:
    """Replace a session's selection.

    The selection is validated against the connections' *current* revisions, so
    a binding can never be written that already fails its own fence. Every
    connection the user names must be theirs, which is what makes the
    connection id safe to accept from a client.
    """
    _require_mcp_enabled()
    job_store = _require_store(store)
    await _require_thread(job_store, thread_id=thread_id, user_id=user["user_id"])
    prepared: list[dict[str, Any]] = []
    for index, item in enumerate(body.bindings):
        row = await job_store.get_mcp_connection(
            connection_id=item.connection_id, user_id=user["user_id"]
        )
        if row is None:
            raise _not_found(f"no MCP connection with id {item.connection_id}")
        resolved = await resolve_connection(job_store, row=row)
        connection = resolved.connection
        snapshot = resolved.snapshot
        capabilities = snapshot.tools if snapshot else ()
        rules = {name: _parse_policy(value) for name, value in item.tools.items()}
        if snapshot is not None:
            known = {capability.name for capability in capabilities}
            unknown = sorted(set(rules) - known)
            if unknown:
                # A rule for a tool the server does not offer is either a stale
                # client or a typo; either way storing it would grant nothing
                # now and something unexpected after the next discovery.
                raise _bad_request(f"{connection.label} does not offer: {', '.join(unknown)}")
        binding = binding_from_selection(
            thread_id=thread_id,
            connection=connection,
            alias=_checked_alias(item.alias, fallback=_default_alias(row, index)),
            rules=rules,
            resources={name: _parse_policy(value) for name, value in item.resources.items()},
            prompts={name: _parse_policy(value) for name, value in item.prompts.items()},
            capabilities=tuple(capabilities),
        )
        prepared.append(
            {
                "connection_id": int(row["id"]),
                "alias": binding.alias,
                "enabled": item.enabled and binding.enabled,
                "config_revision": binding.config_revision,
                "authorization_epoch": binding.authorization_epoch,
                "capability_revision": binding.capability_revision,
                "policy_revision": binding.policy_revision,
                "rules": {
                    name: {"policy": rule.policy.value, "fingerprint": rule.fingerprint}
                    for name, rule in binding.rules.items()
                },
                "resource_rules": {k: v.value for k, v in binding.resource_rules.items()},
                "prompt_rules": {k: v.value for k, v in binding.prompt_rules.items()},
            }
        )
    await job_store.replace_mcp_bindings(thread_id=thread_id, bindings=prepared)
    logger.info(
        "agent_mcp_bindings_replaced",
        extra={"event": "agent_mcp_bindings_replaced", "thread_id": thread_id},
    )
    return BindingsResponse(bindings=await _binding_views(job_store, thread_id=thread_id))


async def _binding_views(job_store: AgentJobStore, *, thread_id: str) -> list[BindingResponse]:
    """Render every binding on one thread, with what each connection offers."""
    rows = await job_store.list_mcp_bindings(thread_id=thread_id)
    views: list[BindingResponse] = []
    for row in rows:
        connection_row = await job_store.get_mcp_connection(connection_id=int(row["connection_id"]))
        if connection_row is None:
            continue
        resolved = await resolve_connection(job_store, row=connection_row)
        snapshot = resolved.snapshot
        capabilities = (
            [
                _capability_view(_capability_payload(item), item.kind)
                for item in snapshot.all_capabilities()
            ]
            if snapshot
            else []
        )
        views.append(
            BindingResponse(
                connection_id=int(row["connection_id"]),
                connection_label=resolved.connection.label,
                alias=str(row["alias"]),
                enabled=bool(row["enabled"]),
                selection_pending=bool(row.get("selection_pending", False)),
                available=resolved.usable,
                error=resolved.error,
                capabilities=capabilities,
                tools={
                    str(name): str(rule.get("policy") if isinstance(rule, Mapping) else rule)
                    for name, rule in (row.get("rules") or {}).items()
                },
                resources={str(k): str(v) for k, v in (row.get("resource_rules") or {}).items()},
                prompts={str(k): str(v) for k, v in (row.get("prompt_rules") or {}).items()},
                config_revision=int(row["config_revision"]),
                authorization_epoch=int(row["authorization_epoch"]),
                capability_revision=int(row["capability_revision"]),
                policy_revision=int(row["policy_revision"]),
            )
        )
    return views


async def _require_thread(job_store: AgentJobStore, *, thread_id: str, user_id: str) -> None:
    """Refuse a thread this user does not own."""
    thread = await job_store.get_thread(thread_id)
    if thread is None or str(thread.get("user_id")) != user_id:
        raise _not_found("no such conversation")


def _default_alias(row: Mapping[str, Any], index: int) -> str:
    """A stable alias derived from the connection, used when none is given."""
    template = str(row.get("template") or "").strip()
    if template:
        return f"{template}{index + 1}" if index else template
    return f"mcp{row['id']}"


# --------------------------------------------------------------------------
# Invocation decisions
# --------------------------------------------------------------------------


@router.get("/invocations", response_model=InvocationsResponse)
async def list_invocations(
    job_id: str | None = Query(default=None),
    thread_id: str | None = Query(default=None),
    pending_only: bool = Query(default=False),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> InvocationsResponse:
    """Calls that needed a decision, for one job or one conversation.

    The owner is part of the query, not a check after it. These rows carry the
    tool call's arguments decrypted for display -- code, documents, whatever the
    model was about to send -- so a listing that trusts the caller's ``job_id``
    is a way to read another user's calls by guessing an id. The named job is
    checked first so a caller learns "not yours" the same way it learns "does
    not exist": with a 404 that says nothing about which it was.
    """
    job_store = _require_store(store)
    if job_id is None and thread_id is None:
        raise _bad_request("name a job or a conversation")
    if job_id is not None:
        job = await job_store.get_job(job_id)
        if job is None or str(job.get("user_id")) != user["user_id"]:
            raise _not_found("no such job")
    statuses = [InvocationStatus.PENDING.value] if pending_only else None
    rows = await job_store.list_mcp_invocations(
        job_id=job_id,
        thread_id=thread_id,
        statuses=statuses,
        user_id=user["user_id"],
    )
    return InvocationsResponse(invocations=[await _invocation_view(job_store, row) for row in rows])


@router.post("/invocations/{invocation_id}/decision", response_model=InvocationView)
async def decide_invocation(
    invocation_id: int,
    body: DecisionRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> InvocationView:
    """Approve or deny one pending call.

    Scoped by owner at the read, and decided conditionally in SQL: an approval
    that arrives after the call expired does not revive it, and a decision by
    somebody who does not own the job is refused before anything is written.
    """
    job_store = _require_store(store)
    existing = await job_store.get_mcp_invocation(invocation_id=invocation_id)
    if existing is None:
        raise _not_found("no such MCP call")
    job = await job_store.get_job(str(existing["job_id"]))
    if job is None or str(job.get("user_id")) != user["user_id"]:
        raise _not_found("no such MCP call")
    decided = await job_store.decide_mcp_invocation(
        invocation_id=invocation_id, approve=body.approve, decided_by=user["user_id"]
    )
    if decided is None:
        raise _conflict("this call was already decided, or it expired")
    return await _invocation_view(job_store, decided)


async def _invocation_view(job_store: AgentJobStore, row: Mapping[str, Any]) -> InvocationView:
    """Render one invocation, including the arguments the owner is approving."""
    from cloud_agent.mcp.host import decode_arguments
    from cloud_agent.mcp.invocations import recovery_note
    from cloud_agent.mcp.models import Invocation, InvocationStatus as Status

    arguments: dict[str, Any] | None = None
    if row.get("arguments_ciphertext"):
        try:
            arguments = decode_arguments(
                mcp_invocation_cipher().decrypt(str(row["arguments_ciphertext"]))
            )
        except Exception:
            arguments = None
    connection = await job_store.get_mcp_connection(connection_id=int(row["connection_id"]))
    invocation = Invocation(
        invocation_id=str(row["id"]),
        job_id=str(row["job_id"]),
        attempt_id=str(row.get("attempt_id") or ""),
        lease_generation=int(row.get("lease_generation") or 0),
        thread_id=str(row["thread_id"]),
        connection_id=str(row["connection_id"]),
        capability_name=str(row["capability_name"]),
        arguments_digest=str(row["arguments_digest"]),
        status=Status(str(row["status"])),
    )
    return InvocationView(
        id=int(row["id"]),
        job_id=str(row["job_id"]),
        thread_id=str(row["thread_id"]),
        connection_id=int(row["connection_id"]),
        connection_label=str(connection["label"]) if connection else None,
        capability_name=str(row["capability_name"]),
        status=str(row["status"]),
        arguments_digest=str(row["arguments_digest"]),
        arguments=arguments,
        created_at=row.get("created_at"),
        expires_at=row.get("expires_at"),
        decided_at=row.get("decided_at"),
        result_summary=row.get("result_summary"),
        guidance=recovery_note(invocation),
    )


def mcp_invocation_cipher() -> Any:
    """The cipher a held call's arguments are stored under."""
    from cloud_agent.mcp.registry import invocation_cipher

    return invocation_cipher()


# --------------------------------------------------------------------------
# Connectors
# --------------------------------------------------------------------------
#
# A connector is the user's own machine, reached over an authenticated outbound
# channel. It exists for the servers that cannot run in the cloud at all: a
# local file, a database on a private network, a program with a licence key on
# the user's laptop. The platform never asks a connector for a shell or a raw
# socket -- it asks for one named connection's MCP operations, and the connector
# decides whether that connection is one it was authorized for.
#
# **Registering is not running.** No connector runtime ships with this service
# yet, so a registered connector is configuration: it records who the user's
# machine is, which connections they routed to it, and the registration secret
# the connector would present. A connection bound to one is refused at use with
# that reason, in the same way a managed-worker connection is refused for having
# no worker. Saying so is the point of the `runtime` field below -- a page that
# showed "connected" beside a server that cannot start would be the same lie the
# "Coming soon" label existed to avoid.

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


async def _connector_view(store: AgentJobStore, row: Mapping[str, Any]) -> ConnectorView:
    """Build the owner-facing view, with the connection ids it may serve."""
    connector_id = int(row["id"])
    return ConnectorView(
        id=connector_id,
        name=str(row["name"]),
        status=str(row["status"]),
        mounts=[str(mount) for mount in (row.get("mounts") or [])],
        connection_ids=await store.list_mcp_connector_connections(connector_id=connector_id),
        created_at=row.get("created_at"),
        last_seen_at=row.get("last_seen_at"),
        revoked_at=row.get("revoked_at"),
    )


async def _require_connector(
    store: AgentJobStore, *, connector_id: int, user_id: str
) -> dict[str, Any]:
    """Fetch one of this user's connectors, or refuse."""
    row = next(
        (
            item
            for item in await store.list_mcp_connectors(user_id=user_id)
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> list[ConnectorView]:
    """Every connector this user registered."""
    job_store = _require_store(store)
    return [
        await _connector_view(job_store, row)
        for row in await job_store.list_mcp_connectors(user_id=user["user_id"])
    ]


@router.post("/connectors", response_model=RegisterConnectorResponse, status_code=201)
async def register_connector(
    body: RegisterConnectorRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> RegisterConnectorResponse:
    """Register one machine, returning the secret it will authenticate with.

    The secret is hashed before it is stored and never returned again, which is
    the same rule the connection credentials follow: a value that can be read
    back is a value that can be read back by somebody else.
    """
    job_store = _require_store(store)
    secret = secrets.token_urlsafe(32)
    row = await job_store.create_mcp_connector(
        user_id=user["user_id"],
        name=body.name.strip(),
        registration_hash=hashlib.sha256(secret.encode()).hexdigest(),
    )
    if body.mounts:
        updated = await job_store.update_mcp_connector(
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ConnectorView:
    """Rename a connector or change the resources it may reach."""
    job_store = _require_store(store)
    updated = await job_store.update_mcp_connector(
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Response:
    """Revoke a connector and detach everything that routed through it."""
    job_store = _require_store(store)
    if not await job_store.revoke_mcp_connector(connector_id=connector_id, user_id=user["user_id"]):
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ConnectorView:
    """Route one connection through this connector.

    Only a connection that runs on the user's own machine can be routed to it.
    A remote connection addressed through a connector would be a server the
    platform could reach directly, sent the long way round -- and a
    managed-worker connection belongs to a worker that is not the user's.
    """
    _require_mcp_enabled()
    job_store = _require_store(store)
    await _require_connector(job_store, connector_id=connector_id, user_id=user["user_id"])
    row = await job_store.get_mcp_connection(
        connection_id=body.connection_id, user_id=user["user_id"]
    )
    if row is None:
        raise _not_found(f"no MCP connection with id {body.connection_id}")
    if str(row.get("location")) != ExecutionLocation.USER_CONNECTOR.value:
        raise _bad_request(
            "only a connection that runs on your own machine can use a connector; "
            f"this one runs at {row.get('location')!r}"
        )
    bound = await job_store.bind_mcp_connection_connector(
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Response:
    """Stop serving one connection through this connector."""
    job_store = _require_store(store)
    await _require_connector(job_store, connector_id=connector_id, user_id=user["user_id"])
    unbound = await job_store.unbind_mcp_connection_connector(
        connection_id=connection_id, user_id=user["user_id"]
    )
    if unbound is None or unbound.get("connector_id") is not None:
        raise _not_found("that connection is not routed through this connector")
    logger.info(
        "agent_mcp_connector_unbound",
        extra={
            "event": "agent_mcp_connector_unbound",
            "connector_id": connector_id,
            "connection_id": connection_id,
        },
    )
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _parse_transport(value: str) -> Transport:
    """Read a transport name, refusing one this platform cannot carry."""
    try:
        return Transport(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Transport)
        raise _bad_request(f"unknown transport {value!r}; use one of: {allowed}") from exc


def _parse_location(value: str) -> ExecutionLocation:
    """Read an execution location name."""
    try:
        return ExecutionLocation(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ExecutionLocation)
        raise _bad_request(f"unknown run location {value!r}; use one of: {allowed}") from exc


def _parse_auth_kind(value: str) -> AuthKind:
    """Read an authentication kind."""
    try:
        return AuthKind(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in AuthKind)
        raise _bad_request(f"unknown auth kind {value!r}; use one of: {allowed}") from exc


def _checked_alias(alias: str | None, *, fallback: str) -> str:
    """Validate a binding alias, or derive one that is valid by construction.

    The alias is a URL path segment on the relay and a prefix on every tool
    name the connection contributes, so a value outside the grammar produces
    either a 404 the agent reads as a broken server or a tool name the model
    cannot call. Checked here rather than at first use, because the user can
    still be told which value was wrong.
    """
    candidate = (alias or fallback).strip()
    if not re.fullmatch(ALIAS_PATTERN, candidate):
        raise _bad_request(
            f"{candidate!r} is not a usable alias: use letters, digits, underscore and "
            "hyphen, starting with a letter or digit, up to 40 characters"
        )
    return candidate


def _parse_policy(value: str) -> ToolPolicy:
    """Read a capability policy."""
    try:
        return ToolPolicy(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ToolPolicy)
        raise _bad_request(f"unknown policy {value!r}; use one of: {allowed}") from exc


def _build_auth(
    auth_kind: str, header_name: str | None, header_scheme: str | None
) -> tuple[AuthKind, dict[str, Any]]:
    """Validate an authentication choice and render its non-secret settings."""
    kind = _parse_auth_kind(auth_kind)
    if kind is AuthKind.STATIC:
        name = (header_name or "Authorization").strip()
        scheme = (
            header_scheme
            if header_scheme is not None
            else ("Bearer" if name.lower() == "authorization" else "")
        ).strip()
        from cloud_agent.mcp.net_policy import sanitize_headers

        # Validated here rather than at dispatch time, so a header the platform
        # would refuse is reported while the user is still on the form.
        sanitize_headers({name: "x"})
        return kind, {"header_name": name, "scheme": scheme}
    if kind is AuthKind.OAUTH:
        return kind, {}
    return kind, {}


def _validate_target(
    *,
    transport: Transport,
    location: ExecutionLocation,
    url: str | None,
    launch: Mapping[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Check a connection's target and return its storable form."""
    if location is ExecutionLocation.REMOTE:
        if transport is Transport.STDIO:
            raise _bad_request("a remote connection cannot use the stdio transport")
        if not url:
            raise _bad_request("a remote connection needs a URL")
        try:
            endpoint = validate_endpoint(url, _policy())
        except EndpointRejected as exc:
            raise _bad_request(str(exc)) from exc
        return endpoint.url, None

    if transport is not Transport.STDIO:
        raise _bad_request("a local process needs the stdio transport")
    if not launch or not launch.get("command"):
        raise _bad_request("a stdio connection needs a command")
    try:
        spec = LaunchSpec(
            command=str(launch["command"]),
            args=tuple(str(arg) for arg in launch.get("args") or ()),
            env={str(k): str(v) for k, v in (launch.get("env") or {}).items()},
            cwd=str(launch["cwd"]) if launch.get("cwd") else None,
        )
        plan = build_launch_plan(
            spec, connection_id="draft", location=location, policy=ManagedLaunchPolicy()
        )
    except LaunchPlanError as exc:
        raise _bad_request(str(exc)) from exc
    rendered: dict[str, Any] = {
        "command": spec.command,
        "args": list(spec.args),
        "env": dict(spec.env),
        "cwd": spec.cwd,
        "mounts": [
            {"source": mount.source, "target": mount.target, "read_only": mount.read_only}
            for mount in plan.mounts
        ],
    }
    return None, rendered


async def _store_static_secret(
    store: AgentJobStore,
    *,
    row: Mapping[str, Any],
    secret: str,
    header_name: str | None,
    header_scheme: str | None,
    replaces_identity: bool,
    expected_epoch: int | None = None,
) -> bool:
    """Encrypt and store one static credential."""
    _kind, config = _build_auth(
        AuthKind.STATIC.value, header_name or _header_of(row), header_scheme
    )
    # Two characters and two more, so a person can tell *which* credential is
    # stored without the page ever holding one it could be made to render.
    config["hint"] = f"{secret[:2]}{secret[-2:]}" if len(secret) > 4 else ""
    try:
        ciphertext = mcp_cipher().encrypt(secret)
    except SourceControlError as exc:
        raise _no_encryption_key() from exc
    epoch = int(row["authorization_epoch"]) if expected_epoch is None else expected_epoch
    return await store.set_mcp_credential(
        connection_id=int(row["id"]),
        expected_authorization_epoch=epoch,
        access_token_ciphertext=ciphertext,
        auth_config_patch=config,
        bump_epoch=replaces_identity,
    )


def _header_of(row: Mapping[str, Any]) -> str:
    """The header name a connection's static credential is injected as."""
    config = row.get("auth_config")
    settings = config if isinstance(config, Mapping) else {}
    return str(settings.get("header_name") or "Authorization")


async def _store_env_secrets(
    store: AgentJobStore, *, row: Mapping[str, Any], secrets: Mapping[str, str]
) -> None:
    """Encrypt a stdio connection's environment secrets into its launch spec.

    The reference a launch spec carries is ``secret://NAME``; the value lives
    here as ciphertext under the same name. Keeping them in one document means
    a plan cannot be read without its secrets and cannot be executed without
    them either, which is the property that stops a half-restored connection
    from starting with an empty credential.
    """
    spec = row.get("launch_spec")
    launch_spec = dict(spec) if isinstance(spec, Mapping) else {}
    env = {str(k): str(v) for k, v in (launch_spec.get("env") or {}).items()}
    wanted = launch.declared_secret_names(env, {})
    if not wanted:
        return
    try:
        cipher = mcp_cipher()
    except SourceControlError as exc:
        raise _no_encryption_key() from exc
    stored = {str(k): str(v) for k, v in (launch_spec.get("env_secrets") or {}).items()}
    for name in wanted:
        if name in secrets:
            stored[name] = cipher.encrypt(secrets[name])
    launch_spec["env_secrets"] = stored
    await store.set_mcp_launch_spec(
        connection_id=int(row["id"]),
        expected_authorization_epoch=int(row["authorization_epoch"]),
        launch_spec=launch_spec,
    )


async def _store_import_secrets(
    store: AgentJobStore, *, row: Mapping[str, Any], secrets: Mapping[str, str]
) -> None:
    """Store whatever an imported configuration carried, encrypted.

    A header credential goes in the credential column and a stdio environment
    value goes in the launch spec, because that is where each one is read from
    at dispatch time. Storing either in the other's place would produce a
    connection that looks configured and authenticates with nothing.
    """
    if not secrets:
        return
    if str(row.get("transport")) == Transport.STDIO.value:
        await _store_env_secrets(store, row=row, secrets=secrets)
        return
    first = next(iter(secrets.values()))
    await _store_static_secret(
        store,
        row=row,
        secret=first,
        header_name=_header_of(row),
        header_scheme=None,
        replaces_identity=False,
    )


def _no_encryption_key() -> HTTPException:
    """The deployment cannot hold a credential at all."""
    return HTTPException(
        status_code=503,
        detail={
            "error": {
                "type": "server_error",
                "message": "this deployment has no MCP encryption key configured",
            }
        },
    )


__all__ = ["ENV_MCP_PUBLIC_BASE_URL", "router"]
