"""Notification-state page metadata preserves its clock-domain guarantees."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cloud_agent.notification_state import NotificationStateCursor
from cloud_agent.storage.notification_state import _bootstrap


@pytest.mark.asyncio
async def test_bootstrap_samples_observed_time_after_materializing_rows():
    """Query latency must not move the browser's enable epoch into history."""
    events: list[str] = []
    observed_at = datetime(2026, 10, 22, 12, 0, 1, tzinfo=UTC)

    class Connection:
        async def fetch(self, _query, *_args):
            events.append("rows")
            return []

        async def fetchval(self, query):
            assert query == "SELECT clock_timestamp()"
            events.append("clock")
            return observed_at

    cursor = NotificationStateCursor(
        scope="test-scope",
        database_snapshot="10:20:",
        bootstrap=True,
        bootstrap_at=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
    )

    page = await _bootstrap(Connection(), cursor, "owner", 200)

    assert events == ["rows", "clock"]
    assert page["observed_at"] == observed_at
