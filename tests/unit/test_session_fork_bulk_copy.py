"""Regression coverage for bounded-round-trip session Fork history copies."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from cloud_agent.storage.repositories.session_fork import PostgresSessionForkRepository


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Connection:
    def __init__(
        self,
        *,
        operation: dict[str, Any],
        turns: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> None:
        self.operation = operation
        self.turns = turns
        self.messages = messages
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((sql, args))
        if "SELECT * FROM agent_session_forks" in sql:
            return self.operation
        if "FROM agent_threads" in sql:
            return {
                "repo": "example/repo",
                "title": "Source",
                "setup_script": None,
                "execution_profile": "trusted-v1",
            }
        if "FROM agent_jobs j" in sql:
            return self.turns[-1]
        if "FROM agent_thread_sandbox_memberships" in sql:
            return {
                "fork_operation_id": self.operation["id"],
                "native_session_id": self.operation["source_native_session_id"],
            }
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, args))
        if "FROM agent_thread_messages" in sql:
            return self.messages
        if "FROM agent_jobs j" in sql:
            return self.turns
        raise AssertionError(f"unexpected fetch: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "OK"

    async def executemany(self, sql: str, args: Any) -> None:
        self.executemany_calls.append((sql, list(args)))


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


async def test_finalize_session_fork_batches_a_thousand_turns_and_preserves_lineage() -> None:
    operation = {
        "id": "fork-1",
        "state": "materialized",
        "target_anchor_job_id": None,
        "source_thread_id": "source-thread",
        "source_job_id": "source-job-1000",
        "target_thread_id": "target-thread",
        "user_id": "user-1",
        "source_native_session_id": "native-source",
        "target_native_session_id": "native-target",
        "sandbox_id": "sandbox-1",
        "target_process_generation": 1,
        "target_residency_generation": 2,
        "target_resident_slot_id": 3,
        "source_process_generation": 4,
        "source_residency_generation": 5,
    }
    turns = [
        {
            "id": f"source-job-{turn_no}",
            "turn_no": turn_no,
            "native_turn_no": turn_no,
            "base_sha": f"base-{turn_no}",
            "task_prompt": f"prompt {turn_no}",
            "runtime": "codex",
            "model": "gpt-test",
            "reasoning_effort": "high",
            "state": "succeeded" if turn_no % 2 else "running",
            "published_pr_url": f"https://example.test/pr/{turn_no}",
            "published_commit_sha": f"commit-{turn_no}",
            "detail": f"detail {turn_no}",
            "metadata": {"turn": turn_no},
            "skills": [{"name": f"skill-{turn_no}"}],
        }
        for turn_no in range(1, 1001)
    ]
    messages = [
        {
            "job_id": turn["id"],
            "role": "user",
            "content": turn["task_prompt"],
            "created_at": f"created-{turn['turn_no']}",
            "mid_turn": False,
        }
        for turn in turns
    ]
    connection = _Connection(operation=operation, turns=turns, messages=messages)
    repository = PostgresSessionForkRepository(cast("Any", _Pool(connection)))
    copied_anchor = {"id": "copied-anchor"}
    read_job = AsyncMock(return_value=copied_anchor)

    with patch("cloud_agent.storage.repositories.session_fork.read_job", read_job):
        assert await repository.finalize_session_fork(operation_id=operation["id"]) == copied_anchor

    # Four locked reads, two history reads, two driver batches, two locks
    # (the target conversation, then both members in thread order) and five
    # fixed finalization statements: the connection-await count does not grow
    # with the 1,000-turn history.
    assert len(connection.fetchrow_calls) == 4
    assert len(connection.fetch_calls) == 2
    assert len(connection.execute_calls) == 7
    assert len(connection.executemany_calls) == 2
    assert (
        len(connection.fetchrow_calls)
        + len(connection.fetch_calls)
        + len(connection.execute_calls)
        + len(connection.executemany_calls)
        == 15
    )
    assert [args for _sql, args in connection.execute_calls[:2]] == [
        ("target-thread",),
        (["source-thread", "target-thread"],),
    ]

    turn_sql, copied_turns = connection.executemany_calls[0]
    assert "INSERT INTO agent_jobs" in turn_sql
    assert len(copied_turns) == 1000
    assert len({row[0] for row in copied_turns}) == 1000
    assert copied_turns[0][2] is None
    assert [row[2] for row in copied_turns[1:]] == [row[0] for row in copied_turns[:-1]]
    assert [row[16] for row in copied_turns] == [turn["id"] for turn in turns]
    assert [row[11] for row in copied_turns[0:2]] == ["succeeded", "cancelled"]
    assert copied_turns[-1][1:15] == (
        operation["target_thread_id"],
        copied_turns[-2][0],
        1000,
        1000,
        operation["user_id"],
        "base-1000",
        "prompt 1000",
        "codex",
        "gpt-test",
        "high",
        "cancelled",
        "https://example.test/pr/1000",
        "commit-1000",
        "detail 1000",
    )
    assert json.loads(copied_turns[-1][15]) == {"turn": 1000}
    assert copied_turns[-1][16] == "source-job-1000"
    assert json.loads(copied_turns[-1][17]) == [{"name": "skill-1000"}]

    message_sql, copied_messages = connection.executemany_calls[1]
    assert "INSERT INTO agent_thread_messages" in message_sql
    source_to_copy = {row[16]: row[0] for row in copied_turns}
    assert [row[1] for row in copied_messages] == [
        source_to_copy[message["job_id"]] for message in messages
    ]
    assert copied_messages[-1] == (
        operation["target_thread_id"],
        source_to_copy["source-job-1000"],
        "user",
        "prompt 1000",
        "created-1000",
        False,
    )
    read_job.assert_awaited_once_with(connection, copied_turns[-1][0])
