"""The migrated flows, run against the real assembly rather than a stand-in.

Design §8.3 asks the migration to keep the real providers and the real
`Storage` and swap only the database connection. These tests do exactly that:
`Storage` is the object `lifespan` builds, over a pool that answers nothing and
records what it was asked. What they check is the seam R4 moved code across —
that a flow which used to reach its capability through a name in some other
module still reaches it through the assembled repository, and refuses in the
same words when the answer is "nothing".

The failure this is shaped after is the one §2.5 registered: code asking the
assembly for a capability by a name the assembly does not have. That is
invisible to a fake built to match the caller, and loud here.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import pytest
from fastapi import HTTPException

from cloud_agent.application import job_admission, session_fork
from cloud_agent.storage.assembly import Storage
from cloud_agent.workers.attempts import reap_expired_attempts
from cloud_agent.workers.snapshots import _capture_due_shared_snapshot


class _RecordingConnection:
    """Enough of an asyncpg connection to take a query and answer nothing."""

    def __init__(self, queries: list[str]) -> None:
        self._queries = queries

    def transaction(self) -> Any:
        return _NullTransaction()

    async def fetchrow(self, sql: str, *args: Any) -> None:
        self._queries.append(sql)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._queries.append(sql)
        return []

    async def fetchval(self, sql: str, *args: Any) -> None:
        self._queries.append(sql)

    async def execute(self, sql: str, *args: Any) -> str:
        self._queries.append(sql)
        return "SELECT 0"


class _NullTransaction:
    async def __aenter__(self) -> _NullTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingAcquire:
    def __init__(self, queries: list[str]) -> None:
        self._queries = queries

    async def __aenter__(self) -> _RecordingConnection:
        return _RecordingConnection(self._queries)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingPool:
    """A pool that records every statement and returns no rows."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def acquire(self) -> _RecordingAcquire:
        return _RecordingAcquire(self.queries)


def _assembled(pool: _RecordingPool) -> Storage:
    return Storage(cast("Any", pool))


async def test_the_snapshot_pass_claims_through_the_assembled_repository() -> None:
    """The loop asks `store.snapshot`, and the statement is the real claim."""
    pool = _RecordingPool()

    await _capture_due_shared_snapshot(_assembled(pool))

    assert len(pool.queries) == 1, "the pass reached no statement at all"
    assert "agent_sandbox_snapshots" in pool.queries[0], (
        "the snapshot pass did not go through the assembled snapshot repository"
    )


async def test_the_reaper_pass_asks_the_assembled_sandbox_repository() -> None:
    """Started the way the lifespan starts it, and cancelled the same way."""
    pool = _RecordingPool()
    task = asyncio.create_task(
        reap_expired_attempts(_assembled(pool), interval_seconds=0.001, grants=None)
    )
    try:
        for _ in range(200):
            if pool.queries:
                break
            await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert pool.queries, "one reaper pass reached no statement"
    assert "terminal_resume_pending" in pool.queries[0], (
        "the reaper's first pass did not go through the assembled sandbox repository"
    )


async def test_the_fork_flow_refuses_through_the_assembled_host_pool() -> None:
    """An unresolvable host is refused, in the words the handler used."""
    pool = _RecordingPool()
    operation = {"id": "sfo_1", "state": "preparing"}

    with pytest.raises(HTTPException) as refusal:
        await session_fork.materialize_session_fork(
            operation, job_id="ajob_1", job_store=_assembled(pool), grants=None
        )

    assert refusal.value.status_code == 503
    assert refusal.value.detail == {
        "error": {
            "type": "sandbox_host_unavailable",
            "message": "The host retaining this chat is not reachable for native Fork.",
        }
    }
    assert any("agent_jobs" in query for query in pool.queries), (
        "the fork flow resolved its sandbox manager without asking the assembled host pool"
    )


def test_the_assembly_satisfies_every_protocol_r4_declared() -> None:
    """The shape half, for the protocols the flows declare."""
    storage = _assembled(_RecordingPool())

    for protocol in (job_admission._JobCreationStorage, session_fork._ForkMaterializationStorage):
        missing = [
            attribute for attribute in protocol.__annotations__ if not hasattr(storage, attribute)
        ]
        assert not missing, f"{protocol.__name__} reaches for {missing}, which Storage lacks"
