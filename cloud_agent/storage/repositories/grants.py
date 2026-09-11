"""Postgres implementation of the Attempt grants port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresGrantRepository:
    """Attempt grants: what was issued, what may be renewed, what to revoke."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def record_attempt_grant(
        self, *, attempt_id: int, lease_generation: int, grant_id: str
    ) -> bool:
        """Attach a minted grant to the attempt it was minted for.

        Fenced like every other worker write: an attempt that was superseded
        between the claim and this call does not get the grant recorded against
        it, so the renewal pass never picks it up and the capability dies at its
        own TTL.

        **The same three predicates as ``list_renewable_grants``, deliberately.**
        That query is the fence, and this write is what puts a row inside it —
        so a row this accepts and that rejects is a grant nothing will ever
        renew, bound to an attempt whose lease another runner may already have
        taken. The expiry check was missing here while being present there,
        which is exactly the pair that produces two live sandboxes for one job:
        a lease can lapse between the claim and this call without the
        generation changing, because nothing bumps a generation on expiry
        alone.

        Args:
            attempt_id: The attempt the grant belongs to.
            lease_generation: The fence the claim returned.
            grant_id: The gateway's identifier for the grant.

        Returns:
            Whether the row was still current enough to write.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agent_attempts SET grant_id = $3
                WHERE id = $1
                  AND lease_generation = $2
                  AND status = 'running'
                  AND lease_expires_at > NOW()
                """,
                attempt_id,
                lease_generation,
                grant_id,
            )
        return result.endswith(" 1")

    async def list_renewable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """List the grants belonging to attempts that are still current.

        **This is the fence.** A renewal loop that skipped this query and
        renewed whatever it minted would keep a superseded attempt's sandbox
        alive and spending, and the gateway could not catch it — it has no
        attempt state to check. Every predicate here is load-bearing:
        ``status = 'running'`` drops superseded and finished attempts, and
        ``lease_expires_at > NOW()`` drops the ones whose lease died before the
        reaper noticed.

        ``lease_generation`` rides along because the renewal pass may have to
        *end* one of these attempts — a grant the gateway keeps refusing — and
        every write that ends an attempt is fenced. Looking the generation up
        again at that point would read a row that may have moved on in between.

        Args:
            limit: Most rows to return in one pass.

        Returns:
            ``[{"attempt_id", "job_id", "grant_id", "lease_generation"}]`` for
            live attempts only.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id AS attempt_id, job_id, grant_id, lease_generation
                FROM agent_attempts
                WHERE grant_id IS NOT NULL
                  AND status = 'running'
                  AND lease_expires_at > NOW()
                -- **Most urgent first, not lowest id.** `ORDER BY id` returns
                -- the same first `limit` rows on every pass, so in a pool with
                -- more live attempts than that, everything above the cut is
                -- never renewed at all — its grants expire and its jobs die at
                -- a model call, while the loop reports itself healthy.
                --
                -- Ordering by the lease also rotates: a heartbeat pushes an
                -- attempt to the back, so the window moves. It is an
                -- approximation — the grant's own expiry lives at the gateway
                -- and this side cannot sort on it — which is why the caller
                -- logs a truncated pass rather than trusting the ordering to
                -- be sufficient on its own.
                ORDER BY lease_expires_at ASC, id ASC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "attempt_id": row["attempt_id"],
                "job_id": row["job_id"],
                "grant_id": row["grant_id"],
                "lease_generation": row["lease_generation"],
            }
            for row in rows
        ]

    async def take_revocable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Claim the grants of attempts that stopped being current, and clear them.

        The complement of :meth:`list_renewable_grants`: a superseded attempt,
        a finished one, or one whose lease lapsed. Returning them and clearing
        the column in one statement is what stops two control-plane replicas —
        or two passes of the same loop — from both trying to revoke, and what
        stops a grant the gateway refuses to revoke from being retried forever.

        The clear is a data-modifying CTE rather than
        ``UPDATE ... RETURNING grant_id``, because ``RETURNING`` reports the
        **new** value — which is the ``NULL`` just written. That version passed
        every in-memory test and handed ``None`` to ``revoke`` against a real
        database; ``tests/integration/storage/test_agent_grant_store.py`` is
        what caught it.

        Clearing before the gateway has confirmed anything is deliberate.
        Revocation is an *acceleration* of the TTL, not the mechanism: the
        capability expires on its own, so a failed revoke costs lateness, and a
        row that kept being retried would cost a call every pass for as long as
        the deployment ran.

        Args:
            limit: Most rows to take in one pass.

        Returns:
            ``[{"attempt_id", "job_id", "grant_id"}]``, now detached from their
            attempts.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH dead AS (
                    SELECT id, job_id, grant_id
                    FROM agent_attempts
                    WHERE grant_id IS NOT NULL
                      AND (status <> 'running' OR lease_expires_at <= NOW())
                    ORDER BY id
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                ), cleared AS (
                    UPDATE agent_attempts a SET grant_id = NULL
                    FROM dead WHERE a.id = dead.id
                    RETURNING a.id
                )
                SELECT id AS attempt_id, job_id, grant_id FROM dead
                """,
                limit,
            )
        return [
            {
                "attempt_id": row["attempt_id"],
                "job_id": row["job_id"],
                "grant_id": row["grant_id"],
            }
            for row in rows
        ]

    async def link_residency_grant(
        self,
        *,
        thread_id: str,
        generation: int,
        grant_id: str,
        binding_id: str | None = None,
    ) -> bool:
        """Record the grant a residency runs on — only its own generation.

        A link that matched nothing means the residency was taken over between
        mint and link; the caller revokes the orphan grant rather than handing
        out a credential nothing owns.
        """
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET residency_grant_id = $3, residency_binding_id = $4,
                        updated_at = NOW()
                    WHERE thread_id = $1 AND residency_generation = $2
                      AND residency_status = 'hot'
                    RETURNING thread_id
                    """,
                    thread_id,
                    generation,
                    grant_id,
                    binding_id,
                )
            )
