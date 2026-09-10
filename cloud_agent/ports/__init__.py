"""Ports the control plane depends on, declared by the layer that calls them.

Each port is a :class:`typing.Protocol` naming one domain's persistence
operations. ``cloud_agent.storage`` implements them; nothing here imports
``cloud_agent.storage``, so the dependency points inward and a caller can be
handed any object with the right shape.
"""

from __future__ import annotations

from cloud_agent.ports.grants import GrantRepository
from cloud_agent.ports.host_pool import HostPoolRepository
from cloud_agent.ports.ledger import LedgerRepository
from cloud_agent.ports.mcp import McpConnectionRepository
from cloud_agent.ports.source_control import SourceControlRepository

__all__ = [
    "GrantRepository",
    "HostPoolRepository",
    "LedgerRepository",
    "McpConnectionRepository",
    "SourceControlRepository",
]
