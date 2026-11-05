"""Both relays read one caller fence: the MCP relay's two statements and the GitHub relay's one.

A residency placeholder buys a relay's credential only while its residency is
hot and serving a live, leased, running, unstopped attempt on its own enrolled
host. The MCP relay checks that in two statements and the GitHub relay in one;
the property that a later edit breaks is that they stop agreeing. So these
tests record the statements each relay sends and assert they are built from
the fragments in :mod:`cloud_agent.storage._sql.fences`, and that the relay
repositories spell none of the fence themselves. ``dbtest`` covers what each
predicate does against a real database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cloud_agent.storage._sql import fences
from cloud_agent.storage._sql.mcp_residency import residency_fence_sql
from cloud_agent.storage.repositories import credential_queries, github_relay
from cloud_agent.storage.repositories.credential_queries import PostgresCredentialQueries
from cloud_agent.storage.repositories.github_relay import PostgresGitHubRelayRepository


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.statements.append(sql)
        return {"user_id": "u", "job_id": "ajob_1", "model": "m", "thread_id": "athr_1"}

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.statements.append(sql)
        return []


class _Acquire:
    def __init__(self, conn: _RecordingConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConnection:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self) -> None:
        self.conn = _RecordingConnection()

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


async def _github_statement() -> str:
    pool = _Pool()
    await PostgresGitHubRelayRepository(pool).resolve_github_caller(  # type: ignore[arg-type]
        thread_id="athr_1", residency_generation=3
    )
    [statement] = pool.conn.statements
    return statement


async def _mcp_statements() -> tuple[str, str]:
    pool = _Pool()
    queries = PostgresCredentialQueries(pool)  # type: ignore[arg-type]
    await queries.resolve_residency_attempt(thread_id="athr_1", residency_generation=3)
    await queries.resolve_model_credential(
        job_id="ajob_1", attempt_id=7, lease_generation=2, residency_generation=3
    )
    residency, credential, _bindings = pool.conn.statements
    return residency, credential


@pytest.mark.asyncio
async def test_the_github_relay_reads_the_whole_shared_fence_in_one_statement() -> None:
    statement = await _github_statement()

    assert fences.RESIDENCY_CALLER_FROM in statement
    assert fences.live_residency_caller_sql("$1", "$2") in statement


@pytest.mark.asyncio
async def test_the_mcp_relay_reads_the_same_two_halves_in_two_statements() -> None:
    residency, credential = await _mcp_statements()

    # The residency names its attempt over the same rows, under the same
    # residency half, as the GitHub relay.
    assert fences.RESIDENCY_CALLER_FROM in residency
    assert fences.residency_caller_sql("$1", "$2") in residency
    assert fences.CURRENT_RUNNING_ATTEMPT in residency
    # The attempt is then live by the same definition, and the residency is
    # re-read from the same predicate.
    assert fences.LIVE_ATTEMPT in credential
    assert residency_fence_sql("$4") in credential
    assert fences.hot_residency_sql("$4") in residency_fence_sql("$4")


def test_the_one_statement_fence_is_exactly_the_two_halves() -> None:
    whole = fences.live_residency_caller_sql("$1", "$2")

    assert fences.residency_caller_sql("$1", "$2") in whole
    assert fences.LIVE_ATTEMPT in whole
    assert fences.CURRENT_RUNNING_ATTEMPT in fences.LIVE_ATTEMPT
    assert fences.hot_residency_sql("$2") in fences.residency_caller_sql("$1", "$2")


@pytest.mark.parametrize("module", [credential_queries, github_relay])
@pytest.mark.parametrize(
    "predicate",
    [
        "residency_status",
        "residency_lease_expires_at",
        "deleted_at IS NULL",
        "a.host_id = g.host",
        "a.status =",
        "a.lease_expires_at",
        "current_attempt_id",
        "j.state =",
        "cancel_requested",
        "LIVE_ATTEMPT_HOST",
    ],
)
def test_no_relay_repository_spells_the_fence_itself(module: Any, predicate: str) -> None:
    assert predicate not in Path(module.__file__).read_text()
