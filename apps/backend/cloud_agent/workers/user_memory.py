"""Keep personal memory current even when Settings is closed."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports.user_memory import UserMemoryRepository

logger = get_logger(__name__)


async def refresh_user_memories(
    repository: UserMemoryRepository, *, interval_seconds: float = 60.0
) -> None:
    """Refresh a fair bounded batch and recover after temporary storage errors."""
    while True:
        try:
            await repository.refresh_active_users(limit=100)
        except Exception:
            # Memory is optional context. Do not log user content or stop
            # future refreshes because this database operation failed once.
            logger.warning("agent_user_memory_refresh_failed")
        await asyncio.sleep(interval_seconds)
