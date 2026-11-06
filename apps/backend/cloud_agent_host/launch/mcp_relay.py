"""The MCP servers a member reaches through this host's MCP relay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, override

from cloud_agent_host.launch.facet import LaunchFacet
from cloud_agent_shared.shared_protocol import is_residency_mcp_credential

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class McpRelayFacet(LaunchFacet):
    """The relay's server ids, its address, and the member's MCP credential.

    All three are fixed at process start, but a credential-less probe may
    borrow a process that holds its residency's credential: that credential
    follows the residency, which the supervisor checks beside this.
    """

    server_ids: tuple[str, ...]
    base_url: str
    credential: str = field(repr=False)

    @override
    def fingerprint(self) -> Mapping[str, object]:
        # A launch without tools has no relay to compare.
        return {"mcp": (self.server_ids, self.base_url if self.server_ids else "")}

    @override
    def differs_from(self, held: Self) -> tuple[str, ...]:
        if names := super().differs_from(held):
            return names
        borrows = self.credential == held.credential or (
            not self.credential and is_residency_mcp_credential(held.credential)
        )
        return ("mcp",) if self.server_ids and not borrows else ()

    @override
    def secrets(self) -> dict[str, str]:
        return {"mcp_credential": self.credential} if self.credential else {}
