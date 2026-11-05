"""OpenCode and Kilo: the two CLIs the platform drives through its HTTP bridge.

``adapter`` builds the launch they share, and ``opencode`` and ``kilo`` name
each CLI; ``parts`` and ``session`` read the bridge's records into the live
session; ``kilo_mcp`` installs Kilo's patched MCP runtime; ``member`` prepares
a member's private XDG home and clears its server locks.

Nothing is imported here: ``session`` and ``member`` build on
:mod:`cloud_agent_host.drivers`, which imports this package's parent, so each
module is imported by its own name.
"""
