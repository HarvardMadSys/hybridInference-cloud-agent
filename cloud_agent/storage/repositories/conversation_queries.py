"""Postgres implementation of the conversation-reads port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING

from cloud_agent.storage._sql.jobs import (
    JOB_SELECT,
    QUALIFIED_JOB_COLUMNS,
    SESSION_DELETE_BLOCK_SQL,
)
from cloud_agent.storage._sql.rows import job_row_to_dict, sandbox_row_to_dict
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE
from cloud_agent.storage._sql.sessions import (
    decode_conversation_cursor,
    decode_session_cursor,
    encode_session_cursor,
    session_cursor_scope,
    session_query,
)

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresConversationQueries:
    """Reading conversations, their jobs, and the project list."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def get_thread_for_job(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return the conversation containing an owned job."""
        async with self._pool.acquire() as conn:
            thread = await conn.fetchrow(
                """
                SELECT t.id, t.repo, t.title, t.created_at, t.updated_at,
                       t.pinned_at, t.archived_at
                FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
                """,
                job_id,
                user_id,
            )
            if thread is None:
                return None
            messages = await conn.fetch(
                """
                SELECT m.id, m.role, m.content, m.job_id, m.created_at
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1
                ORDER BY j.turn_no, CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                """,
                thread["id"],
            )
            jobs = await conn.fetch(
                f"{JOB_SELECT} WHERE j.thread_id = $1 ORDER BY j.turn_no",
                thread["id"],
            )
            sandbox = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread["id"],
            )
            job_rows = [job_row_to_dict(job) for job in jobs]
            for job in job_rows:
                job["title"] = thread["title"]
                job["pinned_at"] = thread["pinned_at"]
                job["archived_at"] = thread["archived_at"]
                if sandbox is not None:
                    job["thread_sandbox"] = sandbox_row_to_dict(sandbox)
        return {
            "id": thread["id"],
            "repo": thread["repo"],
            "title": thread["title"],
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
            "archived_at": thread["archived_at"],
            "messages": [dict(message) for message in messages],
            "jobs": job_rows,
        }

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Fetch one job by id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {QUALIFIED_JOB_COLUMNS}, t.title, t.title_source, t.pinned_at, t.archived_at, t.deleted_at,
                       to_jsonb(s) AS thread_sandbox
                FROM agent_jobs j
                LEFT JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                WHERE j.id = $1
                """,
                job_id,
            )
        return job_row_to_dict(row) if row else None

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """List a user's jobs with pinned threads first, then newest first.

        ``repo`` narrows the page to one project, which is how the sidebar
        pages a single project past the global newest-first window.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {QUALIFIED_JOB_COLUMNS}, t.title, t.title_source, t.pinned_at, t.archived_at
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
                WHERE j.user_id = $1
                  AND t.deleted_at IS NULL
                  AND ($4::text IS NULL OR t.repo = $4)
                  AND (($3 AND t.archived_at IS NOT NULL)
                       OR (NOT $3 AND t.archived_at IS NULL))
                ORDER BY t.pinned_at DESC NULLS LAST, j.created_at DESC
                LIMIT $2
                """,
                user_id,
                limit,
                archived,
                repo,
            )
        return [job_row_to_dict(row) for row in rows]

    async def list_conversations(
        self,
        *,
        user_id: str,
        q: str = "",
        repo: str | None = None,
        include_archived: bool = False,
        pinned: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Page complete owned conversations, searching their canonical title.

        Activity is the thread's own update or the most recently updated of
        its turns, whichever is later. Any turn counts, not only the newest:
        an earlier turn completes or is published after a later one was
        cancelled, and that completion is the activity. It is one probe of the
        ``(thread_id, updated_at)`` index per owned thread, never an aggregate
        over the whole history, and the newest turn is fetched in full only
        for the bounded result page. Messages and job transitions move a
        thread; lease heartbeats do not. The unique thread id breaks ties.
        A cursor is scoped to its owner and filters, and is a pagination hint,
        never authority to read a thread. Updates may move a thread ahead of a
        previously issued cursor; refreshing starts again from the latest page.
        """
        q = q.strip()
        if not 1 <= limit <= 200 or len(q) > 200 or "\x00" in q:
            raise ValueError("Invalid conversation search parameters")
        scope_filters = [user_id, q, repo, include_archived]
        # Preserve issued cursors for searches that do not filter pin state.
        if pinned is not None:
            scope_filters.append(pinned)
        scope = hashlib.sha256(json.dumps(scope_filters).encode()).hexdigest()
        boundary = (
            decode_conversation_cursor(cursor, scope)
            if cursor is not None
            else (None, None, None, None)
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                WITH owned AS (
                    SELECT t.id AS thread_id, t.title, t.pinned_at,
                           t.archived_at IS NOT NULL AS archived,
                           GREATEST(t.updated_at, freshest.updated_at) AS last_activity_at
                    FROM agent_threads t
                    CROSS JOIN LATERAL (
                        SELECT j.updated_at
                        FROM agent_jobs j
                        WHERE j.thread_id = t.id AND j.user_id = $1
                        ORDER BY j.updated_at DESC
                        LIMIT 1
                    ) freshest
                    WHERE t.user_id = $1 AND t.deleted_at IS NULL
                      AND ($2::text IS NULL OR t.repo = $2)
                      AND ($3::text = '' OR POSITION(LOWER($3) IN LOWER(t.title)) > 0
                           OR POSITION(LOWER($3) IN LOWER(t.repo)) > 0)
                      AND ($4::boolean OR t.archived_at IS NULL)
                      AND ($10::boolean IS NULL OR (t.pinned_at IS NOT NULL) = $10)
                ), page AS (
                    SELECT * FROM owned
                    WHERE ($6::boolean IS NULL OR (
                          pinned_at IS NOT NULL,
                          COALESCE(pinned_at, '-infinity'::timestamptz),
                          last_activity_at, thread_id
                      ) < (
                          $6, COALESCE($7::timestamptz, '-infinity'::timestamptz),
                          $8::timestamptz, $9::text
                      ))
                    ORDER BY pinned_at DESC NULLS LAST, last_activity_at DESC, thread_id DESC
                    LIMIT $5
                )
                SELECT {QUALIFIED_JOB_COLUMNS}, page.title, page.pinned_at,
                       page.archived, page.last_activity_at
                FROM page
                JOIN agent_threads t ON t.id = page.thread_id
                JOIN LATERAL (
                    SELECT * FROM agent_jobs latest
                    WHERE latest.thread_id = page.thread_id AND latest.user_id = $1
                    ORDER BY latest.turn_no DESC
                    LIMIT 1
                ) j ON TRUE
                ORDER BY page.pinned_at DESC NULLS LAST,
                         page.last_activity_at DESC, page.thread_id DESC
                """,
                user_id,
                repo,
                q,
                include_archived,
                limit + 1,
                *boundary,
                pinned,
            )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": 1,
                        "scope": scope,
                        "pin": last["pinned_at"].isoformat() if last["pinned_at"] else None,
                        "activity": last["last_activity_at"].isoformat(),
                        "thread": last["thread_id"],
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode()
        return {
            "conversations": [
                {
                    "job": job_row_to_dict(row),
                    "title": row["title"],
                    "archived": row["archived"],
                    "last_activity_at": row["last_activity_at"],
                }
                for row in rows[:limit]
            ],
            "next_cursor": next_cursor,
        }

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        """Summarize a user's repos, most recently active first.

        The sidebar groups tasks by project, so it needs every repo the user
        has ever run in — not just those represented in the newest-first job
        page, which would silently drop dormant projects from the tree.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.repo,
                       COUNT(DISTINCT j.thread_id) AS task_count,
                       COUNT(*) FILTER (
                           WHERE j.state IN ('queued', 'running')
                       ) AS active_count,
                       MAX(GREATEST(t.updated_at, j.updated_at)) AS last_activity_at,
                       COUNT(DISTINCT j.thread_id) FILTER (
                           WHERE t.pinned_at IS NOT NULL
                       ) AS pinned_count,
                       MAX(t.pinned_at) AS pinned_at
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
                WHERE j.user_id = $1
                  AND t.deleted_at IS NULL
                  AND (($2 AND t.archived_at IS NOT NULL)
                       OR (NOT $2 AND t.archived_at IS NULL))
                GROUP BY t.repo
                ORDER BY pinned_at DESC NULLS LAST, last_activity_at DESC
                """,
                user_id,
                archived,
            )
        return [
            {
                "repo": row["repo"],
                "task_count": int(row["task_count"]),
                "active_count": int(row["active_count"]),
                "last_activity_at": row["last_activity_at"],
                "pinned_count": int(row["pinned_count"]),
                "pinned_at": row["pinned_at"],
            }
            for row in rows
        ]

    async def list_sessions(
        self,
        *,
        user_id: str,
        archived: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        search: str | None = None,
        repo: str | None = None,
        state: str | None = None,
        include_summary: bool = True,
    ) -> dict[str, Any]:
        """Page complete conversations independently of their number of turns.

        Project facets apply search/state/archive filters before the selected
        repo. The page and its counts share a database snapshot; the opaque
        keyset remains valid when earlier rows are unarchived or deleted.
        Row-only requests skip the full-history aggregate and return null counts.
        """
        scope = session_cursor_scope(user_id, archived, search, repo, state)
        after_at, after_id = decode_session_cursor(cursor, scope)
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            projects = (
                await conn.fetch(
                    session_query()
                    + "SELECT repo, COUNT(*) AS count FROM matched GROUP BY repo ORDER BY repo",
                    user_id,
                    archived,
                    search,
                    state,
                )
                if include_summary
                else None
            )
            rows = await conn.fetch(
                session_query(filter_repo=True)
                + f"""
                SELECT t.*, {SESSION_DELETE_BLOCK_SQL} AS delete_block_reason
                FROM matched t
                WHERE ($6::timestamptz IS NULL OR (t.sort_at, t.session_id) < ($6, $7::text))
                ORDER BY t.sort_at DESC, t.session_id DESC
                LIMIT $8
                """,
                user_id,
                archived,
                search,
                state,
                repo,
                after_at,
                after_id,
                limit + 1,
            )
        page = rows[:limit]
        return {
            "sessions": [
                {
                    "thread_id": row["session_id"],
                    "title": row["session_title"],
                    "archived_at": row["archived_at"],
                    "job": job_row_to_dict(row),
                    "can_delete": row["delete_block_reason"] is None,
                    "delete_block_reason": row["delete_block_reason"],
                }
                for row in page
            ],
            "total": (
                sum(int(item["count"]) for item in projects if repo is None or item["repo"] == repo)
                if projects is not None
                else None
            ),
            "projects": (
                [{"repo": item["repo"], "count": int(item["count"])} for item in projects]
                if projects is not None
                else None
            ),
            "next_cursor": (
                encode_session_cursor(page[-1]["sort_at"], page[-1]["session_id"], scope)
                if len(rows) > limit
                else None
            ),
        }

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Read a visible thread for the owner's MCP selection surface."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id FROM agent_threads WHERE id = $1 AND deleted_at IS NULL",
                thread_id,
            )
        return dict(row) if row is not None else None
