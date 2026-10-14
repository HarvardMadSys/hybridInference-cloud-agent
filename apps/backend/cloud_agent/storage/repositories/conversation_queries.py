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

from cloud_agent.domain.execution import JobStreamState
from cloud_agent.storage._sql.jobs import (
    CONVERSATION_STATE_SQL,
    JOB_SELECT,
    JOB_SUMMARY_SELECT,
    QUALIFIED_JOB_SUMMARY_COLUMNS,
    SESSION_DELETE_BLOCK_SQL,
)
from cloud_agent.storage._sql.pending_messages import list_pending_messages
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
        """Return the conversation containing an owned job.

        With its turns and transcript, and with the messages still waiting to
        become turns (``pending_messages``, in send order) and why the queue is
        not moving by itself (``queue_paused_reason``), read together.
        """
        async with self._pool.acquire() as conn:
            thread = await conn.fetchrow(
                """
                SELECT t.id, t.repo, t.title, t.created_at, t.updated_at,
                       t.pinned_at, t.archived_at, t.queue_paused_reason
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
                SELECT m.id, m.role, m.content, m.job_id, m.created_at, m.mid_turn
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1
                -- A turn opens with its prompt; a message the owner sent into
                -- the running turn reads where it was sent, among the replies.
                ORDER BY j.turn_no,
                         CASE WHEN m.role = 'user' AND NOT m.mid_turn THEN 0 ELSE 1 END,
                         m.id
                """,
                thread["id"],
            )
            jobs = await conn.fetch(
                f"{JOB_SUMMARY_SELECT} WHERE j.thread_id = $1 ORDER BY j.turn_no",
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
            # Read with the turns, on the same connection: the page renders the
            # queue beside the transcript, and a message that became a turn has
            # to disappear from one in the same read that shows it in the other.
            pending = await list_pending_messages(conn, thread["id"])
        return {
            "id": thread["id"],
            "repo": thread["repo"],
            "title": thread["title"],
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
            "archived_at": thread["archived_at"],
            "messages": [dict(message) for message in messages],
            "jobs": job_rows,
            "pending_messages": pending,
            "queue_paused_reason": thread["queue_paused_reason"],
        }

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Fetch one job by id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {QUALIFIED_JOB_SUMMARY_COLUMNS}, t.title, t.title_source, t.pinned_at, t.archived_at, t.deleted_at,
                       to_jsonb(s) AS thread_sandbox
                FROM agent_jobs j
                LEFT JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                WHERE j.id = $1
                """,
                job_id,
            )
        return job_row_to_dict(row) if row else None

    async def get_job_stream_state(self, *, job_id: str, user_id: str) -> JobStreamState | None:
        """Read only an owned job's state and publication URL for its event stream.

        Stream admission checks the full job's ownership and visibility first.
        Subsequent reads stay scoped to that owner without loading the prompt,
        conversation configuration, or shared sandbox on each event wake-up.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, published_pr_url FROM agent_jobs WHERE id = $1 AND user_id = $2",
                job_id,
                user_id,
            )
        return JobStreamState(row["state"], row["published_pr_url"]) if row else None

    async def owner_changes(
        self, *, user_id: str, job_ids: list[str], thread_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """The current values an owner's tabs show for these jobs and threads.

        ``jobs`` and ``threads``, each row with its ``revision``. Only rows the
        owner holds are returned; an id that is missing was removed or is not
        theirs, and the caller reports it as gone.
        """
        jobs: list[asyncpg.Record] = []
        threads: list[asyncpg.Record] = []
        async with self._pool.acquire() as conn:
            if job_ids:
                jobs = await conn.fetch(
                    f"""
                    SELECT j.id, j.thread_id, j.revision, j.state, j.published_pr_url,
                           {CONVERSATION_STATE_SQL} AS conversation_state
                    FROM agent_jobs j
                    WHERE j.user_id = $1 AND j.id = ANY($2::text[])
                    """,
                    user_id,
                    job_ids,
                )
            if thread_ids:
                threads = await conn.fetch(
                    """
                    SELECT id, revision, title, title_source, pinned_at, archived_at,
                           deleted_at, updated_at
                    FROM agent_threads
                    WHERE user_id = $1 AND id = ANY($2::text[])
                    """,
                    user_id,
                    thread_ids,
                )
        return {"jobs": [dict(row) for row in jobs], "threads": [dict(row) for row in threads]}

    async def latest_turn(self, *, thread_id: str) -> dict[str, Any] | None:
        """The newest turn of this conversation, whatever state it is in.

        Deliberately not only finished turns: the decision has to be able to see
        a turn that is still running, because "a person's turn is in flight" is
        the reason to wait rather than the reason to schedule.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {QUALIFIED_JOB_SUMMARY_COLUMNS}, t.title, t.title_source,
                       t.pinned_at, t.archived_at, t.deleted_at,
                       to_jsonb(s) AS thread_sandbox
                FROM agent_jobs j
                LEFT JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                WHERE j.thread_id = $1
                ORDER BY j.turn_no DESC LIMIT 1
                """,
                thread_id,
            )
        return None if row is None else job_row_to_dict(row)

    async def get_job_for_update(self, *, conn: Any, job_id: str) -> dict[str, Any] | None:
        """Read one turn inside the caller's transaction, taking its row lock.

        The decision re-reads the turn it is about to judge under the same
        transaction that writes the outcome. Between the read that built the
        context and this one the turn can have been reaped, retried or
        superseded, and a decision made about a turn that has since changed is a
        decision about nothing.
        """
        row = await conn.fetchrow(
            f"{JOB_SELECT} WHERE j.id = $1 FOR UPDATE OF j",
            job_id,
        )
        return None if row is None else job_row_to_dict(row)

    async def latest_assistant_message_state(self, *, job_id: str) -> dict[str, Any] | None:
        """The last assistant message, and whether the runtime clipped it.

        ``truncated`` is the difference between "the model said nothing more"
        and "the transport stopped carrying what it said". A report reader has
        to tell those apart: the report block is at the *end* of a message, so a
        clipped one is a message whose answer may have been removed, and reading
        it as silence is exactly the failure the completeness rule exists to
        prevent.

        Ordered by insert, not by time: every assistant message mirrors exactly
        one ``message`` event, and the insert order *is* the order the runtime
        emitted them. A turn that produced no assistant text reports ``None``
        rather than the previous turn's words, which is what makes "no report"
        distinguishable from "reported".
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content, truncated FROM agent_thread_messages
                WHERE job_id = $1 AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                job_id,
            )
        if row is None or row["content"] is None:
            return None
        return {"content": str(row["content"]), "truncated": bool(row["truncated"])}

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
        temporary: bool = False,
    ) -> list[dict[str, Any]]:
        """List a user's jobs with pinned threads first, then newest first.

        ``repo`` narrows the page to one project, which is how the sidebar
        pages a single project past the global newest-first window.
        ``temporary`` narrows it to the conversations without a repository.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {QUALIFIED_JOB_SUMMARY_COLUMNS}, t.title, t.title_source, t.pinned_at, t.archived_at
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
                WHERE j.user_id = $1
                  AND t.deleted_at IS NULL
                  AND ($4::text IS NULL OR t.repo = $4)
                  AND (NOT $5::boolean OR t.repo IS NULL)
                  AND (($3 AND t.archived_at IS NOT NULL)
                       OR (NOT $3 AND t.archived_at IS NULL))
                ORDER BY t.pinned_at DESC NULLS LAST, j.created_at DESC
                LIMIT $2
                """,
                user_id,
                limit,
                archived,
                repo,
                temporary,
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
        temporary: bool = False,
    ) -> dict[str, Any]:
        """Page complete owned conversations, searching their canonical title.

        ``temporary`` keeps only the conversations without a repository.

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
        scope_filters: list[Any] = [user_id, q, repo, include_archived]
        # Preserve issued cursors for searches that do not filter pin state.
        if pinned is not None:
            scope_filters.append(pinned)
        # Likewise for the temporary-workspace filter, which older cursors lack.
        if temporary:
            scope_filters.append("temporary")
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
                      AND (NOT $11::boolean OR t.repo IS NULL)
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
                SELECT {QUALIFIED_JOB_SUMMARY_COLUMNS}, page.title, page.pinned_at,
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
                temporary,
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
        temporary: bool = False,
    ) -> dict[str, Any]:
        """Page complete conversations independently of their number of turns.

        ``temporary`` selects the conversations without a repository, the way
        ``repo`` selects one project; the facets always count every project.

        Project facets apply search/state/archive filters before the selected
        repo. The page and its counts share a database snapshot; the opaque
        keyset remains valid when earlier rows are unarchived or deleted.
        Row-only requests skip the full-history aggregate and return null counts.
        """
        scope = session_cursor_scope(user_id, archived, search, repo, state, temporary=temporary)
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
                temporary,
            )
        page = rows[:limit]

        def selected(project: str | None) -> bool:
            if temporary:
                return project is None
            return repo is None or project == repo

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
                sum(int(item["count"]) for item in projects if selected(item["repo"]))
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
                "SELECT id, user_id, title FROM agent_threads WHERE id = $1 AND deleted_at IS NULL",
                thread_id,
            )
        return dict(row) if row is not None else None
