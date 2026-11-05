"""Reading a job's usage inputs the way the owner's usage read does.

``job_usage_reads`` assembles a job's usage from these reads, inside one
snapshot. A test about one part of it reads that part on its own, through the
same function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_agent.storage._sql.ledger import fetch_job_usage_inputs

#: A minute in which test runs start and finish.
START = datetime(2026, 9, 9, 10, tzinfo=UTC)
END = START + timedelta(minutes=1)


async def job_usage_inputs(pool: Any, *, job_id: str) -> dict[str, Any]:
    """The runtime usage events of this job's attempts, without sibling turns."""
    async with pool.acquire() as conn:
        return await fetch_job_usage_inputs(conn, job_id)
