"""Background paging does not truncate an explicit approval-rule flush."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.storage._sql.mcp_maintenance import ALLOW_GROUP_PAGE, pending_allow_groups


@pytest.mark.parametrize("thread_id,connection_id", [(None, None), ("thread", None), (None, 7)])
async def test_only_background_sweeps_are_page_limited(thread_id, connection_id):
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    assert (
        await pending_allow_groups(
            conn, thread_id=thread_id, connection_id=connection_id, after=None
        )
        == []
    )
    expected = ALLOW_GROUP_PAGE if thread_id is None and connection_id is None else None
    assert conn.fetch.call_args.args[-1] == expected
