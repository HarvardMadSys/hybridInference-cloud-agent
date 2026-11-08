"""The control plane's generic MCP surface: connections, discovery and decisions.

This is where a person adds a server, proves the platform can reach it, sees
what it offers, and approves what a session may use. It is mounted on the
control plane only. The relay's listener deliberately does not contain it,
because a sandbox must not be able to add a connection or approve a call --
reachability is a stronger control than a scope check on each route.

This module's router serves the whole surface. The connection routes are
here; browser sign-in is in ``mcp_authorization``, the decisions on held calls
in ``mcp_invocations`` and a session's selections in ``mcp_thread_bindings``,
whose routes it copies in. What a connection may be saved as is checked in
``cloud_agent.application.mcp_connection_inputs``.

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

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

# The owner guard lives beside the other agent routes. Imported rather than
# reimplemented, and deliberately one-directional: this module depends on
# ``_owner_auth`` and never the reverse, so there is no cycle to reason about.
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.api.errors import as_http_exception
from cloud_agent.api.mcp_authorization import router as authorization_router
from cloud_agent.api.mcp_connections_shared import (
    _SNAPSHOT_SURFACES,
    ENV_MCP_PUBLIC_BASE_URL,
    _bad_request,
    _capability_payload,
    _capability_view,
    _conflict,
    _github_browser_client,
    _McpConnectionsStorage,
    _not_found,
    _policy,
    _require_store,
)
from cloud_agent.api.mcp_invocations import router as invocations_router
from cloud_agent.api.mcp_thread_bindings import (
    BindingRequest,
    BindingResponse,
    BindingsResponse,
    ReplaceBindingsRequest,
    router as thread_bindings_router,
)
from cloud_agent.application import mcp_connection_inputs as inputs
from cloud_agent.crypto.credential_cipher import CredentialCipherError
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import config_import, sandbox_servers as sandbox
from cloud_agent.mcp.errors import McpCredentialWriteRejected
from cloud_agent.mcp.host import HostError, McpHost, SessionKey
from cloud_agent.mcp.models import (
    AuthKind,
    ConnectionErrorKind,
    ConnectionStatus,
    ExecutionLocation,
    Transport,
)
from cloud_agent.mcp.package_pins import pin_import
from cloud_agent.mcp.refresh import RefreshRefused, ensure_fresh
from cloud_agent.mcp.registry import McpRegistryError, connection_from_row, resolve_connection

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/agent/mcp", tags=["agent-mcp"])


def _templates() -> list[dict[str, Any]]:
    """Offer browser sign-in only when the service can complete it."""
    templates = [dict(template) for template in _TEMPLATES]
    github = templates[0]
    available = _github_browser_client() is not None
    github["browser_sign_in_available"] = available
    if available:
        github["auth_kind"] = AuthKind.OAUTH.value
        github["notes"] = ["Choose the GitHub account and repositories your agents can access."]
    return [*templates, *(dict(template) for template in sandbox.TEMPLATES)]


#: Which template simplifications this deployment offers. Templates are a
#: convenience, never a gate: a server with no template is added the same way.
_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "name": "github",
        "label": "GitHub",
        "description": (
            "Read repositories, push file changes and create pull requests with your "
            "GitHub account."
        ),
        "url": "https://api.githubcopilot.com/mcp/",
        "transport": Transport.STREAMABLE_HTTP.value,
        "auth_kind": AuthKind.STATIC.value,
        "documentation_url": "https://github.com/github/github-mcp-server",
        "notes": [
            "Use a personal access token with access to the repositories your agents need.",
            "To push file changes and create pull requests, give a fine-grained token "
            "Contents and Pull requests read and write permissions for those repositories.",
        ],
    },
    {
        "name": "slack",
        "label": "Slack",
        "description": "Search conversations and work with messages in your Slack workspace.",
        "url": "https://mcp.slack.com/mcp",
        "transport": Transport.STREAMABLE_HTTP.value,
        "auth_kind": AuthKind.OAUTH.value,
        "documentation_url": "https://docs.slack.dev/ai/slack-mcp-server/",
        "notes": [
            "Slack requires a registered internal or Marketplace app. Have its OAuth client "
            "id and client secret ready, and register the callback URL shown during sign-in.",
        ],
    },
    {
        "name": "google-drive",
        "label": "Google Drive",
        "description": "Find files and work with documents in your Google Drive.",
        "url": "https://drivemcp.googleapis.com/mcp/v1",
        "transport": Transport.STREAMABLE_HTTP.value,
        "auth_kind": AuthKind.OAUTH.value,
        "documentation_url": (
            "https://developers.google.com/workspace/drive/api/guides/configure-mcp-server"
        ),
        "notes": [
            "Google Drive MCP requires Workspace Developer Preview membership. Enable the "
            "Drive API and Google Drive MCP API in your Google Cloud project before connecting.",
            "Have your OAuth client id and client secret ready, and register the callback URL "
            "shown during sign-in.",
        ],
    },
    {
        "name": "linear",
        "label": "Linear",
        "description": "Find issues, plan projects and keep your team's work up to date.",
        "url": "https://mcp.linear.app/mcp",
        "transport": Transport.STREAMABLE_HTTP.value,
        "auth_kind": AuthKind.OAUTH.value,
        "notes": [],
    },
    {
        "name": "notion",
        "label": "Notion",
        "description": "Search your workspace and work with pages, docs and project notes.",
        "url": "https://mcp.notion.com/mcp",
        "transport": Transport.STREAMABLE_HTTP.value,
        "auth_kind": AuthKind.OAUTH.value,
        "notes": [],
    },
    {
        "name": "context7",
        "label": "Context7",
        "description": "Look up current library documentation and code examples.",
        "url": "https://mcp.context7.com/mcp/oauth",
        "transport": Transport.STREAMABLE_HTTP.value,
        "auth_kind": AuthKind.OAUTH.value,
        "notes": [],
    },
)


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
    enabled: bool = True
    error_kind: str = ConnectionErrorKind.NONE.value
    error_detail: str | None = None
    account_label: str | None = None
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
    #: Per-draft locations, keyed by the draft's label, as its preview placed
    #: it. Only one location fits a draft's shape; any other is refused.
    locations: dict[str, str] = Field(default_factory=dict)
    env_secrets: dict[str, str] = Field(default_factory=dict)
    #: Missing values belong to a server, even when several use the same key.
    server_secrets: dict[str, dict[str, str]] = Field(default_factory=dict)


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
    #: New values for a stdio launch's ``secret://NAME`` keys. Write-only; one
    #: left out or blank keeps its stored value (``launch.edited_secret_env``).
    env_secrets: dict[str, str] = Field(default_factory=dict)


class EnableConnectionRequest(BaseModel):
    """Account-wide availability, independent of connection health."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


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


async def _view(store: _McpConnectionsStorage, row: Mapping[str, Any]) -> ConnectionView:
    """Build the owner-facing view of one connection row."""
    connection = connection_from_row(row)
    snapshot = await store.mcp.get_mcp_capabilities(connection_id=int(row["id"]))
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
        enabled=connection.enabled,
        error_kind=connection.error_kind.value,
        error_detail=connection.error_detail,
        account_label=row.get("account_label"),
        # The credential the server is sent. A stored OAuth client secret is
        # not one: until a sign-in completes there is nothing to send.
        has_credential=bool(row.get("access_token_ciphertext")),
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


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


@router.get("/catalog")
async def list_catalog(
    request: Request,
    search: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0, le=100000),
    limit: int = Query(default=24, ge=1, le=100),
    recommended: bool = Query(default=False),
    _owner: str = Depends(require_agent_owner),
) -> dict[str, Any]:
    """Browse the last complete official Registry sync without calling it live."""
    catalog = getattr(request.app.state, "mcp_catalog", None)
    if catalog is None:
        return {"entries": [], "total": 0, "next_offset": None, "synced_at": None, "stale": True}
    return catalog.search(search, offset, limit, recommended=recommended)


@router.get("/connections", response_model=ConnectionsResponse)
async def list_connections(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectionsResponse:
    """Every MCP connection this user has added."""
    job_store = _require_store(store)
    rows = await job_store.mcp.list_mcp_connections(user_id=user["user_id"])
    views = [await _view(job_store, row) for row in rows]
    return ConnectionsResponse(connections=views, templates=_templates())


@router.post("/connections", response_model=ConnectionView, status_code=201)
async def create_connection(
    body: CreateConnectionRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectionView:
    """Add a connection, validating its address and credentials before saving."""
    job_store = _require_store(store)
    with _answered():
        transport = inputs.parse_transport(body.transport)
        location = inputs.parse_location(body.location)
        url, launch_spec = inputs.validate_target(
            transport=transport, location=location, url=body.url, launch=body.launch
        )
        auth_kind, auth_config = inputs.build_auth(
            body.auth_kind, body.header_name, body.header_scheme
        )
    if refusal := sandbox.refusal(location, auth_kind):
        raise _bad_request(refusal)
    template = body.template if body.template else None
    if template is not None and template not in {
        t["name"] for t in (*_TEMPLATES, *sandbox.TEMPLATES)
    }:
        # An unknown template is refused rather than stored as free text: the
        # column means "which preset this came from", and a value that names
        # nothing makes the provenance unreadable.
        raise _bad_request(f"unknown template: {template!r}")

    # Encrypt before the insert. A missing key used to commit the row and then
    # return 503, and the next attempt inserted another credential-less row.
    with _answered():
        access_token_ciphertext, auth_config_patch = inputs.optional_static_credential(
            secret=body.secret,
            header_name=body.header_name,
            header_scheme=body.header_scheme,
            fallback_header=str(auth_config.get("header_name") or "Authorization"),
        )
        sealed_launch_spec = (
            inputs.sealed_launch_spec(launch_spec, body.env_secrets, location=location)
            if body.env_secrets
            else None
        )

    try:
        row = await job_store.mcp.create_mcp_connection(
            user_id=user["user_id"],
            label=body.label.strip(),
            transport=transport.value,
            location=location.value,
            url=url,
            launch_spec=launch_spec,
            auth_kind=auth_kind.value,
            auth_config=auth_config,
            template=template,
            account_label=body.account_label,
            request_timeout_s=body.request_timeout_s,
            reuse_pending=not body.secret and not body.env_secrets,
            access_token_ciphertext=access_token_ciphertext,
            auth_config_patch=auth_config_patch,
            sealed_launch_spec=sealed_launch_spec,
        )
    except McpCredentialWriteRejected as exc:
        raise _conflict("the credential could not be stored with this connection") from exc
    logger.info(
        "agent_mcp_connection_created",
        extra={"event": "agent_mcp_connection_created", "connection_id": row["id"]},
    )
    refreshed = await job_store.mcp.get_mcp_connection(
        connection_id=int(row["id"]), user_id=user["user_id"]
    )
    return await _view(job_store, refreshed or row)


@router.post("/connections/import", response_model=ImportPreview)
async def import_connections(
    body: ImportRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
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
    result = await pin_import(result)  # a bare package name, at its current release
    with _answered():
        result = inputs.without_short_keys(result, body.locations)

    preview = [draft.summary() for draft in result.drafts]
    if not body.save:
        return ImportPreview(
            drafts=preview,
            rejected=[{"name": name, "reason": reason} for name, reason in result.rejected],
            dialect=result.dialect,
        )

    # Prepare every draft, including encryption, before any insert. A later
    # missing key or rejected credential must not leave the earlier drafts saved.
    with _answered():
        prepared = [
            inputs.prepare_import_draft(
                draft,
                user_id=user["user_id"],
                locations=body.locations,
                env_secrets=body.env_secrets,
                server_secrets=body.server_secrets,
            )
            for draft in result.drafts
        ]
    try:
        rows = await job_store.mcp.create_mcp_connections(connections=prepared)
    except McpCredentialWriteRejected as exc:
        raise _conflict("the credential could not be stored with this connection") from exc
    saved: list[ConnectionView] = []
    for row in rows:
        refreshed = await job_store.mcp.get_mcp_connection(
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
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectionView:
    """One connection, with what discovery last found."""
    job_store = _require_store(store)
    row = await job_store.mcp.get_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
    if row is None:
        raise _not_found("no such MCP connection")
    return await _view(job_store, row)


@router.patch("/connections/{connection_id}", response_model=ConnectionView)
async def update_connection(
    connection_id: int,
    body: UpdateConnectionRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectionView:
    """Edit a connection. A target change invalidates approvals made against it.

    A new address or authentication kind also drops the stored credential, so
    the connection has to be signed in to (or given its secret) again.
    """
    job_store = _require_store(store)
    existing = await job_store.mcp.get_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
    if existing is None:
        raise _not_found("no such MCP connection")

    transport = Transport(str(existing["transport"]))
    location = ExecutionLocation(str(existing["location"]))
    url = body.url
    if url is not None:
        with _answered():
            url, _unused = inputs.validate_target(
                transport=transport,
                location=location,
                url=url,
                launch=None,
            )
    launch_spec = None
    if body.launch is not None or body.env_secrets:
        if location is ExecutionLocation.REMOTE:
            raise _bad_request("a remote connection has no process to launch")
        with _answered():
            launch_spec = inputs.edited_launch_spec(
                existing, body.launch, body.env_secrets, transport=transport, location=location
            )
    auth_kind = body.auth_kind
    if auth_kind is not None:
        with _answered():
            auth_kind = inputs.parse_auth_kind(auth_kind).value
        if refusal := sandbox.refusal(location, auth_kind):
            raise _bad_request(refusal)

    updated = await job_store.mcp.update_mcp_connection(
        connection_id=connection_id,
        user_id=user["user_id"],
        expected_config_revision=body.expected_config_revision,
        label=body.label.strip() if body.label else None,
        url=url,
        launch_spec=launch_spec,
        auth_kind=auth_kind,
        account_label=body.account_label,
        request_timeout_s=body.request_timeout_s,
    )
    if updated is None:
        raise _conflict("this connection changed since you loaded it; reload and try again")
    logger.info(
        "agent_mcp_connection_updated",
        extra={"event": "agent_mcp_connection_updated", "connection_id": connection_id},
    )
    return await _view(job_store, updated)


@router.put("/connections/{connection_id}/enabled", response_model=ConnectionView)
async def set_connection_enabled(
    connection_id: int,
    body: EnableConnectionRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectionView:
    """Enable or pause a server across the owner's conversations."""
    job_store = _require_store(store)
    row = await job_store.mcp.set_mcp_connection_enabled(
        connection_id=connection_id, user_id=user["user_id"], enabled=body.enabled
    )
    if row is None:
        raise _not_found("no such MCP connection")
    return await _view(job_store, row)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> Response:
    """Disconnect a connection, keeping a tombstone and clearing its secret.

    Running jobs are not cancelled. Their next tool call resolves the owner's
    bindings per call rather than at job start, so the disconnect takes effect
    immediately without anything having to hunt down live jobs.
    """
    job_store = _require_store(store)
    removed = await job_store.mcp.disconnect_mcp_connection(
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
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> ConnectionView:
    """Set or replace a static credential."""
    job_store = _require_store(store)
    row = await job_store.mcp.get_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
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
    refreshed = await job_store.mcp.get_mcp_connection(
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
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> TestResult:
    """Handshake, authenticate and read what the server offers. Nothing else.

    No business tool is called, and the result is recorded on the connection so
    the Integrations page can say what is wrong without the user pressing
    anything twice.
    """
    return await _diagnose(connection_id=connection_id, user=user, store=store, record=True)


@router.get("/connections/{connection_id}/capabilities", response_model=list[dict[str, Any]])
async def get_capabilities(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> list[dict[str, Any]]:
    """The stored snapshot: tools, resources, templates and prompts."""
    job_store = _require_store(store)
    row = await job_store.mcp.get_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
    if row is None:
        raise _not_found("no such MCP connection")
    snapshot = await job_store.mcp.get_mcp_capabilities(connection_id=connection_id)
    if snapshot is None:
        return []
    rendered: list[dict[str, Any]] = []
    for key, kind in _SNAPSHOT_SURFACES:
        rendered.extend(_capability_view(item, kind) for item in snapshot.get(key) or [])
    return rendered


async def _diagnose(
    *,
    connection_id: int,
    user: Mapping[str, Any],
    store: _McpConnectionsStorage | None,
    record: bool,
) -> TestResult:
    """Run one staged diagnostic and optionally record its outcome."""
    job_store = _require_store(store)
    loaded_row = await job_store.mcp.get_mcp_connection(
        connection_id=connection_id, user_id=user["user_id"]
    )
    if loaded_row is None:
        raise _not_found("no such MCP connection")
    if loaded_row.get("location") == ExecutionLocation.SANDBOX.value:
        ok, message = await sandbox.checked_in_place(job_store.mcp, loaded_row, record=record)
        return TestResult(ok=ok, error_kind="none" if ok else "launch_failed", message=message)
    row = loaded_row

    async def record_status(
        status: str,
        error_kind: str,
        error_detail: str | None = None,
        *,
        capability_revision: int | None = None,
    ) -> None:
        """Publish a diagnosis only while its connection and catalogue still match."""
        if record and not await job_store.mcp.set_mcp_connection_status(
            connection_id=connection_id,
            status=status,
            error_kind=error_kind,
            error_detail=error_detail,
            expected_config_revision=int(row["config_revision"]),
            expected_authorization_epoch=int(row["authorization_epoch"]),
            expected_credential_version=int(row["credential_version"]),
            expected_capability_revision=(
                int(row["capability_revision"])
                if capability_revision is None
                else capability_revision
            ),
        ):
            raise _conflict("this connection changed during testing; reload and test again")

    try:
        row = dict(await ensure_fresh(job_store.mcp, row=row, policy=_policy()))
    except RefreshRefused as exc:
        await record_status(ConnectionStatus.ERROR.value, exc.kind, str(exc))
        return TestResult(ok=False, error_kind=exc.kind, message=str(exc))
    resolved = await resolve_connection(job_store.mcp, row=row, load_snapshot=False)
    if not resolved.usable:
        await record_status(ConnectionStatus.ERROR.value, resolved.error_kind.value, resolved.error)
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
    except (HostError, McpRegistryError, CredentialCipherError) as exc:
        kind = getattr(exc, "kind", ConnectionErrorKind.UNREACHABLE.value)
        await record_status(ConnectionStatus.ERROR.value, kind, str(exc))
        return TestResult(ok=False, error_kind=kind, message=str(exc))
    finally:
        await mcp_host.aclose()

    revision = await job_store.mcp.save_mcp_capabilities(
        connection_id=connection_id,
        config_revision=resolved.connection.config_revision,
        expected_authorization_epoch=resolved.connection.authorization_epoch,
        expected_capability_revision=resolved.connection.capability_revision,
        expected_credential_version=resolved.connection.credential_version,
        protocol_version=snapshot.protocol_version,
        server_name=snapshot.server_name,
        server_version=snapshot.server_version,
        tools=[_capability_payload(item) for item in snapshot.tools],
        resources=[_capability_payload(item) for item in snapshot.resources],
        resource_templates=[_capability_payload(item) for item in snapshot.resource_templates],
        prompts=[_capability_payload(item) for item in snapshot.prompts],
        support=dict(snapshot.support),
    )
    if not revision:
        raise _conflict("this connection changed during testing; reload and test again")
    await record_status(
        ConnectionStatus.ACTIVE.value, ConnectionErrorKind.NONE.value, capability_revision=revision
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


# --------------------------------------------------------------------------
# Domain refusals
# --------------------------------------------------------------------------


@contextmanager
def _answered() -> Iterator[None]:
    """Answer a connection check's domain refusal as the ``HTTPException`` it maps to."""
    try:
        yield
    except (Refused, Unavailable) as exc:
        raise as_http_exception(exc) from exc


def _build_auth(
    auth_kind: str, header_name: str | None, header_scheme: str | None
) -> tuple[AuthKind, dict[str, Any]]:
    """Validate an authentication choice and render its non-secret settings."""
    with _answered():
        return inputs.build_auth(auth_kind, header_name, header_scheme)


async def _store_static_secret(
    store: _McpConnectionsStorage,
    *,
    row: Mapping[str, Any],
    secret: str,
    header_name: str | None,
    header_scheme: str | None,
    replaces_identity: bool,
    expected_epoch: int | None = None,
) -> bool:
    """Encrypt and store one static credential."""
    with _answered():
        return await inputs.store_static_secret(
            store.mcp,
            row=row,
            secret=secret,
            header_name=header_name,
            header_scheme=header_scheme,
            replaces_identity=replaces_identity,
            expected_epoch=expected_epoch,
        )


__all__ = [
    "ENV_MCP_PUBLIC_BASE_URL",
    "BindingRequest",
    "BindingResponse",
    "BindingsResponse",
    "ReplaceBindingsRequest",
    "router",
]

# Copy already-prefixed routes into the compatibility router, in the order they
# were declared when every route lived here. This keeps the public router flat,
# so an application that includes it can bind dependency overrides exactly as
# it did then.
router.routes.extend(authorization_router.routes)
router.routes.extend(invocations_router.routes)
router.routes.extend(thread_bindings_router.routes)
