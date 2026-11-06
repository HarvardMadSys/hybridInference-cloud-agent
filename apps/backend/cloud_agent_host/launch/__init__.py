"""What each platform capability adds to one member's CLI launch.

A :class:`~cloud_agent_host.resident_transport.ResidentLaunch` is the flat,
process-fixed record the runner and the sandbox manager exchange. Each
capability it carries -- the owner's model connection, the MCP relay, the
sandbox's own MCP servers, the GitHub relay, the owner's environment, plugins,
the shared browser, and the CLI's own helpers -- reads it as one *facet*
(:mod:`.facet`), one module per capability here. A facet states, for its capability alone, what forces a new
CLI process, what the trusted host does in the container before the CLI
starts, what the CLI's environment gets, and which values it carries that
output must never show. :mod:`.facets` fixes their order.

A facet knows nothing of which CLI runs: how a capability is wired into one
(Claude's ``--mcp-config``, Codex's ``-c``, ``AGENT_MCP_SERVERS``, where
platform instructions go) stays with the runtime adapters and each CLI's
launch steps (:mod:`cloud_agent_host.runtimes.member_launch`); the
:class:`~.runtime.RuntimeFacet` only hands its step to them.
"""
