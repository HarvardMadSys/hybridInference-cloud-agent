"""The Command Executions read's overall deadline and pool hand-back.

A per-statement timeout is not an overall one: a read that is slow across
several statements, or that never gets a connection, must still fail
recoverably and leave the pool usable.
"""

from __future__ import annotations

import asyncio

import pytest

from cloud_agent.storage.repositories import command_execution_queries as queries
from cloud_agent.storage.repositories.command_execution_queries import (
    CommandReadTimeout,
    PostgresCommandExecutionQueries,
)


class _SlowConnection:
    async def execute(self, *_args):
        return None

    async def fetch(self, *_args):
        await asyncio.sleep(5)
        return []

    async def fetchval(self, *_args):
        await asyncio.sleep(5)
        return 0

    def transaction(self):
        return _Context()

    def __await__(self):
        async def _return():
            return self

        return _return().__await__()


class _Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _SlowPool:
    def __init__(self) -> None:
        self.released = 0

    def acquire(self):
        outer = self

        class _Acquire:
            async def __aenter__(self_inner):
                return _SlowConnection()

            async def __aexit__(self_inner, *_exc):
                outer.released += 1
                return False

        return _Acquire()


@pytest.mark.asyncio
async def test_the_whole_read_is_bounded_not_each_statement(monkeypatch):
    monkeypatch.setattr(queries, "COMMAND_READ_TIMEOUT_S", 0.2)
    pool = _SlowPool()
    read = PostgresCommandExecutionQueries(pool)
    with pytest.raises(CommandReadTimeout):
        await read.command_executions(job_id="j", user_id="u")
    # The connection was handed back, so the next read can use the pool.
    assert pool.released == 1


@pytest.mark.asyncio
async def test_a_read_that_gets_no_connection_also_times_out(monkeypatch):
    monkeypatch.setattr(queries, "COMMAND_READ_TIMEOUT_S", 0.1)

    class _BlockedPool:
        def acquire(self):
            class _Blocked:
                async def __aenter__(self_inner):
                    await asyncio.sleep(5)

                async def __aexit__(self_inner, *_exc):
                    return False

            return _Blocked()

    with pytest.raises(CommandReadTimeout):
        await PostgresCommandExecutionQueries(_BlockedPool()).command_executions(
            job_id="j", user_id="u"
        )
