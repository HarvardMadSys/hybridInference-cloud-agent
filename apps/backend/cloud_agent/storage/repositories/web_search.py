"""Postgres admission leases for the platform-owned Web Search tool."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from cloud_agent.ports.web_search import WebSearchAdmission, WebSearchAdmissionReason

if TYPE_CHECKING:
    import asyncpg


# These deployment-wide limits are intentionally fixed. They protect the
# built-in Web Search surface across configured providers and control-plane
# replicas; they are not user-configurable spend budgets.
WEB_SEARCH_CONCURRENCY = 4
WEB_SEARCH_GLOBAL_REQUESTS = 60
WEB_SEARCH_USER_REQUESTS = 10
WEB_SEARCH_WINDOW_SECONDS = 60
WEB_SEARCH_LEASE_SECONDS = 30

_ADMISSION_LOCK = "agent-web-search-admission"


class PostgresWebSearchRepository:
    """Coordinate provider capacity and rate receipts in Postgres."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def acquire_web_search(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> WebSearchAdmission:
        """Admit one search under a fresh attempt fence and shared limits.

        One transaction-scoped advisory lock makes pruning, counting and
        inserting a single decision across all replicas. The live attempt and
        its job are then locked in the repository's attempt-before-job order,
        so neither can cross its terminal boundary between validation and the
        admission receipt.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                _ADMISSION_LOCK,
            )

            # A receipt is needed for the complete rate window even after a
            # provider call returns. Older rows carry no admission state and
            # are deleted before each serialized decision.
            await conn.execute(
                """
                DELETE FROM agent_web_search_admissions
                WHERE admitted_at <= clock_timestamp()
                                      - make_interval(secs => $1)
                """,
                WEB_SEARCH_WINDOW_SECONDS,
            )

            attempt = await conn.fetchrow(
                """
                SELECT a.job_id
                FROM agent_attempts a
                WHERE a.id = $1
                  AND a.job_id = $2
                  AND a.lease_generation = $3
                  AND a.status = 'running'
                  AND a.lease_expires_at > clock_timestamp()
                FOR UPDATE OF a
                """,
                attempt_id,
                job_id,
                lease_generation,
            )
            if attempt is None:
                return WebSearchAdmission(call_id=None, reason="stale_fence")

            job = await conn.fetchrow(
                """
                SELECT j.user_id
                FROM agent_jobs j
                WHERE j.id = $1
                  AND j.current_attempt_id = $2
                  AND j.state = 'running'
                FOR UPDATE OF j
                """,
                job_id,
                attempt_id,
            )
            if job is None:
                return WebSearchAdmission(call_id=None, reason="stale_fence")

            counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE active_until > clock_timestamp()
                    ) AS active,
                    COUNT(*) FILTER (
                        WHERE admitted_at > clock_timestamp()
                                            - make_interval(secs => $2)
                    ) AS global_recent,
                    COUNT(*) FILTER (
                        WHERE user_id = $1
                          AND admitted_at > clock_timestamp()
                                                - make_interval(secs => $2)
                    ) AS user_recent
                FROM agent_web_search_admissions
                """,
                job["user_id"],
                WEB_SEARCH_WINDOW_SECONDS,
            )
            decision = _limit_reason(counts)
            if decision is not None:
                return WebSearchAdmission(call_id=None, reason=decision)

            call_id = uuid.uuid4().hex
            await conn.execute(
                """
                INSERT INTO agent_web_search_admissions (
                    call_id, user_id, job_id, attempt_id, lease_generation,
                    admitted_at, active_until
                )
                SELECT $1, $2, $3, $4, $5, admitted.now,
                       admitted.now + make_interval(secs => $6)
                FROM (
                    SELECT clock_timestamp() AS now
                ) admitted
                """,
                call_id,
                job["user_id"],
                job_id,
                attempt_id,
                lease_generation,
                WEB_SEARCH_LEASE_SECONDS,
            )
            return WebSearchAdmission(call_id=call_id, reason="admitted")

    async def release_web_search(self, call_id: str) -> bool:
        """Shorten one live concurrency lease without erasing its rate receipt."""
        async with self._pool.acquire() as conn:
            released = await conn.fetchval(
                """
                UPDATE agent_web_search_admissions
                SET active_until = LEAST(active_until, clock_timestamp())
                WHERE call_id = $1
                  AND active_until > clock_timestamp()
                RETURNING call_id
                """,
                call_id,
            )
        return released is not None


def _limit_reason(counts: Any) -> WebSearchAdmissionReason | None:
    """Return the first fixed limit reached by the serialized snapshot."""
    if int(counts["active"]) >= WEB_SEARCH_CONCURRENCY:
        return "concurrency_limited"
    if int(counts["global_recent"]) >= WEB_SEARCH_GLOBAL_REQUESTS:
        return "global_rate_limited"
    if int(counts["user_recent"]) >= WEB_SEARCH_USER_REQUESTS:
        return "user_rate_limited"
    return None
