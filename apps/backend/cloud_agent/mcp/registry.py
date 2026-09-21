"""Resolving a session's bindings into the servers a job may actually reach.

Three inputs, and a server needs all three:

- a **connection row**, which says where the server is and how to authenticate;
- a **binding row**, which says whether this conversation may use it and which
  of its capabilities it may use;
- a **capability snapshot**, which says what the server currently offers.

A connection that fails any of them is *absent* rather than fatal. One dead
connection must not take out the ones beside it, so each is reported with its
own reason and the rest resolve normally -- which is what lets a page show
"three connected, one needs re-authorization" instead of a stack trace.

**The signature is the feature.** Resolution takes a thread or a fence row, and
there is no process-wide registry to ask instead. A call site with no thread
has no answer and cannot compile its way into one.

Credentials are decrypted through the domain-separated cipher the previous
implementation introduced: one deployment key, two credential kinds that do not
interchange, so an MCP ciphertext cannot be read -- or planted -- as a git
token.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.authorization import visible_capabilities
from cloud_agent.mcp.models import (
    AuthKind,
    AuthProfile,
    Capability,
    CapabilityKind,
    CapabilitySnapshot,
    ConnectionConfig,
    ConnectionErrorKind,
    ConnectionStatus,
    ExecutionLocation,
    HeaderAuth,
    LaunchSpec,
    McpConnection,
    McpServer,
    Mount,
    OAuthProfile,
    ThreadBinding,
    ToolPolicy,
    ToolRule,
    Transport,
)
from cloud_agent.source_control import SourceControlCipher, SourceControlError

if TYPE_CHECKING:
    from cloud_agent.ports.mcp import McpConnectionRepository

logger = get_logger(__name__)

#: Domain separator for stored MCP credentials. The secret is the one this
#: service already deploys for source control; the domain is not, so an MCP
#: ciphertext cannot be read -- or planted -- as a GitLab token and vice versa.
#: One key to rotate, two credential kinds that do not interchange.
MCP_CREDENTIAL_DOMAIN = b"hybridinference/mcp-connection/v1"

#: Domain separator for the argument bodies of calls held for approval. Kept
#: apart from the credential domain for the same reason: a tool argument must
#: never decrypt as a token.
MCP_INVOCATION_DOMAIN = b"hybridinference/mcp-invocation/v1"

#: Domain separator for a flow's PKCE verifier and a client secret.
MCP_OAUTH_DOMAIN = b"hybridinference/mcp-oauth/v1"


class McpRegistryError(Exception):
    """Raised when a caller asks for something its session may not use."""


def mcp_cipher(secret: str | None = None) -> SourceControlCipher:
    """The cipher MCP connection credentials are stored under.

    Raises :class:`~cloud_agent.source_control.SourceControlError` when the
    service has no encryption key, which is the same condition that leaves
    source control unconfigured. Callers that only need to *list* templates
    treat that as "MCP is not configured here" rather than an error.
    """
    return SourceControlCipher(secret, domain=MCP_CREDENTIAL_DOMAIN)


def invocation_cipher(secret: str | None = None) -> SourceControlCipher:
    """The cipher a held call's argument body is protected with."""
    return SourceControlCipher(secret, domain=MCP_INVOCATION_DOMAIN)


def oauth_cipher(secret: str | None = None) -> SourceControlCipher:
    """The cipher a flow's verifier and client secret are protected with."""
    return SourceControlCipher(secret, domain=MCP_OAUTH_DOMAIN)


@dataclass(frozen=True)
class ResolvedConnection:
    """One connection, with the state that makes it usable or not."""

    connection: McpConnection
    credential: Mapping[str, Any] = field(default_factory=dict)
    snapshot: CapabilitySnapshot | None = None
    #: Why this connection buys nothing right now, in the owner's terms.
    error: str | None = None
    error_kind: ConnectionErrorKind = ConnectionErrorKind.NONE

    @property
    def usable(self) -> bool:
        """Whether anything may be called through this connection."""
        return self.error is None


def _parse_auth(row: Mapping[str, Any]) -> AuthProfile:
    """Rebuild a connection's auth profile from its stored columns."""
    kind = AuthKind(str(row.get("auth_kind") or AuthKind.NONE))
    config = row.get("auth_config")
    settings = config if isinstance(config, Mapping) else {}
    if kind is AuthKind.STATIC:
        return AuthProfile(
            kind=kind,
            header=HeaderAuth(
                header_name=str(settings.get("header_name") or "Authorization"),
                scheme=str(settings.get("scheme") or ""),
            ),
        )
    if kind is AuthKind.OAUTH:
        return AuthProfile(
            kind=kind,
            oauth=OAuthProfile(
                issuer=_optional(row.get("issuer")),
                resource=_optional(row.get("resource")),
                scopes=tuple(row.get("scopes") or ()),
                client_id=_optional(settings.get("client_id")),
                client_auth_method=str(row.get("client_auth_method") or "none"),
                registration=str(row.get("registration") or "dcr"),  # type: ignore[arg-type]
            ),
        )
    return AuthProfile(kind=AuthKind.NONE)


def _optional(value: Any) -> str | None:
    """Read an optional text column as a stripped string or ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_launch(row: Mapping[str, Any]) -> LaunchSpec | None:
    """Rebuild a stdio launch spec from its stored JSON."""
    raw = row.get("launch_spec")
    if not isinstance(raw, Mapping) or not raw.get("command"):
        return None
    mounts = raw.get("mounts")
    return LaunchSpec(
        command=str(raw["command"]),
        args=tuple(str(arg) for arg in raw.get("args") or ()),
        env={str(key): str(value) for key, value in (raw.get("env") or {}).items()},
        cwd=_optional(raw.get("cwd")),
        mounts=tuple(
            Mount(
                source=str(mount.get("source")),
                target=str(mount.get("target")),
                read_only=bool(mount.get("read_only", True)),
            )
            for mount in (mounts or [])
            if isinstance(mount, Mapping) and mount.get("source") and mount.get("target")
        ),
        pinned_package=_optional(raw.get("pinned_package")),
        pinned_version=_optional(raw.get("pinned_version")),
    )


def connection_from_row(row: Mapping[str, Any]) -> McpConnection:
    """Build the domain model for one stored connection row."""
    transport = Transport(str(row.get("transport") or Transport.STREAMABLE_HTTP))
    location = ExecutionLocation(str(row.get("location") or ExecutionLocation.REMOTE))
    return McpConnection(
        connection_id=str(row["id"]),
        user_id=str(row["user_id"]),
        label=str(row.get("label") or row.get("template") or "MCP server"),
        config=ConnectionConfig(
            transport=transport,
            location=location,
            url=_optional(row.get("url")),
            launch=_parse_launch(row) if transport is Transport.STDIO else None,
        ),
        auth=_parse_auth(row),
        template=_optional(row.get("template")),
        status=ConnectionStatus(str(row.get("status") or ConnectionStatus.DRAFT)),
        enabled=bool(row.get("enabled", True)),
        error_kind=ConnectionErrorKind(str(row.get("error_kind") or "none")),
        error_detail=_optional(row.get("error_detail")),
        connector_id=str(row["connector_id"]) if row.get("connector_id") else None,
        config_revision=int(row.get("config_revision") or 1),
        authorization_epoch=int(row.get("authorization_epoch") or 1),
        oauth_generation=int(row.get("oauth_generation") or 0),
        credential_version=int(row.get("credential_version") or 1),
        capability_revision=int(row.get("capability_revision") or 1),
        request_timeout_s=(
            float(row["request_timeout_s"]) if row.get("request_timeout_s") else None
        ),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def binding_from_row(row: Mapping[str, Any]) -> ThreadBinding:
    """Build the domain model for one stored thread binding."""
    rules: dict[str, ToolRule] = {}
    raw_rules = row.get("rules")
    if isinstance(raw_rules, Mapping):
        for name, value in raw_rules.items():
            if isinstance(value, Mapping):
                rules[str(name)] = ToolRule(
                    name=str(name),
                    policy=ToolPolicy(str(value.get("policy") or ToolPolicy.DENY)),
                    fingerprint=str(value.get("fingerprint") or ""),
                )
            else:
                # A bare string is the shape an earlier writer used; read it
                # rather than dropping the rule, which would silently turn an
                # allowed tool into a refused one.
                rules[str(name)] = ToolRule(
                    name=str(name), policy=ToolPolicy(str(value)), fingerprint=""
                )
    return ThreadBinding(
        thread_id=str(row["thread_id"]),
        connection_id=str(row["connection_id"]),
        alias=str(row.get("alias") or f"mcp{row['connection_id']}"),
        config_revision=int(row.get("config_revision") or 0),
        authorization_epoch=int(row.get("authorization_epoch") or 0),
        capability_revision=int(row.get("capability_revision") or 0),
        policy_revision=int(row.get("policy_revision") or 1),
        enabled=bool(row.get("enabled", True)),
        selection_pending=bool(row.get("selection_pending", False)),
        rules=rules,
        resource_rules=_policy_map(row.get("resource_rules")),
        prompt_rules=_policy_map(row.get("prompt_rules")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _policy_map(raw: Any) -> dict[str, ToolPolicy]:
    """Decode a stored name-to-policy map."""
    if not isinstance(raw, Mapping):
        return {}
    return {str(name): ToolPolicy(str(value)) for name, value in raw.items()}


def _capability(entry: Mapping[str, Any], kind: CapabilityKind) -> Capability:
    """Rebuild one capability from its stored JSON."""
    schema = entry.get("input_schema")
    annotations = entry.get("annotations")
    return Capability(
        kind=kind,
        name=str(entry.get("name") or ""),
        description=str(entry.get("description") or ""),
        input_schema=schema if isinstance(schema, Mapping) else None,
        annotations=annotations if isinstance(annotations, Mapping) else {},
    )


def snapshot_from_row(row: Mapping[str, Any]) -> CapabilitySnapshot:
    """Rebuild a capability snapshot from its stored row."""
    return CapabilitySnapshot(
        connection_id=str(row["connection_id"]),
        capability_revision=int(row["capability_revision"]),
        config_revision=int(row["config_revision"]),
        protocol_version=str(row["protocol_version"]),
        server_name=str(row.get("server_name") or ""),
        server_version=str(row.get("server_version") or ""),
        tools=tuple(_capability(item, CapabilityKind.TOOL) for item in row.get("tools") or ()),
        resources=tuple(
            _capability(item, CapabilityKind.RESOURCE) for item in row.get("resources") or ()
        ),
        resource_templates=tuple(
            _capability(item, CapabilityKind.RESOURCE_TEMPLATE)
            for item in row.get("resource_templates") or ()
        ),
        prompts=tuple(
            _capability(item, CapabilityKind.PROMPT) for item in row.get("prompts") or ()
        ),
        support=row.get("support") or {},
        discovered_at=row.get("discovered_at"),
    )


def _has_expired(row: Mapping[str, Any], *, margin_seconds: int = 0) -> bool:
    """Whether the stored credential is one we already know to be dead.

    ``NULL`` means "no expiry we were told about", not "never expires", so it
    reads as live: such a connection fails at its next call if the upstream has
    revoked it, which is the best a provider that issues no expiry allows.
    """
    expires_at = row.get("expires_at")
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if margin_seconds:
        from datetime import timedelta

        return expires_at - timedelta(seconds=margin_seconds) <= now
    return expires_at <= now


def _can_refresh(row: Mapping[str, Any]) -> bool:
    """Whether this connection has what a refresh needs."""
    return (
        bool(row.get("refresh_token_ciphertext"))
        and bool(row.get("issuer"))
        and bool(row.get("resource"))
    )


async def resolve_connection(
    store: McpConnectionRepository,
    *,
    row: Mapping[str, Any],
    cipher: SourceControlCipher | None = None,
    load_snapshot: bool = True,
) -> ResolvedConnection:
    """Turn one stored connection row into a usable server, or a reason.

    Never raises because a connection cannot be used. A missing encryption key,
    a credential that will not decrypt, an expiry already past -- each disables
    *that* connection and leaves the others alone.
    """
    connection = connection_from_row(row)
    if not connection.enabled or connection.status is ConnectionStatus.DISABLED:
        return ResolvedConnection(
            connection=connection,
            error="This connection is turned off.",
            error_kind=ConnectionErrorKind.NONE,
        )
    if row.get("deleted_at") is not None:
        return ResolvedConnection(
            connection=connection,
            error="This connection was disconnected.",
            error_kind=ConnectionErrorKind.NONE,
        )

    snapshot: CapabilitySnapshot | None = None
    if load_snapshot:
        snapshot_row = await store.get_mcp_capabilities(connection_id=int(row["id"]))
        if snapshot_row is not None:
            snapshot = snapshot_from_row(snapshot_row)

    if connection.auth.kind is AuthKind.NONE:
        # Nothing to decrypt and nothing to expire: a server that authenticates
        # by network position or not at all is usable as soon as it is reachable.
        return ResolvedConnection(connection=connection, snapshot=snapshot)

    credential_present = bool(row.get("access_token_ciphertext")) or bool(
        row.get("client_secret_ciphertext")
    )
    if not credential_present:
        return ResolvedConnection(
            connection=connection,
            snapshot=snapshot,
            error="This connection needs to be authorized.",
            error_kind=ConnectionErrorKind.NEEDS_AUTHORIZATION,
        )
    if _has_expired(row) and not _can_refresh(row):
        return ResolvedConnection(
            connection=connection,
            snapshot=snapshot,
            error="This connection has expired. Connect it again.",
            error_kind=ConnectionErrorKind.NEEDS_AUTHORIZATION,
        )

    resolved_cipher = cipher
    if resolved_cipher is None:
        try:
            resolved_cipher = mcp_cipher()
        except SourceControlError:
            logger.error(
                "agent_mcp_encryption_key_missing",
                extra={
                    "event": "agent_mcp_encryption_key_missing",
                    "connection_id": connection.connection_id,
                },
            )
            return ResolvedConnection(
                connection=connection,
                snapshot=snapshot,
                error="MCP is not configured on this deployment.",
                error_kind=ConnectionErrorKind.NONE,
            )

    ciphertext = row.get("access_token_ciphertext") or row.get("client_secret_ciphertext") or ""
    try:
        secret = resolved_cipher.decrypt(str(ciphertext))
    except SourceControlError:
        # Narrow deliberately: this is the whole contract of `decrypt`, and a
        # broader catch would swallow a genuine bug here as "the user's
        # credential is bad" -- the one diagnosis nobody would question.
        logger.warning(
            "agent_mcp_credential_unreadable",
            extra={
                "event": "agent_mcp_credential_unreadable",
                "connection_id": connection.connection_id,
            },
        )
        return ResolvedConnection(
            connection=connection,
            snapshot=snapshot,
            error="This connection could not be read. Connect it again.",
            error_kind=ConnectionErrorKind.NEEDS_AUTHORIZATION,
        )
    return ResolvedConnection(
        connection=connection,
        credential={"secret": secret, "refresh_rotates": bool(row.get("refresh_rotates"))},
        snapshot=snapshot,
    )


async def resolve_for_thread(
    store: McpConnectionRepository,
    *,
    thread_id: str,
    cipher: SourceControlCipher | None = None,
) -> list[tuple[ResolvedConnection, ThreadBinding]]:
    """Every connection one conversation is bound to, with its state.

    The order is the bindings' own, so a page and a composer agree without
    either sorting again.
    """
    binding_rows = await store.list_mcp_bindings(thread_id=thread_id)
    resolved: list[tuple[ResolvedConnection, ThreadBinding]] = []
    for binding_row in binding_rows:
        connection_row = await store.get_mcp_connection(
            connection_id=int(binding_row["connection_id"])
        )
        if connection_row is None:
            continue
        resolved.append(
            (
                await resolve_connection(store, row=connection_row, cipher=cipher),
                binding_from_row(binding_row),
            )
        )
    return resolved


def reachable_tools(resolved: ResolvedConnection, binding: ThreadBinding) -> list[Capability]:
    """The tools one binding may list, from one connection's snapshot.

    Default bindings advertise discovered capabilities and require approval.
    Explicit denials remain hidden, and disabled servers advertise nothing.
    """
    if resolved.snapshot is None:
        return []
    return visible_capabilities(binding, resolved.snapshot.tools, CapabilityKind.TOOL)


def server_for(
    resolved: ResolvedConnection,
    binding: ThreadBinding,
    *,
    tools: Sequence[Capability] | None = None,
) -> McpServer:
    """Build the reachable server the relay will dial for one binding.

    The credential is attached here and nowhere else. It reaches the upstream
    and nothing further: :meth:`McpServer.public` has nowhere to put it, and the
    relay strips it from everything travelling back toward the sandbox.
    """
    connection = resolved.connection
    allowed = tools if tools is not None else reachable_tools(resolved, binding)
    # The other two surfaces are resolved with the same function as the tools,
    # because "what may this session reach" must have one answer per kind and
    # not one answer for tools plus a hopeful default for the rest.
    snapshot = resolved.snapshot
    resources = (
        visible_capabilities(
            binding, snapshot.of_kind(CapabilityKind.RESOURCE), CapabilityKind.RESOURCE
        )
        if snapshot
        else ()
    )
    templates = (
        visible_capabilities(binding, snapshot.resource_templates, CapabilityKind.RESOURCE_TEMPLATE)
        if snapshot
        else ()
    )
    prompts = (
        visible_capabilities(
            binding, snapshot.of_kind(CapabilityKind.PROMPT), CapabilityKind.PROMPT
        )
        if snapshot
        else ()
    )
    headers: dict[str, str] = {}
    secret = resolved.credential.get("secret")
    if secret and connection.auth.kind is AuthKind.STATIC:
        headers = connection.auth.header.render(str(secret))
    elif secret and connection.auth.kind is AuthKind.OAUTH:
        headers = {"Authorization": f"Bearer {secret}"}
    return McpServer(
        name=binding.alias,
        url=connection.config.url,
        connection_id=connection.connection_id,
        transport=connection.config.transport,
        headers=headers,
        tools=frozenset(capability.name for capability in allowed),
        resources=frozenset(capability.name for capability in [*resources, *templates]),
        prompts=frozenset(capability.name for capability in prompts),
        description=connection.label,
        request_timeout_s=connection.request_timeout_s,
        location=connection.config.location,
        launch=connection.config.launch,
        connector_id=connection.connector_id,
    )


async def servers_for_thread(
    store: McpConnectionRepository,
    *,
    thread_id: str,
    cipher: SourceControlCipher | None = None,
) -> dict[str, McpServer]:
    """Every server a job on ``thread_id`` may reach, keyed by alias.

    A connection that is not usable is absent rather than fatal, so one dead
    credential does not take out the servers beside it. The relay reports the
    absence by alias, which is why the alias is the key.
    """
    resolved_pairs = await resolve_for_thread(store, thread_id=thread_id, cipher=cipher)
    servers: dict[str, McpServer] = {}
    for resolved, binding in resolved_pairs:
        if (
            not resolved.usable
            or not binding.enabled
            or resolved.connection.status is ConnectionStatus.ERROR
        ):
            continue
        servers[binding.alias] = server_for(resolved, binding)
    return servers


def authorize_visible(
    resolved: ResolvedConnection,
    binding: ThreadBinding,
    *,
    kind: CapabilityKind,
    name: str,
) -> Capability | None:
    """The capability a call names, if the binding may see it at all."""
    if resolved.snapshot is None:
        return None
    for capability in resolved.snapshot.of_kind(kind):
        if capability.name == name:
            return capability
    return None


__all__ = [
    "MCP_CREDENTIAL_DOMAIN",
    "MCP_INVOCATION_DOMAIN",
    "MCP_OAUTH_DOMAIN",
    "McpRegistryError",
    "ResolvedConnection",
    "authorize_visible",
    "binding_from_row",
    "connection_from_row",
    "invocation_cipher",
    "mcp_cipher",
    "oauth_cipher",
    "reachable_tools",
    "resolve_connection",
    "resolve_for_thread",
    "server_for",
    "servers_for_thread",
    "snapshot_from_row",
]
