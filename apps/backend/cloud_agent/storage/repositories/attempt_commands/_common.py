"""State and the attempt fence shared by the attempt lifecycle's mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.fences import lock_live_attempt

if TYPE_CHECKING:
    import asyncpg


logger = get_logger(__package__)


class AttemptCommandsBase:
    """The pool every mixin writes through, and the live-attempt fence they share."""

    _pool: asyncpg.Pool

    @staticmethod
    async def _lock_live_attempt(conn: Any, attempt_id: int, lease_generation: int | None) -> bool:
        """Lock an attempt first, then test its lease against the post-wait wall clock."""
        return await lock_live_attempt(conn, attempt_id, lease_generation) is not None
