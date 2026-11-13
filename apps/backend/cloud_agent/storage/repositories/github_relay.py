"""Postgres implementation of the GitHub relay port."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.fences import RESIDENCY_CALLER_FROM, live_residency_caller_sql

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresGitHubRelayRepository:
    """The GitHub relay's caller fence and pull request record."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def resolve_github_caller(
        self, *, thread_id: str, residency_generation: int
    ) -> dict[str, Any] | None:
        """Resolve a residency to its running turn, fenced as the MCP relay fences.

        One statement holds both halves the MCP relay checks in two
        (``resolve_residency_attempt``, then ``resolve_model_credential``): the
        residency is hot and serving this attempt on its own host, and the
        attempt's lease, its job's state and the host's enrolment are live. A
        GitHub request is a single forwarded call, so there is no later moment
        that would need the fence read again. Both relays build their fence
        from the same fragments (:mod:`cloud_agent.storage._sql.fences`).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT j.id AS job_id, a.id AS attempt_id, j.thread_id, j.user_id, t.repo
                FROM {RESIDENCY_CALLER_FROM}
                WHERE {live_residency_caller_sql("$1", "$2")}
                """,
                thread_id,
                residency_generation,
            )
        return dict(row) if row is not None else None

    async def record_pull_request(
        self,
        *,
        thread_id: str,
        job_id: str,
        user_id: str,
        repo: str,
        number: int,
        url: str,
        head_ref: str,
        base_ref: str,
        draft: bool,
    ) -> None:
        """Insert the pull request once, and name it on the turn that opened it.

        A repeat report keeps the first row. The turn's ``published_pr_url``
        keeps the first pull request it opened; writing it bumps the job's
        revision, which is what tells the owner's open tabs to read it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_thread_pull_requests
                    (thread_id, repo, number, url, job_id, user_id, head_ref, base_ref, draft)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (thread_id, repo, number) DO NOTHING
                """,
                thread_id,
                repo,
                number,
                url,
                job_id,
                user_id,
                head_ref,
                base_ref,
                draft,
            )
            await conn.execute(
                """
                UPDATE agent_jobs SET published_pr_url = $2, updated_at = NOW()
                WHERE id = $1 AND thread_id = $3 AND published_pr_url IS NULL
                """,
                job_id,
                url,
                thread_id,
            )

    async def list_pull_requests(self, *, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Newest first, bounded."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT thread_id, repo, number, url, job_id, user_id, head_ref, base_ref,
                       draft, created_at
                FROM agent_thread_pull_requests
                WHERE thread_id = $1
                ORDER BY created_at DESC, number DESC
                LIMIT $2
                """,
                thread_id,
                limit,
            )
        return [dict(row) for row in rows]
