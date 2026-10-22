"""Event-idempotency lookup only costs a second statement on a replay."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_agent.storage._sql.events import insert_event


async def test_first_delivery_inserts_without_a_dedupe_read():
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=41), execute=AsyncMock())

    event_id = await insert_event(
        connection,
        job_id="ajob_1",
        attempt_id=7,
        event_type="tool_use",
        payload={"name": "ls"},
        client_seq=3,
    )

    assert event_id == 41
    connection.fetchval.assert_awaited_once()
    sql = connection.fetchval.await_args.args[0]
    assert "ON CONFLICT (attempt_id, client_seq)" in sql
    assert "WHERE client_seq IS NOT NULL DO NOTHING" in sql
    connection.execute.assert_not_awaited()


async def test_replay_reads_the_stored_id_and_skips_event_side_effects():
    connection = SimpleNamespace(fetchval=AsyncMock(side_effect=[None, 41]), execute=AsyncMock())

    event_id = await insert_event(
        connection,
        job_id="ajob_1",
        attempt_id=7,
        event_type="tool_use",
        payload={"name": "ls"},
        client_seq=3,
    )

    assert event_id == 41
    assert connection.fetchval.await_count == 2
    assert "SELECT id FROM agent_job_events" in connection.fetchval.await_args.args[0]
    connection.execute.assert_not_awaited()
