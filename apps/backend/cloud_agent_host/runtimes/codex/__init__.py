"""Codex: everything the platform knows about this one CLI.

``adapter`` builds the app-server launch; ``app_server`` reads its
notifications; ``session`` is its live session; ``mcp`` and ``plugins`` are its
MCP relay policy and plugin installer; ``member`` prepares a member's private
home, defines its launch steps, and recovers a native Fork from its rollouts.

Nothing is imported here: ``session``, ``plugins`` and ``member`` build on
:mod:`cloud_agent_host.drivers`, which imports this package's parent, so each
module is imported by its own name.
"""
