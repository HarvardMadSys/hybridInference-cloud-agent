"""Terminal replay sends distinct first-page and continuation plans."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cloud_agent.storage._sql.terminal_replay import page


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.calls.append((query, args))
        return []


async def test_terminal_replay_uses_a_real_keyset_bound_only_on_continuations() -> None:
    connection = _Connection()
    cursor = (datetime(2026, 1, 2, tzinfo=UTC), "ajob_after")

    await page(connection, after=None, limit=100)
    await page(connection, after=cursor, limit=100)

    first_query, first_args = connection.calls[0]
    continuation_query, continuation_args = connection.calls[1]
    assert "IS NULL OR" not in first_query
    assert "(updated_at, id) >" not in first_query
    assert len(first_args) == 2
    assert "IS NULL OR" not in continuation_query
    assert "(updated_at, id) > ($3::timestamptz, $4::text)" in continuation_query
    assert continuation_args[2:] == cursor
