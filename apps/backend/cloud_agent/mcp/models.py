"""The records a generic MCP connection is made of.

The design keeps a deliberate split between what a person *configured* and what
the platform *observed*, because the two change for different reasons and only
one of them may widen what an agent can reach:

- A **connection** is the user's configuration: where the server is, how to
  authenticate, where it runs. Editing it is a ``config_revision`` bump, and a
  security-relevant edit also bumps the ``authorization_epoch`` so that
  sessions bound to the old target stop working until their owner re-approves.
- A **capability snapshot** is what the server said it offers, pinned to the
  ``config_revision`` it was discovered under. A tool that appears in a later
  release therefore cannot inherit an earlier approval; it arrives as a new
  capability revision and is unknown until the owner allows it.
- A **thread binding** is one conversation's use of one connection, holding the
  authorization versions it was granted under together with the exact
  capability names allowed. Narrowing it takes effect immediately, which is why
  the checks read the binding rather than a job's creation-time copy.

Every identifier here is opaque and immutable. Labels are for display only and
never resolve anything: two connections to the same service under different
accounts are distinguished by connection id, not by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

# --------------------------------------------------------------------------
# Enumerations. Stored as text; the values are the database's check constraints.
# --------------------------------------------------------------------------


class Transport(StrEnum):
    """How the platform speaks to one MCP server."""

    #: One endpoint that answers JSON or SSE per request. The current spec.
    STREAMABLE_HTTP = "streamable-http"
    #: The 2024-11-05 shape: an SSE stream that announces a POST endpoint.
    HTTP_SSE = "http-sse"
    #: A child process speaking JSON-RPC on stdin/stdout.
    STDIO = "stdio"


class ExecutionLocation(StrEnum):
    """Where a stdio server actually runs."""

    #: An HTTP endpoint reached directly. Only valid with an HTTP transport.
    REMOTE = "remote"
    #: A platform-managed, isolated worker. Dependencies are pinned at install.
    MANAGED_WORKER = "managed-worker"
    #: A connector the user runs on their own machine or private network.
    USER_CONNECTOR = "user-connector"


class AuthKind(StrEnum):
    """How a connection authenticates to its server."""

    NONE = "none"
    #: A secret the user pasted, injected as one header.
    STATIC = "static"
    #: An authorization-code flow with PKCE, refreshed by the platform.
    OAUTH = "oauth"


class ConnectionStatus(StrEnum):
    """The lifecycle state of a connection, in the owner's terms."""

    #: Saved but never successfully tested.
    DRAFT = "draft"
    #: Tested and usable. Capabilities may still be undiscovered.
    ACTIVE = "active"
    #: The last test or discovery failed. Kept, with its diagnosis.
    ERROR = "error"
    #: Turned off by the owner. Nothing resolves through it.
    DISABLED = "disabled"


class ConnectionErrorKind(StrEnum):
    """Why a connection is not usable, at the granularity a user can act on.

    One value per thing the owner can actually do about it, rather than one per
    failure: "needs login" and "your network cannot reach it" call for
    different actions, while two different TLS errors do not.
    """

    NONE = "none"
    #: The address did not answer, or the name did not resolve.
    UNREACHABLE = "unreachable"
    #: The address resolved to something the egress policy refuses.
    BLOCKED_TARGET = "blocked_target"
    #: The server answered but rejected the credential.
    NEEDS_AUTHORIZATION = "needs_authorization"
    #: OAuth discovery found no way for us to register a client.
    CLIENT_REGISTRATION_REQUIRED = "client_registration_required"
    #: The process or connector would not start.
    LAUNCH_FAILED = "launch_failed"
    #: The server does not speak a protocol version we implement.
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    #: Handshake and authorization worked; some optional capabilities do not.
    PARTIAL_CAPABILITIES = "partial_capabilities"


class ToolPolicy(StrEnum):
    """What happens when an agent calls one tool on one connection."""

    #: Refused before anything leaves the platform.
    DENY = "deny"
    #: Held as a pending invocation until the owner decides.
    CONFIRM = "confirm"
    #: Dispatched without asking, for the life of the binding.
    ALLOW = "allow"


class InvocationStatus(StrEnum):
    """The persisted state of one tool call that may need a decision."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    DISPATCHING = "dispatching"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Dispatch began and the outcome cannot be observed reliably. Never
    #: retried automatically: an external side effect may already have landed.
    OUTCOME_UNKNOWN = "outcome_unknown"


class CapabilityKind(StrEnum):
    """Which of the protocol's three top-level surfaces a capability is on."""

    TOOL = "tool"
    RESOURCE = "resource"
    RESOURCE_TEMPLATE = "resource_template"
    PROMPT = "prompt"


class ConnectorStatus(StrEnum):
    """Whether a user-run connector is currently reachable."""

    PENDING = "pending"
    ONLINE = "online"
    OFFLINE = "offline"
    REVOKED = "revoked"


#: The characters an alias may contain. It becomes a URL path segment on the
#: relay and a prefix on every tool name the connection contributes, so the
#: grammar is the intersection of the two rather than whatever a client sends:
#: a slash would break the path, and anything outside this set would be mangled
#: in a tool name the model then cannot call.
ALIAS_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"

#: Version negotiation outcome for one optional capability.
SupportState = Literal["native", "platform-bridge", "unsupported"]

#: The protocol revisions this platform implements, newest first. The relay
#: negotiates down this list; a server that supports none of them is diagnosed
#: rather than driven with a guessed header.
#:
#: ``2026-07-28`` is a different shape rather than a newer number: it removed
#: the ``initialize`` handshake, protocol sessions, the GET stream and SSE
#: resumability, and carries the version and the client's capabilities in
#: ``_meta`` on every request. A client that speaks only the older shape cannot
#: talk to a server that speaks only the newer one, so the era is detected
#: rather than assumed.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2024-11-05",
)

#: The revision the platform prefers when the server does not choose.
PREFERRED_PROTOCOL_VERSION = "2026-07-28"

#: Revisions that use the per-request metadata shape. Membership is what makes
#: "is this modern" a question with one answer.
STATELESS_PROTOCOL_VERSIONS: frozenset[str] = frozenset({"2026-07-28"})


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderAuth:
    """A static credential injected as one HTTP header.

    The secret itself is never here: it lives encrypted in the credential row
    and is attached at dispatch time, so a connection read by a request handler
    cannot leak it.
    """

    header_name: str = "Authorization"
    #: Rendered as ``f"{scheme} {secret}"`` when non-empty, otherwise the
    #: secret is the whole header value.
    scheme: str = "Bearer"

    def render(self, secret: str) -> dict[str, str]:
        """Build the outbound header for ``secret``."""
        from cloud_agent.mcp.net_policy import credential_headers

        value = f"{self.scheme} {secret}".strip() if self.scheme else secret
        return credential_headers({self.header_name: value})


@dataclass(frozen=True)
class OAuthProfile:
    """The OAuth facts that must survive between requests.

    Everything a flow needs to be *resumed* is here rather than in the flow
    row, because a refresh happens without any browser involved and must not
    depend on a flow that has long been consumed.
    """

    issuer: str | None = None
    #: The protected resource the token is bound to. Sent on both the
    #: authorization and token requests for servers that require it.
    resource: str | None = None
    scopes: tuple[str, ...] = ()
    client_id: str | None = None
    #: Set when the authorization server issued us a secret (a confidential
    #: client). Public clients leave it empty.
    client_auth_method: str = "none"
    #: Where the client registration came from, for diagnostics and for
    #: deciding whether revocation must also delete the registration.
    registration: Literal["preregistered", "cimd", "dcr"] = "dcr"


@dataclass(frozen=True)
class AuthProfile:
    """One connection's authentication, whichever kind it is."""

    kind: AuthKind = AuthKind.NONE
    header: HeaderAuth = field(default_factory=HeaderAuth)
    oauth: OAuthProfile | None = None

    def __post_init__(self) -> None:
        """Refuse a profile that cannot authenticate anything."""
        if self.kind is AuthKind.OAUTH and self.oauth is None:
            raise ValueError("an oauth auth profile must carry its OAuth settings")
        if self.kind is not AuthKind.OAUTH and self.oauth is not None:
            raise ValueError("OAuth settings are only valid on an oauth auth profile")


@dataclass(frozen=True)
class Mount:
    """One read-only resource a managed stdio worker may see."""

    source: str
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class LaunchSpec:
    """How to start a stdio server, as data rather than as a shell string.

    ``argv`` is executed directly, never through a shell, so a package name
    containing ``;`` is a package name. ``env`` carries *references* to secrets
    (``secret://NAME``), not values: the worker resolves them into its own
    environment at start, which is what keeps another connection's secret and
    the control plane's own credentials out of reach.
    """

    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    mounts: tuple[Mount, ...] = ()
    #: The resolved package and version, once a dependency plan exists.
    pinned_package: str | None = None
    pinned_version: str | None = None

    def __post_init__(self) -> None:
        """Refuse a launch spec that would be ambiguous or unsafe to run."""
        if not self.command.strip():
            raise ValueError("a stdio launch spec needs a command")
        if any(not arg for arg in self.args if arg is None):
            raise ValueError("a stdio launch spec cannot carry an empty argument")
        for name in self.env:
            if not name or "=" in name:
                raise ValueError(f"invalid environment variable name: {name!r}")

    def argv(self) -> tuple[str, ...]:
        """The full argument vector, command first."""
        return (self.command, *self.args)


@dataclass(frozen=True)
class ConnectionConfig:
    """The parts of a connection that define *what is being reached*.

    Kept as one value object because every field here participates in the
    ``config_revision`` fingerprint: editing any of them invalidates the
    approvals that were made against the previous target.
    """

    transport: Transport
    location: ExecutionLocation
    url: str | None = None
    launch: LaunchSpec | None = None

    def __post_init__(self) -> None:
        """Refuse configs whose transport and location contradict each other."""
        if self.location is ExecutionLocation.REMOTE:
            if self.transport is Transport.STDIO:
                raise ValueError("a remote execution location cannot use the stdio transport")
            if not self.url:
                raise ValueError("a remote connection needs a URL")
        else:
            if self.transport is not Transport.STDIO:
                raise ValueError("a managed or connector location is only valid for stdio")
            if self.launch is None:
                raise ValueError("a stdio connection needs a launch spec")


@dataclass(frozen=True)
class McpConnection:
    """One user's saved configuration for one MCP server.

    ``connection_id`` is immutable and is what a thread binding names, so
    renaming a connection or connecting the same service twice under different
    accounts cannot move a running session onto a different target.
    """

    connection_id: str
    user_id: str
    label: str
    config: ConnectionConfig
    auth: AuthProfile = field(default_factory=AuthProfile)
    #: A catalogue template this was created from, when one was used. Never
    #: required: a connection with no template is a first-class connection.
    template: str | None = None
    status: ConnectionStatus = ConnectionStatus.DRAFT
    enabled: bool = True
    error_kind: ConnectionErrorKind = ConnectionErrorKind.NONE
    error_detail: str | None = None
    #: The connector a ``user-connector`` connection routes through.
    connector_id: str | None = None
    config_revision: int = 1
    authorization_epoch: int = 1
    oauth_generation: int = 0
    credential_version: int = 1
    capability_revision: int = 1
    #: How long one call to this server may take, in seconds. ``None`` means the
    #: platform's default. Per connection because a repository search and a
    #: single-record fetch are not the same wait, and the design is explicit
    #: that one service's patience must not be every service's limit.
    request_timeout_s: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def usable(self) -> bool:
        """Whether this connection may currently authorize anything."""
        return self.enabled and self.status in (ConnectionStatus.ACTIVE, ConnectionStatus.DRAFT)

    def revision_tuple(self) -> tuple[int, int, int, int, int]:
        """Every version that a decision must be fenced against, in order.

        One call site asking for "the versions" is safer than five call sites
        each remembering a different subset, which is how a stale approval
        survives a reconfiguration.
        """
        return (
            self.config_revision,
            self.authorization_epoch,
            self.oauth_generation,
            self.credential_version,
            self.capability_revision,
        )


@dataclass(frozen=True)
class McpCredential:
    """The stored secret material for one connection.

    Absent for ``none`` and for a connector-local secret: in both cases there
    is nothing on the platform to hold. ``expires_at`` is ``None`` when the
    issuer gave no expiry, which reads as "live until refused" rather than
    "never expires".
    """

    connection_id: str
    access_token_ciphertext: str | None = None
    refresh_token_ciphertext: str | None = None
    client_secret_ciphertext: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    issuer: str | None = None
    resource: str | None = None
    credential_version: int = 1
    #: Whether the issuer rotates the refresh token, and whether it issued a
    #: new one last time. Guessing here loses the ability to refresh at all.
    refresh_rotates: bool = False
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Capability:
    """One thing a server offers, with the schema it was advertised under.

    The description and schema are stored verbatim: they are what the model
    reads, and a change to either is a reason to stop trusting an earlier
    approval rather than a cosmetic update.
    """

    kind: CapabilityKind
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] | None = None
    #: Server-supplied hints. Recorded for display only: the specification is
    #: explicit that they are untrusted, so they never decide authorization.
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """A stable digest of what an approval is bound to."""
        import hashlib
        import json

        payload = json.dumps(
            {
                "kind": self.kind.value,
                "name": self.name,
                "description": self.description,
                "input_schema": self.input_schema,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class CapabilitySnapshot:
    """What one server offered, as observed under one config revision."""

    connection_id: str
    capability_revision: int
    config_revision: int
    protocol_version: str
    server_name: str = ""
    server_version: str = ""
    tools: tuple[Capability, ...] = ()
    resources: tuple[Capability, ...] = ()
    resource_templates: tuple[Capability, ...] = ()
    prompts: tuple[Capability, ...] = ()
    support: Mapping[str, SupportState] = field(default_factory=dict)
    discovered_at: datetime | None = None

    def all_capabilities(self) -> tuple[Capability, ...]:
        """Every capability across the three surfaces."""
        return (*self.tools, *self.resources, *self.resource_templates, *self.prompts)

    def names(self, kind: CapabilityKind) -> list[str]:
        """Advertised names on one surface, sorted."""
        return sorted(capability.name for capability in self.of_kind(kind))

    def of_kind(self, kind: CapabilityKind) -> tuple[Capability, ...]:
        """Every capability of one kind."""
        match kind:
            case CapabilityKind.TOOL:
                return self.tools
            case CapabilityKind.RESOURCE:
                return self.resources
            case CapabilityKind.RESOURCE_TEMPLATE:
                return self.resource_templates
            case CapabilityKind.PROMPT:
                return self.prompts


@dataclass(frozen=True)
class ToolRule:
    """The decision for one capability name on one binding.

    ``fingerprint`` is the capability's digest at the moment it was decided. A
    rule whose fingerprint no longer matches is not an approval that has aged:
    the tool's schema or description changed, and what the owner agreed to is
    not what the server now offers, so the call goes back to them.
    """

    name: str
    policy: ToolPolicy
    fingerprint: str = ""


@dataclass(frozen=True)
class ThreadBinding:
    """One conversation's use of one connection.

    Explicit capability rules override the server's default. Automatically
    connected servers ask before each call; manually restricted selections
    deny omitted capabilities. Neither mode silently approves a new tool.
    """

    thread_id: str
    connection_id: str
    alias: str
    #: Versions this binding was approved under. A call is refused when the
    #: connection has moved past any of them.
    config_revision: int
    authorization_epoch: int
    capability_revision: int
    policy_revision: int = 1
    enabled: bool = True
    #: Use the default policy: advertise discovered capabilities and ask before
    #: each call. Explicit per-capability choices override this default. Also
    #: used by legacy bindings whose owners have not chosen specific tools.
    selection_pending: bool = False
    rules: Mapping[str, ToolRule] = field(default_factory=dict)
    resource_rules: Mapping[str, ToolPolicy] = field(default_factory=dict)
    prompt_rules: Mapping[str, ToolPolicy] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def policy_for(self, kind: CapabilityKind, name: str) -> ToolPolicy:
        """Read an explicit override, then the binding's confirmation or deny default."""
        match kind:
            case CapabilityKind.RESOURCE | CapabilityKind.RESOURCE_TEMPLATE:
                rules = self.resource_rules
            case CapabilityKind.PROMPT:
                rules = self.prompt_rules
            case _:
                rules = {name: rule.policy for name, rule in self.rules.items()}
        policy = rules.get(name)
        if policy is not None:
            return policy
        # Default access advertises capabilities without approving execution.
        # Explicit selections keep their original deny-by-default semantics.
        return ToolPolicy.CONFIRM if self.selection_pending else ToolPolicy.DENY

    def rule_for(self, kind: CapabilityKind, name: str) -> ToolRule | None:
        """The stored rule for one capability, if the owner enabled it."""
        if kind in (CapabilityKind.RESOURCE, CapabilityKind.RESOURCE_TEMPLATE):
            policy = self.resource_rules.get(name)
            return ToolRule(name=name, policy=policy) if policy is not None else None
        if kind is CapabilityKind.PROMPT:
            policy = self.prompt_rules.get(name)
            return ToolRule(name=name, policy=policy) if policy is not None else None
        return self.rules.get(name)

    def fenced_by(self, connection: McpConnection) -> bool:
        """Whether this binding is still valid for ``connection``.

        The check is deliberately one-directional and total: any movement in a
        version the binding recorded invalidates it, including movement that
        might be harmless. Re-approving is cheap; discovering after the fact
        that a session spent a credential against a target its owner did not
        approve is not.
        """
        return (
            self.config_revision == connection.config_revision
            and self.authorization_epoch == connection.authorization_epoch
            and self.capability_revision == connection.capability_revision
        )


@dataclass(frozen=True)
class Invocation:
    """One tool call that the platform may need to hold for a decision."""

    invocation_id: str
    job_id: str
    attempt_id: str
    lease_generation: int
    thread_id: str
    connection_id: str
    capability_name: str
    #: Digest of the arguments, for binding a decision to what was shown.
    arguments_digest: str
    status: InvocationStatus = InvocationStatus.PENDING
    config_revision: int = 1
    authorization_epoch: int = 1
    capability_revision: int = 1
    policy_revision: int = 1
    #: Bumped when the dispatcher claims the call, so a superseded worker
    #: cannot send after a takeover.
    dispatch_generation: int = 0
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    result_summary: str | None = None

    @property
    def open(self) -> bool:
        """Whether the call is still waiting for its owner."""
        return self.status is InvocationStatus.PENDING


@dataclass(frozen=True)
class Connector:
    """A user-run connector that can serve their stdio and private MCP.

    The platform never asks a connector for a shell or a raw socket: it asks
    for one named connection's MCP operations, and the connector decides
    whether that connection is one it was authorized for.
    """

    connector_id: str
    user_id: str
    name: str
    status: ConnectorStatus = ConnectorStatus.PENDING
    connection_ids: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class McpServer:
    """A fully resolved server a job may reach through the relay.

    This is what the registry hands the relay: the endpoint and credential are
    present here and nowhere else, and :meth:`public` is the only rendering a
    sandbox ever sees.
    """

    name: str
    url: str | None
    connection_id: str
    transport: Transport
    headers: Mapping[str, str] = field(default_factory=dict)
    tools: frozenset[str] = frozenset()
    #: The resource URIs and prompt names this session enabled. Kept beside the
    #: tools rather than derived from them, because the design gives the three
    #: surfaces their own scopes: a session that turned a resource off must not
    #: reach it just because the tools beside it are reachable.
    resources: frozenset[str] = frozenset()
    prompts: frozenset[str] = frozenset()
    description: str = ""
    protocol_version: str = PREFERRED_PROTOCOL_VERSION
    #: The bound on one call, in seconds, or ``None`` for the relay's default.
    request_timeout_s: float | None = None
    #: Where a stdio server runs, for the relay's routing decision.
    location: ExecutionLocation = ExecutionLocation.REMOTE
    launch: LaunchSpec | None = None
    connector_id: str | None = None

    @property
    def unfiltered(self) -> bool:
        """Whether every tool this server exposes is reachable.

        Always false. A connection's reachable set is its session binding's
        decision, and an empty set means *nothing* rather than *everything* --
        which is the opposite of the provider-catalogue model this replaced,
        where an absent allowlist meant the whole tool surface. Keeping the
        property, rather than deleting it, is what lets the filter below stay
        readable while making the new answer unmissable: a server with no
        allowed tools has no reachable tools.
        """
        return False

    def allows_resource(self, uri: str) -> bool:
        """Whether a resource read may reach this server."""
        return uri in self.resources

    def allows_prompt(self, name: str) -> bool:
        """Whether a prompt fetch may reach this server."""
        return name in self.prompts

    def allows(self, tool: str) -> bool:
        """Whether ``tool`` was allowed by this server's binding."""
        return tool in self.tools

    def public(self) -> dict[str, Any]:
        """Render what a job owner may see: never the URL or the headers."""
        return {
            "connection_id": self.connection_id,
            "name": self.name,
            "description": self.description,
            "tools": sorted(self.tools),
            "transport": self.transport.value,
            "location": self.location.value,
        }


@runtime_checkable
class ConnectionStore(Protocol):
    """The storage operations the MCP package needs.

    Declared here rather than imported from ``storage.store`` so that the
    package's logic can be exercised without a database, and so that a caller
    can see the whole contract in one place. The concrete store satisfies it
    structurally; nothing inherits from it.
    """

    async def list_mcp_connections(self, *, user_id: str) -> Sequence[Mapping[str, Any]]:
        """Return every connection this user owns."""
        ...

    async def get_mcp_connection(self, *, connection_id: str) -> Mapping[str, Any] | None:
        """Return one connection by immutable id, whoever owns it."""
        ...


__all__ = [
    "ALIAS_PATTERN",
    "PREFERRED_PROTOCOL_VERSION",
    "STATELESS_PROTOCOL_VERSIONS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "AuthKind",
    "AuthProfile",
    "Capability",
    "CapabilityKind",
    "CapabilitySnapshot",
    "ConnectionConfig",
    "ConnectionErrorKind",
    "ConnectionStatus",
    "Connector",
    "ConnectorStatus",
    "ExecutionLocation",
    "HeaderAuth",
    "Invocation",
    "InvocationStatus",
    "LaunchSpec",
    "McpConnection",
    "McpCredential",
    "McpServer",
    "Mount",
    "OAuthProfile",
    "SupportState",
    "ThreadBinding",
    "ToolPolicy",
    "ToolRule",
    "Transport",
]
