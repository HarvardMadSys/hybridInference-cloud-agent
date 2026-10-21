"""Reading one part of a job's usage the way the owner's usage read does.

``job_usage_reads`` assembles a job's usage from these reads, inside one
snapshot. A test about one part of it reads that part on its own, through the
same function.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.storage._sql.ledger import (
    fetch_job_usage_inputs,
    fetch_ledger_rows_for_conversation,
)


async def conversation_rows(pool: Any, *, job_id: str, user_id: str) -> list[dict[str, Any]]:
    """The owner's ledger rows for every turn of this job's conversation."""
    async with pool.acquire() as conn:
        return await fetch_ledger_rows_for_conversation(conn, job_id, user_id)


async def job_usage_inputs(pool: Any, *, job_id: str) -> dict[str, Any]:
    """The runtime usage events of this job's attempts, without sibling turns."""
    async with pool.acquire() as conn:
        return await fetch_job_usage_inputs(conn, job_id)
