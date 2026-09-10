"""Query budget for archive project rows, which refresh independently of facets."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_agent.storage.store import AgentJobStore


@pytest.mark.asyncio
@pytest.mark.parametrize("include_summary, queries", [(False, 1), (True, 2)])
async def test_session_summary_is_only_queried_when_requested(include_summary: bool, queries: int):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn

    result = await AgentJobStore(pool).list_sessions(
        user_id="owner", repo="owner/project", archived=True, include_summary=include_summary
    )

    assert conn.fetch.await_count == queries
    assert result["sessions"] == []
    assert result["total"] == (0 if include_summary else None)
    assert result["projects"] == ([] if include_summary else None)
