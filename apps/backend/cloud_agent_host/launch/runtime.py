"""The member's CLI itself: its release and the trusted helpers its package installs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from cloud_agent_host.launch.facet import LaunchFacet
from cloud_agent_host.runtimes.live import member_launch

if TYPE_CHECKING:
    from cloud_agent_host.launch.facet import PrepareContext


@dataclass(frozen=True)
class RuntimeFacet(LaunchFacet):
    """What the CLI's helpers depend on; the CLI's package decides which it installs.

    No value of its own forces a new process: the runtime and its version are
    the launch's own identity, and the MCP servers and relay are
    :class:`~cloud_agent_host.launch.mcp_relay.McpRelayFacet`'s.
    """

    runtime_id: str
    runtime_version: str | None
    mcp_server_ids: tuple[str, ...]
    mcp_base_url: str

    @override
    def prepare(self, context: PrepareContext) -> None:
        member_launch(self.runtime_id).install_helpers(context, self)
