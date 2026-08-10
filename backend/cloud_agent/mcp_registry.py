"""What MCP servers a job may reach: the ones its owner connected, and no others.

This service used to read a deployment-owned YAML registry: an operator listed
remote MCP servers with their credentials, and every job on the deployment
could use them. That is the shape a *team* MCP configuration has, and it is the
one this product does not want. A server nobody connected is a server nobody
should be able to call, and a credential the deployment supplies is one
identity shared by every user who can create a job. #48 removed that source.

This is its replacement: a registry resolved **per user**, from the servers that
person connected their own account to. The catalogue
(``mcp_catalogue.py``) says what may be connected; a row in
``agent_mcp_connections`` says what someone has connected, and holds their
credential. Neither alone can produce a reachable server, which is what keeps
one person's tools out of another person's job.

**The signature is the feature.** Resolution takes a user, and there is no
process-wide registry to ask instead — a call site that has no user has no
answer, and will not compile its way into one. The three that need one are the
composer's config response, job creation, and the relay; the relay resolves
against the *job owner*, not the caller, because the caller is a sandbox.

**What deliberately stays** is everything downstream: the relay
(``api/mcp_relay.py``), the proxy and its tool filtering (``mcp_proxy.py``), the
``mcp``-scoped sandbox token, and the network boundary that keeps a sandbox from
reaching an MCP upstream directly, together with the tests that prove a sandbox
cannot bypass them. Per-user connections are a new *source* feeding that same
pipe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp_catalogue import get_provider
from cloud_agent.source_control import SourceControlCipher, SourceControlError

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

logger = get_logger(__name__)


class McpRegistryError(Exception):
    """Raised when a caller asks for an MCP server that is not on offer."""


@dataclass(frozen=True)
class McpServer:
    """One remote MCP server a job may reach through the relay.

    Built by :func:`registry_for_user` from a catalogue entry plus one person's
    connection: the entry supplies the URL and the tool bound, the connection
    supplies the credential in :attr:`headers`. This is the shape the proxy
    filters against and the relay dials.
    """

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    # An allowlist of tool names. Empty means "whatever the server exposes",
    # which is honest but unbounded: see ``unfiltered``.
    tools: frozenset[str] = frozenset()
    description: str = ""
    # Offered to a job that does not name its servers explicitly.
    default: bool = False

    @property
    def unfiltered(self) -> bool:
        """Whether every tool this server exposes is reachable."""
        return not self.tools

    def allows(self, tool: str) -> bool:
        """Whether ``tool`` may be listed and called through this server."""
        return self.unfiltered or tool in self.tools

    def public(self) -> dict[str, Any]:
        """Render what a job owner may see: never the URL or the headers.

        The upstream address and its credential are the two things this whole
        indirection exists to keep off the sandbox, so they are absent by
        construction rather than filtered at each call site.
        """
        return {
            "name": self.name,
            "description": self.description,
            "tools": sorted(self.tools),
            "default": self.default,
        }


@dataclass(frozen=True)
class McpRegistry:
    """Every server one user can reach right now, by name.

    Always somebody's. There is no deployment-wide instance of this — see
    :func:`registry_for_user`.
    """

    servers: dict[str, McpServer] = field(default_factory=dict)

    def get(self, name: str) -> McpServer | None:
        """Return a reachable server, or ``None``."""
        return self.servers.get(name)

    @property
    def names(self) -> list[str]:
        """Reachable server names, sorted."""
        return sorted(self.servers)

    @property
    def defaults(self) -> list[str]:
        """Servers a job gets when it does not choose for itself."""
        return sorted(name for name, server in self.servers.items() if server.default)

    def resolve(self, requested: list[str] | None) -> list[str]:
        """Turn a job's request into the server list it will actually run with.

        ``None`` means the job did not choose and takes the defaults — which,
        for a per-user registry, is everything its owner connected: connecting a
        server *is* the opt-in, and a job that named nothing gets the tools its
        owner set up. An explicit ``[]`` means it wants none.

        Unknown names raise. A job that asked for a tool surface it will not get
        should fail at creation, not discover mid-run that the agent is missing
        the one capability the task depended on. "Unknown" here means unknown
        *to this user*: a server someone else connected is as unreachable as one
        that does not exist, and says so in the same words.
        """
        if requested is None:
            return self.defaults
        unknown = sorted({name for name in requested if name not in self.servers})
        if unknown:
            connected = ", ".join(self.names) or "none yet"
            raise McpRegistryError(
                f"you have not connected these MCP server(s): {', '.join(unknown)}. "
                f"Connected: {connected}. Connect a server on the Integrations page."
            )
        return sorted(set(requested))

    def public(self) -> list[dict[str, Any]]:
        """Render the whole registry for the task composer."""
        return [self.servers[name].public() for name in self.names]


#: Domain separator for stored MCP credentials. The secret is the one this
#: service already deploys for source control; the domain is not, so an MCP
#: ciphertext cannot be read — or planted — as a GitLab token and vice versa.
#: One key to rotate, two credential kinds that do not interchange.
MCP_CREDENTIAL_DOMAIN = b"hybridinference/mcp-connection/v1"


def mcp_cipher(secret: str | None = None) -> SourceControlCipher:
    """The cipher MCP connection credentials are stored under.

    Raises :class:`~cloud_agent.source_control.SourceControlError` when the
    service has no encryption key, which is the same condition that leaves
    source control unconfigured. Callers that only need to *list* the catalogue
    treat that as "MCP is not configured here" rather than an error, because a
    deployment without the key can still show what is on offer — it just cannot
    hold anybody's credential.
    """
    return SourceControlCipher(secret, domain=MCP_CREDENTIAL_DOMAIN)


def _has_expired(connection: dict[str, Any]) -> bool:
    """Whether the stored credential is one we already know to be dead.

    ``NULL`` means "no expiry we were told about", not "never expires", so it
    reads as live: such a connection fails at its next tool call if the upstream
    has revoked it, which is the best a provider that issues no expiry allows. A
    *known* expiry in the past is a different thing — that token is one we know
    will be refused, offered to the composer as though it worked and then sent
    upstream anyway.

    No refresh margin. Refreshing arrives with the connect flow; until then a
    margin would only disconnect still-valid credentials early.
    """
    expires_at = connection.get("expires_at")
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        # asyncpg returns tz-aware values for TIMESTAMPTZ, so a naive one can
        # only come from a fixture; reading it as UTC matches how it was written.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


@dataclass(frozen=True)
class McpConnectionState:
    """One stored connection, and whether it can currently be used.

    The single answer to "is this connection live", so the composer, job
    creation, the relay and the Integrations page cannot disagree — which they
    would the moment each applied its own subset of the checks. A page saying
    *Connected* beside a picker offering nothing is the shape that produces.
    """

    server: str
    account_label: str | None
    #: The reachable server, or ``None`` when this connection buys nothing.
    resolved: McpServer | None
    #: Why it buys nothing, in the owner's terms. ``None`` when it works.
    error: str | None


async def connection_states(
    store: AgentJobStore,
    *,
    user_id: str,
    cipher: SourceControlCipher | None = None,
) -> list[McpConnectionState]:
    """Read every connection ``user_id`` has: resolved, or with its reason.

    Never raises because a connection cannot be used. A missing encryption key,
    a credential that will not decrypt, an expiry already past, a provider
    withdrawn from the catalogue — each disables *that* connection and leaves
    the others alone.

    The missing key used to be the exception: it propagated, and had to be
    caught identically at every call site. The relay's catch was missing, so a
    key rotated away turned every tool call of every running job into a 500 with
    a stack trace rather than the 503 the relay documents. Handling it here
    means no call site can forget.
    """
    connections = await store.list_mcp_connections(user_id=user_id)
    if not connections:
        return []

    resolved_cipher = cipher
    if resolved_cipher is None:
        try:
            resolved_cipher = mcp_cipher()
        except SourceControlError:
            # Rows exist and not one of them can be opened: a deployment-level
            # fault, and the only failure here that is nobody's connection's
            # own. Logged once rather than per row, and at error, because it is
            # the earliest signal that a key rotation went wrong.
            logger.error(
                "agent_mcp_encryption_key_missing",
                extra={
                    "event": "agent_mcp_encryption_key_missing",
                    "user_id": user_id,
                    "connections": len(connections),
                },
            )
            return [
                McpConnectionState(
                    server=connection["server"],
                    account_label=connection.get("account_label"),
                    resolved=None,
                    error="MCP is not configured on this deployment.",
                )
                for connection in connections
            ]

    states: list[McpConnectionState] = []
    for connection in connections:
        server_name = connection["server"]
        label = connection.get("account_label")
        provider = get_provider(server_name)
        if provider is None:
            # Withdrawn after someone connected it: our decision, not their
            # mistake. Not logged — ordinary operation, not a fault.
            states.append(
                McpConnectionState(
                    server=server_name,
                    account_label=label,
                    resolved=None,
                    error="This server is no longer offered.",
                )
            )
            continue
        if _has_expired(connection):
            states.append(
                McpConnectionState(
                    server=server_name,
                    account_label=label,
                    resolved=None,
                    error="This connection has expired. Connect it again.",
                )
            )
            continue
        try:
            token = resolved_cipher.decrypt(connection["access_token_ciphertext"])
        except SourceControlError:
            # Narrow deliberately: this is the whole contract of `decrypt`, and
            # a broader catch would swallow a genuine bug here as "the user's
            # credential is bad" — the one diagnosis nobody would question.
            # The earliest signal that a re-key left rows behind; without it the
            # first sign is a job failing a tool call much later. The ciphertext
            # is deliberately not in the record.
            logger.warning(
                "agent_mcp_credential_unreadable",
                extra={
                    "event": "agent_mcp_credential_unreadable",
                    "user_id": user_id,
                    "server": server_name,
                },
            )
            states.append(
                McpConnectionState(
                    server=server_name,
                    account_label=label,
                    resolved=None,
                    error="This connection could not be read. Connect it again.",
                )
            )
            continue
        states.append(
            McpConnectionState(
                server=server_name,
                account_label=label,
                resolved=McpServer(
                    name=provider.name,
                    # The user's own credential. It reaches the upstream and
                    # nothing else: `public()` has nowhere to put it, and the
                    # relay strips it from everything travelling back toward
                    # the sandbox.
                    headers={"Authorization": f"Bearer {token}"},
                    url=provider.url,
                    tools=provider.tools,
                    description=provider.description,
                    # Connecting is the opt-in, so everything connected is on
                    # by default for a job that did not choose. See
                    # `McpRegistry.resolve`.
                    default=True,
                ),
                error=None,
            )
        )
    return states


async def registry_for_user(
    store: AgentJobStore,
    *,
    user_id: str,
    cipher: SourceControlCipher | None = None,
) -> McpRegistry:
    """Resolve the servers ``user_id`` can reach right now, with their credentials.

    Two independent inputs, and a server needs both: a catalogue entry, which
    says the provider is on offer and how its tool surface is bounded, and this
    user's connection row, which supplies a credential that still works. A
    connection failing any of those tests is absent rather than fatal, so one
    dead row cannot take out the servers beside it — :func:`connection_states`
    holds which tests those are and what each one reports.
    """
    states = await connection_states(store, user_id=user_id, cipher=cipher)
    return McpRegistry(
        servers={
            state.resolved.name: state.resolved for state in states if state.resolved is not None
        }
    )


__all__ = [
    "MCP_CREDENTIAL_DOMAIN",
    "McpConnectionState",
    "McpRegistry",
    "McpRegistryError",
    "McpServer",
    "connection_states",
    "mcp_cipher",
    "registry_for_user",
]
