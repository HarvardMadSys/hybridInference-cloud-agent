"""Wake a tool call held for its owner's approval when it is decided.

A decision on a held call notifies ``agent_mcp_decisions`` with the call's id
(migration ``0032``), and the relay process's own listener hands it here. Only
the request holding that call wakes, to read its row again.
"""

from __future__ import annotations

from cloud_agent.workers.notifications import KeyedSignal

CHANNEL = "agent_mcp_decisions"


class McpDecisionSignal(KeyedSignal):
    """Per-call wake-ups for the calls this relay process holds."""

    channel = CHANNEL
