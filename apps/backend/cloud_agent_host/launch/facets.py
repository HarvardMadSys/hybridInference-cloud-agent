"""A launch's facets, and the two orders they are used in.

**Comparison order** is the order :func:`launch_facets` returns them in. The
names a refused reuse reports are recorded in that order, between the
launch's own identity fields and its slot
(``ResidentTransport.incompatible_fields``).

**Preparation order** is :data:`PREPARE_ORDER`. The browser service comes
first, for every runtime. Sandbox MCP admission follows, and starts the
gateway before the slot's runner, as the sandbox MCP design requires. The
runtime's own helpers (:class:`RuntimeFacet`) depend on the CLI, so its
package installs them; the compatibility helper comes last.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.launch.browser import BrowserFacet
from cloud_agent_host.launch.environment import EnvironmentFacet
from cloud_agent_host.launch.github_relay import GitHubRelayFacet
from cloud_agent_host.launch.inference import InferenceFacet
from cloud_agent_host.launch.mcp_relay import McpRelayFacet
from cloud_agent_host.launch.plugins import PluginsFacet
from cloud_agent_host.launch.runtime import RuntimeFacet
from cloud_agent_host.launch.sandbox_mcp import SandboxMcpFacet

if TYPE_CHECKING:
    from cloud_agent_host.launch.facet import LaunchFacet
    from cloud_agent_host.resident_transport import ResidentLaunch

#: The trusted steps before a member's first spawn, in the order they run.
PREPARE_ORDER: tuple[type[LaunchFacet], ...] = (
    BrowserFacet,
    SandboxMcpFacet,
    RuntimeFacet,
    InferenceFacet,
)


def launch_facets(launch: ResidentLaunch) -> tuple[LaunchFacet, ...]:
    """Every capability ``launch`` carries, one facet each, in comparison order."""
    return (
        PluginsFacet(plugins=launch.plugins),
        InferenceFacet(
            credential=launch.credential,
            base_url=launch.inference_base_url,
            protocol=launch.inference_protocol,
            source=launch.inference_source,
            client_protocol=launch.inference_client_protocol,
            local_token=launch.inference_local_token,
        ),
        McpRelayFacet(
            server_ids=launch.mcp_server_ids,
            base_url=launch.mcp_base_url,
            credential=launch.mcp_credential,
        ),
        SandboxMcpFacet(
            servers=launch.sandbox_mcp_servers,
            relay_credential=launch.mcp_credential,
            relay_url=launch.mcp_base_url,
            environment=launch.environment,
            thread_id=launch.thread_id,
        ),
        GitHubRelayFacet(
            credential=launch.github_credential,
            repo=launch.github_repo,
            author_name=launch.git_author_name,
            author_email=launch.git_author_email,
            coauthor=launch.git_coauthor,
        ),
        EnvironmentFacet(environment=launch.environment, slot_id=launch.slot_id),
        BrowserFacet(),
        RuntimeFacet(
            runtime_id=launch.runtime_id,
            runtime_version=launch.runtime_version,
            mcp_server_ids=launch.mcp_server_ids,
            mcp_base_url=launch.mcp_base_url,
        ),
    )


def facet_differences(
    held: tuple[LaunchFacet, ...], offered: tuple[LaunchFacet, ...]
) -> tuple[str, ...]:
    """The names keeping ``offered`` from borrowing ``held``'s process, each once, in order.

    Two facets may report under one name (the relay's and the sandbox's MCP
    servers are both ``mcp``); it is reported once.
    """
    names: dict[str, None] = {}
    for mine, theirs in zip(offered, held, strict=True):
        names.update(dict.fromkeys(mine.differs_from(theirs)))
    return tuple(names)
