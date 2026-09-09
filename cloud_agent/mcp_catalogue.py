"""The curated catalogue: which MCP servers a person may connect, and to what.

#48 removed the deployment-wide registry, where an operator listed servers with
their credentials and every user who could create a job could call them. This
is the replacement's first half: a fixed, in-code list of the servers a person
*may* connect their own account to. The second half is the connection itself —
one row per user per server, holding that person's credential.

**Curated, not arbitrary.** A user cannot paste an MCP URL. Cursor allows both,
and we deliberately allow only the first: an arbitrary upstream is not the
shared-identity problem the old registry had, but it is still an unreviewed
server that the proxy would attach a credential to and that an agent — running
untrusted repository content — would be told about. Adding a provider is a code
change, reviewed like one.

**Every entry declares how its tool surface is bounded.** The retired registry
let an operator write ``tools:`` per server and warned when one was left
unfiltered, because a server can grow a write tool in a later release with no
configuration change to notice. In a per-user world there is no operator to
write that list, so the catalogue carries it — and :class:`McpProvider` makes
the choice explicit rather than accidental by requiring :attr:`read_scope`:

``"endpoint"``
    The provider itself serves a read-scoped endpoint, and :attr:`url` points at
    it. Stronger than a list we maintain: a write tool added upstream never
    appears, without us noticing anything.
``"allowlist"``
    We bound it, by name, in :attr:`tools`. For providers that offer one
    endpoint and expect the client to choose.

There is no third option. A provider whose surface is unbounded does not go in
the catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: How a provider's tool surface is kept to what an agent should reach.
ReadScope = Literal["endpoint", "allowlist"]


@dataclass(frozen=True)
class McpProvider:
    """One server a user may connect, and the shape of what they get.

    Holds no credential: the catalogue says what *may* be connected, and a
    connection row says what a given person *has* connected. That separation is
    the whole point — the old registry conflated them, which is how one
    deployment credential ended up serving every user.
    """

    name: str
    label: str
    description: str
    #: The endpoint the relay dials. For ``read_scope="endpoint"`` this is the
    #: provider's read-scoped URL, not its default one.
    url: str
    read_scope: ReadScope
    #: Why this scope is the right bound, in the terms a reviewer needs to check
    #: it. Required: an entry that cannot explain its scope has not been curated.
    scope_note: str
    #: Our own allowlist, applied by the proxy. Empty is only valid when the
    #: provider bounds the surface itself — see :attr:`read_scope`.
    tools: frozenset[str] = frozenset()
    #: Shown in the Integrations row, in the user's terms.
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Reject an entry whose declared scope is not the one it implements."""
        if self.read_scope == "allowlist" and not self.tools:
            raise ValueError(
                f"MCP provider {self.name!r} declares an allowlist scope but lists no tools"
            )
        if self.read_scope == "endpoint" and self.tools:
            raise ValueError(
                f"MCP provider {self.name!r} declares endpoint scope; a tool list there reads "
                "as the bound when the endpoint is, and would silently drop tools the "
                "provider adds"
            )
        if not self.scope_note.strip():
            raise ValueError(f"MCP provider {self.name!r} must explain why its scope is safe")


#: Every server on offer. One entry per reviewed provider; adding one is a code
#: change, and #49 names the candidates (Sentry, Linear, Jira, Notion). Each
#: needs its endpoint and its scope checked against the provider's own
#: documentation before it lands — an entry is a claim about somebody else's
#: service, and a wrong URL fails at the first tool call of a real job.
CATALOGUE: tuple[McpProvider, ...] = (
    McpProvider(
        name="github",
        label="GitHub",
        description="Read issues, pull requests, code and releases across the repositories "
        "your GitHub account can see.",
        # The read-only variant of the default toolset. GitHub documents both
        # `https://api.githubcopilot.com/mcp/` and this one; we pin this one.
        url="https://api.githubcopilot.com/mcp/readonly",
        read_scope="endpoint",
        scope_note=(
            "GitHub serves a read-only variant of every toolset and excludes write tools "
            "from it server-side, so a write tool added in a later release cannot reach an "
            "agent through this URL. That bound is maintained by GitHub against their own "
            "releases, which a name list here could not be."
        ),
        capabilities=("issues", "pull requests", "code search", "releases"),
    ),
)

_BY_NAME: dict[str, McpProvider] = {provider.name: provider for provider in CATALOGUE}


def catalogue() -> tuple[McpProvider, ...]:
    """Every provider a user may connect, in display order."""
    return CATALOGUE


def get_provider(name: str) -> McpProvider | None:
    """Return a catalogue entry by name, or ``None`` if there is no such server.

    ``None`` is a real case rather than a lookup failure: a connection outlives
    the catalogue entry it was made against, so a retired provider leaves rows
    that must read as "not on offer" rather than crash the job that holds one.
    """
    return _BY_NAME.get(name)


def provider_names() -> list[str]:
    """Catalogue names, sorted — for error messages that say what *is* on offer."""
    return sorted(_BY_NAME)


__all__ = [
    "CATALOGUE",
    "McpProvider",
    "ReadScope",
    "catalogue",
    "get_provider",
    "provider_names",
]
