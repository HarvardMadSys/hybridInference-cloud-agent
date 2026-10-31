"""Owner-scoped command-execution reads for one conversation.

Every statement names the caller, so a conversation that is not theirs (or is
deleted) reads as absent. The reads are bounded: the newest
``MAX_COMMAND_EXECUTIONS`` recorded command calls, the results and polls that
answer them, and a database count of the conversation's total command calls so
a capped subset is reported as one. No command is executed, expanded or
re-derived here, and no schema is added: the projection is a read of the event
ledger the platform already writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.command_executions import (
    DEFAULT_COMMAND_PAGE,
    MAX_COMMAND_EXECUTIONS,
    CommandFilter,
    CommandSort,
    assemble_command_executions,
    command_executions_page,
    poll_tool_names,
)
from cloud_agent.storage._sql.command_executions import (
    CALLS_SQL,
    RESULTS_SQL,
    TOTAL_SQL,
    TURNS_SQL,
)

if TYPE_CHECKING:
    import asyncpg

#: Ids one results statement may look for. Postgres binds an array per query,
#: so the covered identities are read in bounded chunks rather than one huge
#: statement.
RESULTS_CHUNK = 500
#: Every read is bounded in wall time; a large ledger cannot hold a pooled
#: connection for an unbounded period.
COMMAND_READ_TIMEOUT_S = 15.0


class PostgresCommandExecutionQueries:
    """The Command Executions read behind one conversation."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the existing pool."""
        self._pool = pool

    async def command_executions(
        self,
        *,
        job_id: str,
        user_id: str,
        filter: CommandFilter = "all",
        sort: CommandSort = "latest",
        q: str = "",
        program: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_COMMAND_PAGE,
    ) -> dict[str, Any] | None:
        """The caller's conversation's command executions, filtered and paged.

        ``None`` when the conversation is not the caller's or is deleted.

        Raises:
            ValueError: the cursor is malformed or belongs to another list.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # One optional read never outlives this bound; the setting is LOCAL
            # so it cannot leak to the pooled connection's next user.
            await conn.execute(
                f"SET LOCAL statement_timeout = {int(COMMAND_READ_TIMEOUT_S * 1000)}"
            )
            return await self._read(
                conn,
                job_id=job_id,
                user_id=user_id,
                filter=filter,
                sort=sort,
                q=q,
                program=program,
                cursor=cursor,
                limit=limit,
            )

    async def _read(
        self,
        conn: Any,
        *,
        job_id: str,
        user_id: str,
        filter: CommandFilter,
        sort: CommandSort,
        q: str,
        program: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any] | None:
        turns = [dict(row) for row in await conn.fetch(TURNS_SQL, job_id, user_id)]
        if not turns:
            return None
        calls = [
            dict(row)
            for row in await conn.fetch(CALLS_SQL, job_id, user_id, MAX_COMMAND_EXECUTIONS)
        ]
        total = int(await conn.fetchval(TOTAL_SQL, job_id, user_id) or 0)
        attempt_ids = sorted({int(call["attempt_id"]) for call in calls})
        call_ids = sorted({str(call["call_id"]) for call in calls if call.get("call_id")})
        sessions = sorted({str(call["session_id"]) for call in calls if call.get("session_id")})
        related = await self._results(
            conn,
            attempt_ids=attempt_ids,
            call_ids=call_ids,
            sessions=sessions,
            job_id=job_id,
            user_id=user_id,
        )
        rows = [_call_row(call) for call in calls] + [_related_row(row) for row in related]
        assembly = assemble_command_executions(
            thread_id=str(turns[0].get("thread_id") or ""),
            generated_at=datetime.now(UTC),
            jobs=_jobs(turns),
            rows=rows,
            executions_total=total,
        )
        page = command_executions_page(
            assembly, filter=filter, sort=sort, q=q, program=program, cursor=cursor, limit=limit
        )
        return page.model_dump(mode="json")

    async def _results(
        self,
        conn: Any,
        *,
        attempt_ids: list[int],
        call_ids: list[str],
        sessions: list[str],
        job_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """The results and polls that answer the covered calls, in bounded chunks.

        Only identities the covered calls name are read, so an unrelated result
        in the same attempt cannot crowd a command's own result out of the
        bound. If the total bound is still reached, the caller reports the
        read as incomplete instead of calling its totals complete.
        """
        if not attempt_ids:
            return []
        found: list[dict[str, Any]] = []
        chunk = RESULTS_CHUNK
        ids = call_ids or [""]
        for start in range(0, len(ids), chunk):
            batch = ids[start : start + chunk]
            found.extend(
                dict(row)
                for row in await conn.fetch(
                    RESULTS_SQL,
                    attempt_ids,
                    user_id,
                    job_id,
                    batch,
                    sessions or [""],
                    list(poll_tool_names()),
                    chunk,
                )
            )
        # A poll's own id is covered by the session branch; a chunk boundary may
        # have split a call from its result, so query once more by session only
        # when there is more than one chunk.
        if len(ids) > chunk:
            found.extend(
                dict(row)
                for row in await conn.fetch(
                    RESULTS_SQL,
                    attempt_ids,
                    user_id,
                    job_id,
                    [""],
                    sessions or [""],
                    list(poll_tool_names()),
                    chunk,
                )
            )
        deduped: dict[int, dict[str, Any]] = {}
        for row in found:
            deduped[int(row["event_id"])] = row
        return list(deduped.values())


def _jobs(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation's turns, with the thread id its rows share."""
    thread_id = str(turns[0].get("thread_id") or "")
    return [
        {
            "job_id": str(turn["job_id"]),
            "turn_no": int(turn.get("turn_no") or 0),
            "state": turn.get("state"),
            "runtime": turn.get("runtime"),
            "copied": bool(turn.get("copied")),
            "thread_id": thread_id or str(turn.get("thread_id") or ""),
        }
        for turn in turns
    ]


def _call_row(call: dict[str, Any]) -> dict[str, Any]:
    """The common row shape the assembler reads, from a projected call."""
    duration = call.get("duration_ms")
    return {
        "event_id": int(call["event_id"]),
        "attempt_id": int(call["attempt_id"]),
        "event_type": "tool_use",
        "job_id": str(call["job_id"]),
        "turn_no": int(call.get("turn_no") or 0),
        "attempt_no": int(call.get("attempt_no") or 0),
        "attempt_status": call.get("attempt_status"),
        "runtime": call.get("runtime"),
        "job_state": call.get("job_state"),
        "seq": call.get("seq"),
        "at": call.get("at"),
        "tool_name": call.get("tool_name"),
        "call_id": call.get("call_id"),
        "command_text": call.get("command_text"),
        "command_chars": call.get("command_chars"),
        "cwd": call.get("cwd"),
        "session_id": call.get("session_id"),
        "background": call.get("background"),
        "duration_ms": int(duration) if duration is not None else None,
        "timing_source": call.get("timing_source"),
    }


def _related_row(row: dict[str, Any]) -> dict[str, Any]:
    """A result or poll row in the same shape, with its outcome fields."""
    return {
        "event_id": int(row["event_id"]),
        "attempt_id": int(row["attempt_id"]),
        "event_type": row.get("event_type"),
        "job_id": str(row["job_id"]),
        "seq": row.get("seq"),
        "at": row.get("at"),
        "tool_use_id": row.get("tool_use_id"),
        "call_id": row.get("call_id"),
        "tool_name": row.get("tool_name"),
        "is_error": row.get("is_error"),
        "exit_code": row.get("exit_code"),
        "content": row.get("content"),
        "content_chars": row.get("content_chars"),
        "status": row.get("status"),
        "session_id": row.get("session_id"),
        "background": row.get("background"),
        "structured_output": _structured(row.get("structured_output")),
        "command_text": None,
    }


def _structured(value: Any) -> dict[str, Any] | None:
    """A recorded structured outcome, when the projection kept one."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.startswith("{"):
        try:
            loaded = json.loads(value)
        except ValueError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


__all__ = ["PostgresCommandExecutionQueries"]
