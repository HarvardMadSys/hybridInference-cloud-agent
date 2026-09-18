"""The admin audit trail, checked through the assembly rather than a stand-in.

`log_admin_action` looked the recorder up on the assembly by name:
``getattr(store, "record_admin_action", None)``. `Storage` has not had that
method since #210 split it into capabilities — the row goes through
``store.audit`` — so the lookup answered "no such method" for a store that had
one, and every admin action since then revoked a machine's credential or
removed a host from the pool without leaving a record of who did it.

The unit tests around `log_admin_action` could not see it: their fake carried
the method flat, the way the assembly used to. These run against the real
composition root and the real repository, over a pool that records what it was
asked to do, so the assertion is that a row reaches Postgres — not that a
method was called on something shaped like the fake.
"""

from __future__ import annotations

from typing import Any, cast

from cloud_agent.api.deps import log_admin_action
from cloud_agent.storage.assembly import Storage


class _RecordingConnection:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._calls = calls

    async def fetchval(self, sql: str, *args: Any) -> int:
        self._calls.append((sql, args))
        return len(self._calls)


class _RecordingAcquire:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._calls = calls

    async def __aenter__(self) -> _RecordingConnection:
        return _RecordingConnection(self._calls)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingPool:
    """Enough of an asyncpg pool for the repository to reach its INSERT."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self) -> _RecordingAcquire:
        return _RecordingAcquire(self.calls)


def _assembled_storage(pool: _RecordingPool) -> Storage:
    return Storage(cast("Any", pool))


async def test_an_admin_action_reaches_the_assembled_trail() -> None:
    """The write this file is named for: assembly in, INSERT out."""
    pool = _RecordingPool()
    storage = _assembled_storage(pool)

    await log_admin_action(
        storage,
        "203.0.113.9",
        "agent.runner_host.drain",
        None,
        {"host": "runner-a"},
        actor_id="admin-1",
    )

    assert len(pool.calls) == 1, (
        "an admin action reached no INSERT — the recorder was looked up on the "
        "assembly, which exposes it as ``store.audit.record_admin_action``"
    )
    sql, args = pool.calls[0]
    assert "INSERT INTO agent_admin_audit_log" in sql
    assert args == (
        "agent.runner_host.drain",
        "203.0.113.9",
        None,
        '{"host": "runner-a"}',
        True,
        "admin-1",
    )


async def test_the_assembled_storage_carries_the_capability_the_helper_needs() -> None:
    """The shape half of the same bug, in the style of the composition root."""
    storage = _assembled_storage(_RecordingPool())

    assert not hasattr(storage, "record_admin_action"), (
        "the assembly grew a top-level recorder; log_admin_action goes through "
        "store.audit and this test exists to notice the shape changing"
    )
    assert hasattr(storage.audit, "record_admin_action")
