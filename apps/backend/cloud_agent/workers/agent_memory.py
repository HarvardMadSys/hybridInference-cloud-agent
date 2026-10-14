"""Forget archived durable memories once their retention period ends."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports.agent_memory import AgentMemoryRepository

logger = get_logger(__name__)


async def maintain_agent_memories(
    repository: AgentMemoryRepository, *, interval_seconds: float = 3600.0
) -> None:
    """Purge expired archives periodically."""
    while True:
        try:
            await repository.purge_memories()
        except Exception:
            # Memory is optional context. A failed pass must not stop later ones.
            logger.warning("agent_memory_maintenance_failed")
        await asyncio.sleep(interval_seconds)
