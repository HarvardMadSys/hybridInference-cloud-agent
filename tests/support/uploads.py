"""Reading what a conversation's ``/uploads`` holds.

The claim path decides it with the same query under the conversation's lock;
a test asserts on the outcome with this read.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.attachments.models import current_uploads
from cloud_agent.storage._sql.attachments import THREAD_UPLOADS_SQL


async def thread_uploads(pool: Any, *, thread_id: str) -> list[dict[str, Any]]:
    """The newest upload at each path of the conversation."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(THREAD_UPLOADS_SQL, thread_id)
    return current_uploads([dict(row) for row in rows])
