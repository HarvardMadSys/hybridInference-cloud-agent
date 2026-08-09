"""What MCP servers a job may reach — and, for now, the answer is none.

This service used to read a deployment-owned YAML registry: an operator listed
remote MCP servers with their credentials, and every job on the deployment
could use them. That is the shape a *team* MCP configuration has, and it is the
one this product does not want. A server nobody connected is a server nobody
should be able to call, and a credential the deployment supplies is one
identity shared by every user who can create a job — which is a different
product from the one the Integrations page promises, where each person connects
their own accounts.

So the source is gone, not merely unwired. There is no file to point at, no
environment variable to set, and no way for a deployment to re-enable this by
configuration. :func:`get_registry` returns an empty registry, and the composer
therefore offers nothing and job creation refuses any server a caller names.

**What deliberately stays** is everything downstream of the registry: the relay
(``api/mcp_relay.py``), the proxy and its tool filtering (``mcp_proxy.py``), the
``mcp``-scoped sandbox token, and the network boundary that keeps a sandbox from
reaching an MCP upstream directly. Those are not the team-configuration
feature; they are the machinery any MCP support needs, they carry the tests
that prove the sandbox cannot bypass them, and per-user connections will be a
new *source* feeding the same pipe. Deleting them would mean rebuilding — and
re-proving — that boundary later.

The types below are that pipe's interface. :class:`McpServer` is what the proxy
filters against and what the relay resolves to; it stays so both keep compiling
against a real shape rather than a placeholder. Anything that constructs one
today is a test.

See ``docs/`` and the MCP ownership amendment for how MCP got here; the
per-user replacement is tracked separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class McpRegistryError(Exception):
    """Raised when a caller asks for an MCP server that is not on offer."""


@dataclass(frozen=True)
class McpServer:
    """One remote MCP server a job may reach through the relay.

    Nothing constructs this from configuration any more — see the module
    docstring. It remains the shape the proxy filters against, so that a future
    per-user source has something to produce rather than a type to invent.
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
    """Every server reachable right now, by name. Empty in this deployment."""

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

        ``None`` means the job did not choose and takes the defaults; an
        explicit ``[]`` means it wants none. Unknown names raise — a job that
        asked for a tool surface it will not get should fail at creation, not
        discover mid-run that the agent is missing the one capability the task
        depended on. With no servers on offer, every name is unknown, which is
        the refusal this product wants rather than an accident of emptiness.
        """
        if requested is None:
            return self.defaults
        unknown = sorted({name for name in requested if name not in self.servers})
        if unknown:
            known = ", ".join(self.names) or "none — MCP servers are not available yet"
            raise McpRegistryError(
                f"unknown MCP server(s): {', '.join(unknown)}. This deployment offers: {known}"
            )
        return sorted(set(requested))

    def public(self) -> list[dict[str, Any]]:
        """Render the whole registry for the task composer."""
        return [self.servers[name].public() for name in self.names]


#: The one registry this service has: empty, and not configurable.
#:
#: A module-level constant rather than a cached loader, because there is
#: nothing left to load. When per-user connections arrive they will resolve a
#: registry *per caller*, which is a different signature — not this one with a
#: file behind it again.
_EMPTY = McpRegistry()


def get_registry() -> McpRegistry:
    """Return the servers currently on offer, which is none.

    Kept as a function, and kept as the relay's and composer's only way to ask,
    so that per-user resolution replaces one call site rather than being
    threaded through from scratch.
    """
    return _EMPTY


__all__ = [
    "McpRegistry",
    "McpRegistryError",
    "McpServer",
    "get_registry",
]
