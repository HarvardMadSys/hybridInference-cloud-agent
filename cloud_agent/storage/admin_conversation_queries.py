"""Cross-user conversation reads using explicit metadata projections only."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from cloud_agent.admin_conversations import (
    AdminConversation,
    AdminConversationDetail,
    AdminConversationList,
    AdminConversationMatch,
    AdminConversationTurn,
    ConversationCursor,
    ConversationTurnsCursor,
)
from cloud_agent.admin_observability import RunFilters, cursor_scope, encode_cursor
from cloud_agent.storage._sql.jobs import CONVERSATION_STATE_SQL
from cloud_agent.storage.admin_queries import _RUN_COLUMNS, _fold_run_resource_summary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    import asyncpg


# Match the same turn cohort as Usage; the window is NOT thread creation time.
# EXISTS keeps multiple attempts on one host from duplicating a turn.
_MATCH = """
    ($1::timestamptz IS NULL OR j.created_at >= $1)
    AND ($2::timestamptz IS NULL OR j.created_at < $2)
    AND ($3::text IS NULL OR j.user_id = $3)
    AND ($4::text IS NULL OR t.repo = $4)
    AND ($5::text IS NULL OR j.state = $5)
    AND ($6::text IS NULL OR j.runtime = $6)
    AND ($7::text IS NULL OR j.model = $7)
    AND ($8::text IS NULL OR EXISTS (
        SELECT 1 FROM agent_attempts a WHERE a.job_id = j.id AND a.host_id = $8
    ))
"""

_COLUMNS = f"""
    t.id AS thread_id,
    CASE WHEN t.title_source IN ('generated', 'manual')
         THEN LEFT(t.title, 160) END AS title,
    LEFT(t.user_id, 256) AS user_id, LEFT(t.repo, 256) AS repo,
    t.created_at, t.archived_at, t.deleted_at,
    ({CONVERSATION_STATE_SQL}) AS state,
    (SELECT COUNT(*) FROM agent_jobs x WHERE x.thread_id = t.id) AS turn_count,
    (SELECT MAX(x.updated_at) FROM agent_jobs x WHERE x.thread_id = t.id) AS last_activity_at,
    j.id AS latest_job_id, j.turn_no AS latest_turn_no, j.state AS latest_turn_state,
    LEFT(j.runtime, 128) AS latest_runtime, LEFT(j.model, 256) AS latest_model
"""

_FROM = """
    FROM agent_threads t
    CROSS JOIN LATERAL (
        SELECT x.id, x.thread_id, x.user_id, x.turn_no, x.state, x.runtime, x.model
        FROM agent_jobs x WHERE x.thread_id = t.id
        ORDER BY x.turn_no DESC LIMIT 1
    ) j
"""


class AdminConversationQueries:
    """Bounded admin projections, independent of owner-authenticated readers."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the shared Agent database pool."""
        self._pool = pool

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[asyncpg.Connection]:
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            yield conn

    async def list_conversations(
        self,
        *,
        filters: RunFilters,
        since: datetime,
        until: datetime,
        cursor: ConversationCursor | None,
        limit: int,
    ) -> AdminConversationList:
        """Group before paginating so each conversation appears exactly once."""
        async with self._read() as conn:
            rows = await conn.fetch(
                f"""
                WITH matching AS (
                    SELECT j.thread_id, COUNT(*) AS matching_turn_count,
                           MAX(j.created_at) AS last_matching_turn_at
                    FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id
                    WHERE {_MATCH}
                    GROUP BY j.thread_id
                ), page AS (
                    SELECT thread_id, matching_turn_count, last_matching_turn_at
                    FROM matching
                    WHERE $9::timestamptz IS NULL
                       OR (last_matching_turn_at, thread_id) < ($9, $10::text)
                    ORDER BY last_matching_turn_at DESC, thread_id DESC LIMIT $11
                )
                SELECT {_COLUMNS}, p.matching_turn_count, p.last_matching_turn_at
                {_FROM}
                JOIN page p ON p.thread_id = t.id
                ORDER BY p.last_matching_turn_at DESC, t.id DESC
                """,
                since,
                until,
                filters.user_id,
                filters.repo,
                filters.state,
                filters.runtime,
                filters.model,
                filters.host,
                cursor.created_at if cursor else None,
                cursor.thread_id if cursor else None,
                limit + 1,
            )
        conversations = [AdminConversationMatch.model_validate(dict(row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = conversations[-1]
            next_cursor = encode_cursor(
                ConversationCursor(
                    scope=cursor_scope(["conversations", filters.model_dump()]),
                    since=since,
                    until=until,
                    created_at=last.last_matching_turn_at,
                    thread_id=last.thread_id,
                )
            )
        return AdminConversationList(
            conversations=conversations,
            since=since,
            until=until,
            next_cursor=next_cursor,
        )

    async def detail(
        self,
        thread_id: str,
        *,
        filters: RunFilters,
        since: datetime | None,
        until: datetime | None,
        cursor: ConversationTurnsCursor | None,
        limit: int,
    ) -> AdminConversationDetail | None:
        """Keep all turns in context, marking matches instead of hiding siblings."""
        async with self._read() as conn:
            header = await conn.fetchrow(
                f"SELECT {_COLUMNS} {_FROM} WHERE t.id = $1",
                thread_id,
            )
            if header is None:
                return None
            rows = await conn.fetch(
                f"""
                SELECT {_RUN_COLUMNS}, j.fork_source_job_id, ({_MATCH}) AS matches_filters
                FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id
                WHERE j.thread_id = $9
                  AND ($10::integer IS NULL OR j.turn_no < $10)
                ORDER BY j.turn_no DESC LIMIT $11
                """,
                since,
                until,
                filters.user_id,
                filters.repo,
                filters.state,
                filters.runtime,
                filters.model,
                filters.host,
                thread_id,
                cursor.before_turn_no if cursor else None,
                limit + 1,
            )
        turns = [
            AdminConversationTurn.model_validate(_fold_run_resource_summary(dict(row)))
            for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(
                ConversationTurnsCursor(
                    scope=turns_scope(thread_id, filters, since, until),
                    before_turn_no=turns[-1].turn_no,
                )
            )
        return AdminConversationDetail(
            conversation=AdminConversation.model_validate(dict(header)),
            turns=turns,
            next_cursor=next_cursor,
        )


def turns_scope(
    thread_id: str,
    filters: RunFilters,
    since: datetime | None,
    until: datetime | None,
) -> str:
    """Bind turn pagination to its conversation and optional originating cohort."""
    return cursor_scope(
        [
            "conversation-turns",
            thread_id,
            filters.model_dump(),
            since.isoformat() if since else None,
            until.isoformat() if until else None,
        ]
    )
