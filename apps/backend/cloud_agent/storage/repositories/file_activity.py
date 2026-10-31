"""Owner-scoped reads behind a conversation's File Activity.

One conversation is read by the job the client names — the same key the Context
view uses — and every statement names the owner: a conversation that is not
the caller's, or is deleted, reads as absent rather than as empty.

The scan follows the conversation's whole event ledger in pages of
:data:`EVENT_PAGE` rows, with two bounds that are reported rather than hidden:
:data:`MAX_SCAN_EVENTS` stored rows in total, and the projection's own text
bound per record. A capped read says so on its coverage, so a caller can never
label what it holds the conversation's whole history.

Nothing here writes, and nothing is derived at send, settlement, cancellation
or fork time: the read happens when a page asks for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.file_activity.aggregate import (
    build_operations_page,
    build_view,
)
from cloud_agent.file_activity.cursor import CursorError, decode_cursor, list_scope
from cloud_agent.file_activity.models import (
    EVENT_PAGE,
    MAX_RECORDED_TEXT,
    MAX_SCAN_EVENTS,
    FileActivityCursor,
    FileActivityView,
    FileOperationsPage,
    OperationsCursor,
)
from cloud_agent.file_activity.scan import ScanResult, scan_operations
from cloud_agent.storage._sql.file_activity import (
    FILE_ACTIVITY_COVERAGE_SQL,
    FILE_ACTIVITY_EVENT_SQL,
)
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg


def _decoded(row: Any) -> dict[str, Any]:
    """One scan row with its JSONB payload decoded and its time a datetime."""
    payload = load_json(row["payload"])
    return {
        **dict(row),
        "payload": payload if isinstance(payload, dict) else {},
        "created_at": row["created_at"],
    }


class PostgresFileActivityQueries:
    """A conversation's recorded file operations, read on demand and bounded."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the existing pool."""
        self._pool = pool

    async def _thread_id(self, conn: Any, job_id: str, user_id: str) -> str | None:
        """The caller's conversation holding ``job_id``, or None."""
        return await conn.fetchval(
            """
            SELECT j.thread_id
            FROM agent_jobs j
            JOIN agent_threads t ON t.id = j.thread_id
            WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
            """,
            job_id,
            user_id,
        )

    async def _scan(
        self, conn: Any, *, thread_id: str, user_id: str, preview_chars: int
    ) -> ScanResult:
        """Every recorded file operation of the conversation, within the bounds."""
        rows: list[dict[str, Any]] = []
        after = 0
        capped = False
        while True:
            page = await conn.fetch(
                FILE_ACTIVITY_EVENT_SQL,
                thread_id,
                user_id,
                preview_chars,
                after,
                EVENT_PAGE,
            )
            if not page:
                break
            rows.extend(_decoded(row) for row in page)
            after = int(page[-1]["id"])
            if len(page) < EVENT_PAGE:
                break
            if len(rows) >= MAX_SCAN_EVENTS:
                capped = True
                break
        result = scan_operations(rows, thread_id=thread_id)
        result.events_scanned = len(rows)
        result.capped = capped
        coverage = await conn.fetchrow(FILE_ACTIVITY_COVERAGE_SQL, thread_id, user_id)
        if coverage is not None:
            result.turns = int(coverage["turns"])
            result.attempts = int(coverage["attempts"])
        return result

    async def file_activity(
        self,
        *,
        job_id: str,
        user_id: str,
        sort: str,
        filter_key: str,
        q: str,
        cursor: str | None,
        limit: int,
    ) -> FileActivityView | None:
        """One page of the caller's conversation File Activity, or None.

        The cursor is bound to this conversation, sort, filter and search, so a
        page from another list is refused instead of continuing from a
        position that means something else.
        """
        scope = list_scope(thread=job_id, sort=sort, filter=filter_key, q=q, view="files")
        try:
            parsed = decode_cursor(cursor, FileActivityCursor, scope)
        except CursorError as exc:
            raise ValueError(str(exc)) from exc
        async with self._pool.acquire() as conn:
            thread_id = await self._thread_id(conn, job_id, user_id)
            if thread_id is None:
                return None
            await conn.execute("SET statement_timeout = '10s'")
            try:
                result = await self._scan(
                    conn,
                    thread_id=thread_id,
                    user_id=user_id,
                    preview_chars=MAX_RECORDED_TEXT,
                )
            finally:
                await conn.execute("RESET statement_timeout")
        view = build_view(
            thread_id=thread_id,
            operations=result.operations,
            turns=result.turns,
            attempts=result.attempts,
            events_scanned=result.events_scanned,
            capped=result.capped,
            sort=sort,  # type: ignore[arg-type]
            filter_key=filter_key,  # type: ignore[arg-type]
            q=q,
            cursor=parsed,
            limit=limit,
            scope=scope,
        )
        return view

    async def file_operations(
        self,
        *,
        job_id: str,
        user_id: str,
        path: str,
        cursor: str | None,
        limit: int,
    ) -> FileOperationsPage | None:
        """One page of one file's recorded operations, newest first, or None."""
        scope = list_scope(thread=job_id, path=path, view="operations")
        try:
            parsed = decode_cursor(cursor, OperationsCursor, scope)
        except CursorError as exc:
            raise ValueError(str(exc)) from exc
        async with self._pool.acquire() as conn:
            thread_id = await self._thread_id(conn, job_id, user_id)
            if thread_id is None:
                return None
            await conn.execute("SET statement_timeout = '10s'")
            try:
                result = await self._scan(
                    conn,
                    thread_id=thread_id,
                    user_id=user_id,
                    preview_chars=MAX_RECORDED_TEXT,
                )
            finally:
                await conn.execute("RESET statement_timeout")
        return build_operations_page(
            thread_id=thread_id,
            path=path,
            operations=result.operations,
            cursor=parsed,
            limit=limit,
            scope=scope,
        )
