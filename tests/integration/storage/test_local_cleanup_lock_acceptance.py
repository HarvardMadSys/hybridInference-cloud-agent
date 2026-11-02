"""Cancellation after PostgreSQL took a cleanup lock, before its result arrives.

The account repository must clean up an acquisition whose outcome its caller
does not yet know. A real connection takes the lock; only delivery of that
result is paused. The pool uses production's no-op reset so asyncpg cannot
silently unlock a leaked session lock and make this regression pass.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import pytest_asyncio
from tests.support.db import assert_pool_idle
from tests.support.local_accounts import db_config
from tests.support.migrations import apply_migrations

from cloud_agent.bootstrap.lifespan import _release_as_is
from cloud_agent.storage._sql.advisory import LOCK_KEY_SQL
from cloud_agent.storage.repositories.accounts import (
    PostgresLocalAccountRepository,
    termination_lock_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ACCOUNT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_TRY_LOCK = f"SELECT pg_try_advisory_lock({LOCK_KEY_SQL})"
_UNLOCK = f"SELECT pg_advisory_unlock({LOCK_KEY_SQL})"


@pytest_asyncio.fixture
async def no_reset_pool() -> AsyncIterator[tuple[asyncpg.Pool, dict[str, Any]]]:
    # db_config refuses non-test names and selects this xdist worker's DB.
    config = db_config()
    try:
        created = await asyncpg.create_pool(**config, min_size=1, max_size=1, reset=_release_as_is)
    except Exception as exc:
        pytest.fail(f"PostgreSQL is required for this dbtest: {type(exc).__name__}")
    try:
        apply_migrations(config)
        # No account rows are needed: termination_lock takes a namespaced
        # advisory lock. This test changes no persisted application data.
        yield created, config
    finally:
        await created.close()


class _PausedConnection:
    """Delegate every operation, but pause delivery of a successful acquisition."""

    def __init__(self, connection, barrier: _PausedPool) -> None:
        self.connection = connection
        self.barrier = barrier

    async def fetchval(self, query: str, *args: Any):
        result = await self.connection.fetchval(query, *args)
        if query == _TRY_LOCK:
            assert result is True
            self.barrier.owner_pid = self.connection.get_server_pid()
            self.barrier.acquired.set()
            await self.barrier.return_result.wait()
        return result

    async def reset(self) -> None:
        await self.connection.reset()

    async def close(self) -> None:
        await self.connection.close()


class _PausedPool:
    """Wrap only a pool this test created, without reaching through repository state."""

    def __init__(self, real_pool: asyncpg.Pool) -> None:
        self.real_pool = real_pool
        self.acquired = asyncio.Event()
        self.return_result = asyncio.Event()
        self.owner_pid: int | None = None

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_PausedConnection]:
        async with self.real_pool.acquire() as connection:
            yield _PausedConnection(connection, self)


async def test_cancellation_before_lock_result_delivery_releases_the_session_lock(no_reset_pool):
    real_pool, config = no_reset_pool
    paused = _PausedPool(real_pool)
    accounts = PostgresLocalAccountRepository(paused)
    key = termination_lock_key(_ACCOUNT_ID)
    body_entered = asyncio.Event()

    async def take_lock() -> None:
        async with accounts.termination_lock(_ACCOUNT_ID) as held:
            assert held
            body_entered.set()

    # A separate connection is essential: session advisory locks are reentrant
    # on the owning backend, so borrowing that connection could hide a leak.
    observer = await asyncpg.connect(**config)
    observer_holds_lock = False
    task = asyncio.create_task(take_lock())
    try:
        async with asyncio.timeout(10):
            await paused.acquired.wait()
            assert paused.owner_pid != observer.get_server_pid()
            assert not body_entered.is_set()
            observer_holds_lock = bool(await observer.fetchval(_TRY_LOCK, key))
            assert not observer_holds_lock, "The server must hold the lock before cancellation"

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not body_entered.is_set(), "Cancellation must precede the context body"
            await assert_pool_idle(real_pool)

            observer_holds_lock = bool(await observer.fetchval(_TRY_LOCK, key))
            assert observer_holds_lock, "Cancelled acquisition leaked its session advisory lock"
    finally:
        paused.return_result.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            if observer_holds_lock:
                await observer.fetchval(_UNLOCK, key)
        finally:
            await observer.close()
