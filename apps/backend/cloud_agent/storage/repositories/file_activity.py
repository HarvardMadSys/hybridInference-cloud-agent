"""Owner-scoped reads behind a conversation's File Activity.

One conversation is read by the job the client names — the same key the Context
view uses — and every statement names the owner: a conversation that is not
the caller's, or is deleted, reads as absent rather than as empty.

The scan follows the conversation's whole event ledger in pages of
:data:`EVENT_PAGE` rows, with three bounds that are reported rather than
hidden: :data:`MAX_SCAN_EVENTS` stored rows in total, the projection's own text
bound per record, and :data:`READ_DEADLINE_S` of wall time for the whole read.
A capped read says so on its coverage, so a caller can never label what it
holds the conversation's whole history, and a read that runs out of time
reports the events it did scan.

Nothing here writes, and nothing is derived at send, settlement, cancellation
or fork time: the read happens when a page asks for it.
"""

from __future__ import annotations

import asyncio
import time
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
    FILE_ACTIVITY_CWD_SQL,
    FILE_ACTIVITY_EVENT_SQL,
)
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg

#: Wall time one whole File Activity read may take: acquiring a connection,
#: resolving the conversation's owner, every page of the scan and the coverage
#: statement. A per-statement timeout alone is not enough — a scan can issue
#: many pages while holding one pooled connection, and a final page could begin
#: just inside the deadline and run its full timeout past it. Each statement's
#: timeout is therefore clipped to the time left, and the whole read is wrapped
#: in one cancellation scope.
READ_DEADLINE_S = 8.0
#: A recorded result that is JSON and carries an image content block, in any
#: key order and with or without a text block before it. Escaped quotes inside
#: a JSON string do not match, so a document that merely mentions an image
#: block is not image evidence.
IMAGE_BLOCK_PATTERN = r'(?:^\s*[\[{])[\s\S]*"type"\s*:\s*"image"'
#: A page's own timeout, clipped further by the deadline's remaining time.
STATEMENT_TIMEOUT_MS = 5_000


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

    async def _owned_thread(self, job_id: str, user_id: str, deadline: float) -> str | None:
        """The caller's conversation, resolved under the read's own deadline."""
        async with asyncio.timeout_at(deadline):
            async with self._pool.acquire() as conn:
                return await self._thread_id(conn, job_id, user_id)

    async def _scan(
        self,
        conn: Any,
        *,
        thread_id: str,
        user_id: str,
        preview_chars: int,
        deadline: float,
    ) -> ScanResult:
        """Every recorded file operation of the conversation, within the bounds.

        ``deadline`` is a :func:`time.monotonic` instant. The loop checks it
        before each page, so a ledger long enough to exhaust it stops with the
        rows already read, exactly like the row cap: the view then reports that
        it holds a bounded window rather than pretending to be complete.
        """
        rows: list[dict[str, Any]] = []
        # The window is the newest operations: pages descend from the newest
        # stored event, so a conversation longer than the bound keeps its
        # recent activity (and Latest sorting stays meaningful). The rows are
        # reversed into ledger order before reducing, so identity and ordering
        # are the reducer's usual ones.
        before: int | None = None
        capped = False
        while time.monotonic() < deadline:
            # Never ask for more than the bound allows, so a single page can
            # not overshoot it.
            want = min(EVENT_PAGE, MAX_SCAN_EVENTS - len(rows))
            if want <= 0:
                capped = True
                break
            page = await conn.fetch(
                FILE_ACTIVITY_EVENT_SQL,
                thread_id,
                user_id,
                preview_chars,
                before if before is not None else 2**63 - 1,
                want,
                IMAGE_BLOCK_PATTERN,
            )
            if not page:
                break
            rows.extend(_decoded(row) for row in page)
            before = int(page[-1]["id"])
            if len(page) < want:
                break
            if len(rows) >= MAX_SCAN_EVENTS:
                capped = True
                break
        else:
            # The deadline passed between pages: the scan is incomplete.
            capped = True
        rows.reverse()
        scanned = len(rows)
        if rows:
            attempt_ids = sorted({int(row["attempt_id"]) for row in rows})
            cwds = await conn.fetch(FILE_ACTIVITY_CWD_SQL, thread_id, user_id, attempt_ids)
            for row in cwds:
                rows.insert(
                    0,
                    {
                        "event_type": "lifecycle",
                        "attempt_id": int(row["attempt_id"]),
                        "payload": {
                            "id": 0,
                            "attempt_id": int(row["attempt_id"]),
                            "cwd": row["cwd"],
                        },
                    },
                )
        result = scan_operations(rows, thread_id=thread_id)
        # The synthetic cwd rows are read to resolve paths, not scanned events.
        result.events_scanned = scanned
        result.capped = capped
        if time.monotonic() < deadline:
            coverage = await conn.fetchrow(FILE_ACTIVITY_COVERAGE_SQL, thread_id, user_id)
            if coverage is not None:
                result.turns = int(coverage["turns"])
                result.attempts = int(coverage["attempts"])
        if not result.turns:
            result.turns = len({str(row["job_id"]) for row in rows if row.get("job_id")})
        if not result.attempts:
            result.attempts = len({int(row["attempt_id"]) for row in rows})
        return result

    async def _read(
        self, conn: Any, *, thread_id: str, user_id: str, preview_chars: int, deadline: float
    ) -> ScanResult:
        """One scan with the read's own statement timeout and the shared deadline."""
        left_ms = max(1, int((deadline - time.monotonic()) * 1000))
        await conn.execute(f"SET LOCAL statement_timeout = {min(STATEMENT_TIMEOUT_MS, left_ms)}")
        return await self._scan(
            conn,
            thread_id=thread_id,
            user_id=user_id,
            preview_chars=preview_chars,
            deadline=deadline,
        )

    async def _read_scoped(
        self, *, thread_id: str, user_id: str, preview_chars: int, deadline: float
    ) -> ScanResult:
        """The read with its own pooled connection, deadline and cancellation.

        The deadline starts before the connection is acquired, so a busy pool
        counts against it; the read runs inside one cancellation scope, so a
        client disconnect or a timeout releases the connection instead of
        letting the scan finish in the background.
        """
        async with asyncio.timeout_at(deadline):
            async with self._pool.acquire() as conn, conn.transaction():
                return await self._read(
                    conn,
                    thread_id=thread_id,
                    user_id=user_id,
                    preview_chars=preview_chars,
                    deadline=deadline,
                )

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
        deadline = time.monotonic() + READ_DEADLINE_S
        thread_id = await self._owned_thread(job_id, user_id, deadline)
        if thread_id is None:
            return None
        result = await self._read_scoped(
            thread_id=thread_id,
            user_id=user_id,
            preview_chars=MAX_RECORDED_TEXT,
            deadline=deadline,
        )
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
        deadline = time.monotonic() + READ_DEADLINE_S
        thread_id = await self._owned_thread(job_id, user_id, deadline)
        if thread_id is None:
            return None
        result = await self._read_scoped(
            thread_id=thread_id,
            user_id=user_id,
            preview_chars=MAX_RECORDED_TEXT,
            deadline=deadline,
        )
        return build_operations_page(
            thread_id=thread_id,
            path=path,
            operations=result.operations,
            cursor=parsed,
            limit=limit,
            scope=scope,
        )
