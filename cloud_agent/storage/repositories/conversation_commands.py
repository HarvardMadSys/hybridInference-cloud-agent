"""Postgres implementation of the conversation write port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from cloud_agent.domain.execution import QUEUED, SUCCEEDED
from cloud_agent.domain.ids import new_job_id, new_thread_id
from cloud_agent.domain.requests import check_client_request
from cloud_agent.execution_profiles import default_execution_profile
from cloud_agent.storage._sql.jobs import JOB_SELECT, SESSION_DELETE_BLOCK_SQL
from cloud_agent.storage._sql.mcp import write_mcp_bindings
from cloud_agent.storage._sql.rows import job_row_to_dict, load_json
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries
from cloud_agent.task_titles import fallback_title


class PostgresConversationCommands:
    """Submissions, follow-ups, and the thread metadata a person edits."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns.

        A submission that loses the idempotency race reads the winner back,
        and that read is ``PostgresConversationQueries.get_job`` -- the same
        call it made when both lived on one class, over the same pool, and
        outside any transaction this class holds. Held as a peer rather than
        reimplemented, so there is still one statement behind it.
        """
        self._pool = pool
        self._queries = PostgresConversationQueries(pool)

    async def create_job(
        self,
        *,
        user_id: str,
        repo: str,
        task_prompt: str,
        runtime: str,
        model: str,
        execution_profile: str | None = None,
        reasoning_effort: str | None = None,
        base_sha: str | None = None,
        setup_script: str | None = None,
        metadata: dict[str, Any] | None = None,
        mcp_bindings: list[dict[str, Any]] | None = None,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued job and return it.

        Callers that do not name a profile still go through the deployment's
        server-owned default.  This matters for internal producers that call
        the store directly: bypassing the HTTP route must not silently turn a
        secure deployment's jobs into ``trusted-v1`` work.

        ``client_request_id`` is the caller's key for this one submission,
        unique per owner, with ``client_request_fingerprint`` describing what
        was submitted under it. A resend with the same pair returns the job
        the first send created; the same key with a different fingerprint
        returns ``{"error": "client_request_conflict"}`` instead of someone
        else's task. Both or neither.
        """
        check_client_request(client_request_id, client_request_fingerprint)
        if execution_profile is None:
            execution_profile = default_execution_profile().value
        if client_request_id is not None:
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if replayed is not None:
                return replayed
        job_id = new_job_id()
        thread_id = new_thread_id()
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                # What every turn of this conversation shares is written once,
                # here, on the thread; the turns below carry only their own.
                await conn.execute(
                    """
                    INSERT INTO agent_threads
                        (id, user_id, repo, title, title_source,
                         setup_script, execution_profile)
                    VALUES ($1, $2, $3, $4, 'fallback', $5, $6)
                    """,
                    thread_id,
                    user_id,
                    repo,
                    fallback_title(task_prompt),
                    setup_script,
                    execution_profile,
                )
                await write_mcp_bindings(conn, thread_id=thread_id, bindings=mcp_bindings)
                await conn.execute(
                    """
                    INSERT INTO agent_jobs
                        (id, thread_id, turn_no, user_id, base_sha, task_prompt,
                         runtime, model, reasoning_effort, metadata,
                         client_request_id, client_request_fingerprint)
                    VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
                    """,
                    job_id,
                    thread_id,
                    user_id,
                    base_sha,
                    task_prompt,
                    runtime,
                    model,
                    reasoning_effort,
                    json.dumps(metadata) if metadata is not None else None,
                    client_request_id,
                    client_request_fingerprint,
                )
                row = await conn.fetchrow(f"{JOB_SELECT} WHERE j.id = $1", job_id)
                await conn.execute(
                    """
                    INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                    VALUES ($1, $2, 'user', $3)
                    """,
                    thread_id,
                    job_id,
                    task_prompt,
                )
        except asyncpg.UniqueViolationError as exc:
            # Two sends of one submission racing: the loser reads the winner.
            if exc.constraint_name != "uq_agent_jobs_client_request":
                raise
            # The violated constraint is on client_request_id, so the winner has one.
            assert client_request_id is not None
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            assert replayed is not None
            return replayed
        result = job_row_to_dict(row)
        result["title"] = fallback_title(task_prompt)
        result["title_source"] = "fallback"
        return result

    async def create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reset_reasoning_effort: bool = False,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Append a user turn and create the run that will answer it.

        ``reasoning_effort`` overrides the inherited depth; omitting it keeps
        the parent's, as an omitted model does. ``reset_reasoning_effort``
        clears it back to Auto — the one thing a null override cannot express,
        since null is what "inherit" already sends.

        Every follow-up is ``queued`` until all earlier turns have finished.
        The thread row is locked before selecting the latest turn, so
        concurrent submissions form one linear chain instead of siblings that
        could later mutate the same branch at once. A request made from an old
        turn also appends to the current tip, matching chat semantics.

        ``client_request_id`` and ``client_request_fingerprint`` work as for
        :meth:`create_job`: a resend returns the turn the first send made,
        the same key under different content returns
        ``{"error": "client_request_conflict"}``.
        """
        check_client_request(client_request_id, client_request_fingerprint)
        if client_request_id is not None:
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if replayed is not None:
                return replayed
        try:
            return await self._create_follow_up(
                parent_job_id=parent_job_id,
                user_id=user_id,
                prompt=prompt,
                runtime=runtime,
                model=model,
                reasoning_effort=reasoning_effort,
                reset_reasoning_effort=reset_reasoning_effort,
                client_request_id=client_request_id,
                client_request_fingerprint=client_request_fingerprint,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "uq_agent_jobs_client_request":
                raise
            # The violated constraint is on client_request_id, so the winner has one.
            assert client_request_id is not None
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            assert replayed is not None
            return replayed

    async def replayed_submission(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        """The job an owner's key already made, a conflict, or ``None`` for a new key.

        Public because the routes ask it *before* their checks that can change
        between two sends of one submission — model resolution, repo
        entitlement, tool-server connections, whether the session is still
        open. A resend must reach the job the first send made whatever has
        happened since; only a key that has never been used runs those checks.
        :meth:`create_job` and :meth:`create_follow_up` still ask it
        themselves, so a race that slips past the route is still resolved by
        the unique index rather than by a second task.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, client_request_fingerprint FROM agent_jobs "
                "WHERE user_id = $1 AND client_request_id = $2",
                user_id,
                client_request_id,
            )
        if row is None:
            return None
        if row["client_request_fingerprint"] != fingerprint:
            return {"error": "client_request_conflict"}
        return await self._queries.get_job(str(row["id"]))

    async def set_sessions_archived(
        self, *, thread_ids: list[str], user_id: str, archived: bool
    ) -> dict[str, Any]:
        """Set every owned selected thread's archive state, or change none."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id FROM agent_threads "
                "WHERE id = ANY($1::text[]) AND user_id = $2 AND deleted_at IS NULL "
                "ORDER BY id FOR UPDATE",
                thread_ids,
                user_id,
            )
            if len(rows) != len(thread_ids):
                return {"error": "not_found"}
            updated = await conn.fetch(
                """
                UPDATE agent_threads
                SET archived_at = CASE WHEN $3 THEN COALESCE(archived_at, NOW()) ELSE NULL END
                WHERE id = ANY($1::text[]) AND user_id = $2
                RETURNING id AS thread_id, archived_at
                """,
                thread_ids,
                user_id,
                archived,
            )
        return {"sessions": [dict(row) for row in updated]}

    async def delete_sessions(self, *, thread_ids: list[str], user_id: str) -> dict[str, Any]:
        """Irreversibly hide settled archived history while retaining audit rows.

        The thread lock serializes owner deletion against follow-up insertion,
        native-fork preparation and archive changes. Worker lifecycle access
        deliberately keeps the immutable job, residency and sandbox ledgers.
        A fork's shared workspace is never removed by a chat-history action.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id FROM agent_threads "
                "WHERE id = ANY($1::text[]) AND user_id = $2 AND deleted_at IS NULL "
                "ORDER BY id FOR UPDATE",
                thread_ids,
                user_id,
            )
            if len(rows) != len(thread_ids):
                return {"error": "not_found"}
            blocked = await conn.fetchrow(
                f"""
                WITH selected AS (
                    SELECT id AS session_id, archived_at FROM agent_threads
                    WHERE id = ANY($1::text[]) AND user_id = $2
                ), checked AS (
                    SELECT t.session_id, {SESSION_DELETE_BLOCK_SQL} AS reason
                    FROM selected t
                )
                SELECT reason FROM checked WHERE reason IS NOT NULL ORDER BY session_id LIMIT 1
                """,
                thread_ids,
                user_id,
            )
            if blocked is not None:
                return {"error": blocked["reason"]}
            await conn.execute(
                "UPDATE agent_threads SET deleted_at = NOW() "
                "WHERE id = ANY($1::text[]) AND user_id = $2",
                thread_ids,
                user_id,
            )
        return {"thread_ids": thread_ids}

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Archive or restore the owned thread containing ``job_id``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET archived_at = CASE
                    WHEN $3 THEN COALESCE(t.archived_at, NOW())
                    ELSE NULL
                END
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM agent_jobs j
                      WHERE j.id = $1
                        AND j.thread_id = t.id
                        AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.archived_at
                """,
                job_id,
                user_id,
                archived,
            )
        if row is None:
            return None
        return {"thread_id": row["thread_id"], "archived_at": row["archived_at"]}

    async def set_thread_title(
        self, *, job_id: str, user_id: str, title: str
    ) -> dict[str, Any] | None:
        """Rename the owned conversation without changing any turn's prompt."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET title = $3, title_source = 'manual'
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM agent_jobs j
                      WHERE j.id = $1 AND j.thread_id = t.id AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.title
                """,
                job_id,
                user_id,
                title,
            )
        return dict(row) if row is not None else None

    async def set_generated_thread_title(
        self, *, job_id: str, attempt_id: int, lease_generation: int, title: str
    ) -> bool:
        """Save a first-turn title while its running/successful attempt remains current."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Match follow-up/fork lock ordering: thread before job. Locking the
            # source also makes a simultaneous manual rename win in either order.
            thread_id = await conn.fetchval(
                """
                SELECT t.id FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1 AND j.turn_no = 1
                  AND t.title_source = 'fallback' AND t.deleted_at IS NULL
                FOR UPDATE OF t
                """,
                job_id,
            )
            if thread_id is None:
                return False
            live = await conn.fetchval(
                """
                SELECT a.id FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE j.id = $1 AND j.current_attempt_id = a.id
                  AND a.id = $2 AND a.lease_generation = $3 AND a.attempt_no = 1
                  AND (
                      (j.state = 'running' AND a.status = 'running'
                       AND a.lease_expires_at > clock_timestamp())
                      OR (j.state = 'succeeded' AND a.status = 'finished')
                  )
                FOR SHARE OF a, j
                """,
                job_id,
                attempt_id,
                lease_generation,
            )
            if live is None:
                return False
            updated = await conn.fetchval(
                """
                UPDATE agent_threads SET title = $2, title_source = 'generated'
                WHERE id = $1 AND title_source = 'fallback' AND deleted_at IS NULL
                RETURNING id
                """,
                thread_id,
                title,
            )
            return updated is not None

    async def set_thread_pinned(
        self, *, job_id: str, user_id: str, pinned: bool
    ) -> dict[str, Any] | None:
        """Pin or unpin the owned thread containing ``job_id``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET pinned_at = CASE
                    WHEN $3 THEN COALESCE(t.pinned_at, NOW())
                    ELSE NULL
                END
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM agent_jobs j
                      WHERE j.id = $1
                        AND j.thread_id = t.id
                        AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.pinned_at
                """,
                job_id,
                user_id,
                pinned,
            )
        if row is None:
            return None
        return {"thread_id": row["thread_id"], "pinned_at": row["pinned_at"]}

    async def _create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None,
        model: str | None,
        reasoning_effort: str | None,
        reset_reasoning_effort: bool,
        client_request_id: str | None,
        client_request_fingerprint: str | None,
    ) -> dict[str, Any] | None:
        """The one-transaction body of :meth:`create_follow_up`."""
        job_id = new_job_id()
        async with self._pool.acquire() as conn, conn.transaction():
            requested = await conn.fetchrow(
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
                parent_job_id,
                user_id,
            )
            if requested is None:
                return None
            thread = await conn.fetchrow(
                "SELECT id, title, title_source, pinned_at, archived_at FROM agent_threads "
                "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
                requested["thread_id"],
                user_id,
            )
            if thread is None:
                return None
            if thread["archived_at"] is not None:
                return {"error": "session_archived"}
            thread_id = thread["id"]
            parent = await conn.fetchrow(
                f"{JOB_SELECT} WHERE j.thread_id = $1 "
                "ORDER BY j.turn_no DESC LIMIT 1 FOR UPDATE OF j",
                thread_id,
            )
            if parent is None:  # Defensive: every thread is created with turn one.
                return None
            # The owner-facing route checks this before doing entitlement and
            # MCP work, but that check is necessarily advisory: an activation
            # can become indeterminate while those awaits run. Locking the
            # durable sandbox here closes that TOCTOU. If this transaction
            # wins the row first, the concurrent LOST writer subsequently
            # fails the inserted turn in its own transaction; if LOST wins,
            # no turn is inserted at all.
            # Every lifecycle writer takes membership before sandbox. Locking
            # the two joined rows in planner order would make that order
            # implicit and could invert against host reconciliation.
            await conn.fetchval(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1
                FOR UPDATE
                """,
                thread_id,
            )
            sandbox = await conn.fetchrow(
                """
                SELECT g.state, m.context_status, TRUE AS shared_live
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                FOR UPDATE OF g
                """,
                thread_id,
            )
            if sandbox is not None and (
                sandbox["context_status"] == "lost" or sandbox["state"] in {"stopped_dirty", "lost"}
            ):
                return {"error": "sandbox_context_lost"}
            if sandbox is None and parent["state"] == SUCCEEDED:
                # A successful native turn must have settled inside its group.
                # Rows that predate that invariant are audit history, not a
                # source from which the new runtime can fabricate continuity.
                return {"error": "sandbox_context_lost"}
            turn_no = parent["turn_no"] + 1
            # Repo, setup script, execution profile and tool servers are the
            # thread's, not copied per turn: the next turn of a conversation
            # runs with the surface the thread has been running with, and a
            # follow-up cannot quietly gain a server the owner never chose.
            await conn.execute(
                """
                INSERT INTO agent_jobs
                    (id, thread_id, parent_job_id, turn_no, user_id, base_sha,
                     task_prompt, runtime, model, reasoning_effort, state, metadata,
                     client_request_id, client_request_fingerprint)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14)
                """,
                job_id,
                thread_id,
                parent["id"],
                turn_no,
                user_id,
                parent["published_commit_sha"] or parent["base_sha"],
                prompt,
                runtime or parent["runtime"],
                model or parent["model"],
                None
                if reset_reasoning_effort
                else (reasoning_effort or parent["reasoning_effort"]),
                QUEUED,
                json.dumps(load_json(parent["metadata"]))
                if parent["metadata"] is not None
                else None,
                client_request_id,
                client_request_fingerprint,
            )
            row = await conn.fetchrow(f"{JOB_SELECT} WHERE j.id = $1", job_id)
            await conn.execute(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                VALUES ($1, $2, 'user', $3)
                """,
                thread_id,
                job_id,
                prompt,
            )
            await conn.execute(
                "UPDATE agent_threads SET updated_at = NOW() WHERE id = $1", thread_id
            )
        result = job_row_to_dict(row)
        result["title"] = thread["title"]
        result["title_source"] = thread["title_source"]
        result["pinned_at"] = thread["pinned_at"]
        return result
