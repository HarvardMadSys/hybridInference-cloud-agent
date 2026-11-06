"""The MCP servers a member runs inside the sandbox, behind its MCP gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

from cloud_agent_host.launch.facet import LaunchFacet
from cloud_agent_host.sandbox_mcp import admit_sandbox_mcp

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_host.launch.facet import PrepareContext
    from cloud_agent_shared.environment import BoundEnvironment
    from cloud_agent_shared.sandbox_mcp import SandboxMcpServer


@dataclass(frozen=True)
class SandboxMcpFacet(LaunchFacet):
    """The servers, and what admitting them takes: the relay's credential and address.

    Reported under the relay's name, ``mcp``: to the CLI both are its MCP
    servers, fixed at process start.
    """

    servers: tuple[SandboxMcpServer, ...]
    # The gateway authenticates the relay's credential, and a server reports
    # through the relay's address. The relay's facet names that credential
    # among its secrets, so this one does not.
    relay_credential: str = field(repr=False)
    relay_url: str
    # The chat's environment, for the servers' ${NAME} references.
    environment: BoundEnvironment | None = field(repr=False)
    # The chat the servers run for, which the gateway names in its incident reports.
    thread_id: str = ""

    @override
    def fingerprint(self) -> Mapping[str, object]:
        return {"mcp": self.servers}

    @override
    def prepare(self, context: PrepareContext) -> None:
        # Its sandbox MCP servers, or the removal of an earlier launch's. The
        # gateway starts before the slot's runner (admit_sandbox_mcp).
        admit_sandbox_mcp(
            context.manager,
            context.spec,
            slot_id=context.slot_id,
            credential=self.relay_credential,
            servers=self.servers,
            environment=self.environment.values("runtime") if self.environment else {},
            mcp_base_url=self.relay_url,
            thread_id=self.thread_id,
        )

    @override
    def secrets(self) -> dict[str, str]:
        return {name: value for server in self.servers for name, value in server.secrets().items()}
