"""Notification-state bootstrap pages preserve their bounded baseline contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cloud_agent.notification_state import NotificationStateCursor
from cloud_agent.storage import notification_state as notification_state_storage
from cloud_agent.storage.notification_state import _bootstrap, read_notification_state


@pytest.mark.asyncio
async def test_bootstrap_uses_request_baseline_and_bounds_both_candidate_reads():
    """Response delay cannot move the epoch; each source reads at most one page."""
    queries: list[str] = []
    bootstrap_at = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    class Connection:
        async def fetch(self, query, *_args):
            if "SELECT id FROM agent_jobs" in query:
                queries.append("active")
                assert _args[-1] == 201
                return []
            if "FROM agent_notification_changes" in query:
                queries.append("recent")
                assert _args[-1] == 201
                return []
            raise AssertionError(query)

    cursor = NotificationStateCursor(
        scope="test-scope",
        database_snapshot="10:20:",
        bootstrap=True,
        bootstrap_at=bootstrap_at,
        bootstrap_phase="active",
    )

    page = await _bootstrap(Connection(), cursor, "owner", 200)

    assert queries == ["active", "recent"]
    assert page["observed_at"] == bootstrap_at
    assert page["next_cursor"] is None
    assert page["change_cursor"]


@pytest.mark.asyncio
async def test_initial_read_does_not_run_notification_maintenance(monkeypatch):
    """Bootstrap stays independent of retention work and its failure modes."""
    events: list[str] = []
    bootstrap_at = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    class Transaction:
        async def __aenter__(self):
            events.append("transaction")

        async def __aexit__(self, *_args):
            return False

    class Connection:
        async def fetchval(self, query):
            if query == "SELECT clock_timestamp()":
                events.append("clock")
                return bootstrap_at
            if query == "SELECT pg_current_snapshot()::text":
                events.append("snapshot")
                return "10:20:"
            raise AssertionError(query)

        async def execute(self, query):
            assert query == "SET LOCAL statement_timeout = '5s'"
            events.append("timeout")

        def transaction(self, *, isolation):
            assert isolation == "repeatable_read"
            return Transaction()

    connection = Connection()

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    async def bootstrap(conn, cursor, user_id, limit):
        assert conn is connection
        assert cursor.bootstrap_at == bootstrap_at
        assert user_id == "owner"
        assert limit == 200
        events.append("bootstrap")
        return {"observed_at": cursor.bootstrap_at}

    monkeypatch.setattr(notification_state_storage, "_bootstrap", bootstrap)

    page = await read_notification_state(Pool(), user_id="owner", cursor_value=None, limit=200)

    assert page["observed_at"] == bootstrap_at
    assert events == ["clock", "transaction", "timeout", "snapshot", "bootstrap"]
