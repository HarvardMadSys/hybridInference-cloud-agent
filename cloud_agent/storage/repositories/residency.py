"""Postgres implementation of the shared-residency port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


class PostgresResidencyRepository:
    """The lease that says which host may speak for a retained chat."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def establish_residency(
        self,
        *,
        thread_id: str,
        host: str,
        runtime_id: str,
        runtime_version: str | None,
        ttl_seconds: float,
        sandbox_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Take (or rotate) a chat's residency; ``None`` = not earned.

        Residency is something a host earns by *currently* running the
        thread's turn — attempt on this host, ``running``, lease unexpired —
        and the check shares a transaction and a row lock with the write, so
        a finish or a supersede cannot slip between them, and an expired
        attempt the reaper has not reached yet earns nothing. The lease lives
        on the chat's membership row: its generation only ever increments,
        and every rotation clears the grant so the caller revokes the
        predecessor's (``previous_grant_id``) and mints anew.

        ``runtime_id`` and ``runtime_version`` are accepted for the worker
        contract and not stored: the sandbox the membership belongs to is the
        authority on what runs in it.
        """
        del runtime_id, runtime_version
        async with self._pool.acquire() as conn, conn.transaction():
            earns = await conn.fetchrow(
                """
                SELECT j.id AS job_id, j.user_id, j.model, a.id AS attempt_id
                FROM agent_jobs j
                JOIN agent_attempts a ON a.id = j.current_attempt_id
                WHERE j.thread_id = $1 AND a.host_id = $2
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                LIMIT 1
                FOR SHARE OF a
                """,
                thread_id,
                host,
            )
            if earns is None:
                return None
            # Attempt, then membership: the order every admission writer keeps.
            member = await conn.fetchrow(
                """
                SELECT m.residency_grant_id, g.host, g.runtime_id, g.runtime_version
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1 AND g.host = $2
                  AND ($3::text IS NULL OR m.sandbox_id = $3)
                FOR UPDATE OF m
                """,
                thread_id,
                host,
                sandbox_id,
            )
            if member is None:
                return None
            row = await conn.fetchrow(
                """
                UPDATE agent_thread_sandbox_memberships
                SET residency_generation = residency_generation + 1,
                    residency_status = 'hot',
                    residency_lease_expires_at = NOW() + make_interval(secs => $2),
                    residency_grant_id = NULL,
                    residency_binding_id = NULL,
                    residency_established_at = NOW(),
                    residency_last_active_at = NOW(),
                    updated_at = NOW()
                WHERE thread_id = $1
                RETURNING thread_id, sandbox_id,
                          residency_generation AS generation,
                          residency_status AS status,
                          residency_lease_expires_at AS lease_expires_at,
                          residency_grant_id AS grant_id,
                          residency_binding_id AS ledger_binding_id,
                          residency_established_at AS established_at,
                          residency_last_active_at AS last_active_at
                """,
                thread_id,
                ttl_seconds,
            )
        result = dict(row)
        result["host"] = member["host"]
        result["runtime_id"] = member["runtime_id"]
        result["runtime_version"] = member["runtime_version"]
        result["previous_grant_id"] = member["residency_grant_id"]
        result["establishing_job_id"] = earns["job_id"]
        result["establishing_attempt_id"] = earns["attempt_id"]
        result["user_id"] = earns["user_id"]
        result["model"] = earns["model"]
        return result

    async def heartbeat_residency(
        self, *, thread_id: str, generation: int, ttl_seconds: float, host: str | None = None
    ) -> str | None:
        """Extend a residency's lease — only the generation that holds it.

        Returns ``None`` when nothing was extended (lost/taken over), else the
        residency's grant id — ``""`` when it runs without one — so the caller
        can renew the credential in the same pass. ``host`` narrows the write
        to the machine the row names: a beat is a holder's statement, and a
        guessed (thread, generation) from anywhere else must extend nothing.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_lease_expires_at = NOW() + make_interval(secs => $3),
                    residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.residency_status = 'hot'
                  AND g.id = m.sandbox_id AND ($4::text IS NULL OR g.host = $4)
                RETURNING COALESCE(m.residency_grant_id, '')
                """,
                thread_id,
                generation,
                ttl_seconds,
                host,
            )

    async def release_residency(
        self, *, thread_id: str, generation: int, host: str | None = None
    ) -> str | None:
        """End a residency (teardown/eviction) — only its own generation.

        Returns ``None`` when nothing was released, else the grant id to
        revoke (``""`` when the residency ran without one).
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id AND ($3::text IS NULL OR g.host = $3)
                RETURNING COALESCE(m.residency_grant_id, '')
                """,
                thread_id,
                generation,
                host,
            )

    async def release_shared_residency_if_quiescent(
        self,
        *,
        thread_id: str,
        generation: int,
        process_generation: int,
        host: str,
        sandbox_id: str,
    ) -> str | None:
        """Release a stopped shared member unless a durable transition owns it.

        A host observation is sampled before its heartbeat transaction. Native
        Fork can rotate and mint a new residency after that sample but before
        the heartbeat reaches this method. The group's durable operation fence,
        member attempt, and observed process generation therefore participate
        in the same UPDATE that releases the grant. The membership must also
        have accepted the stopped/lost observation; checking any of these in
        Python would recreate the revoke-after-mint race this method exists to
        prevent.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.sandbox_id = $4 AND g.id = m.sandbox_id AND g.host = $3
                  AND m.residency_status <> 'none'
                  AND m.process_generation <= $5
                  AND m.resident_state IN ('stopped', 'lost')
                  AND m.active_attempt_id IS NULL
                  AND g.active_operation_count = 0
                  AND g.snapshot_state <> 'snapshotting'
                RETURNING COALESCE(m.residency_grant_id, '')
                """,
                thread_id,
                generation,
                host,
                sandbox_id,
                process_generation,
            )

    async def resident_host(self, *, thread_id: str) -> dict[str, Any] | None:
        """The live hot residency for a thread, or ``None``.

        Live means unexpired: the reaper marks expiry on its own schedule,
        and scheduling decisions between passes must not trust a lease the
        clock has already taken away.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT m.thread_id, m.sandbox_id, g.host, g.runtime_id, g.runtime_version,
                       m.residency_generation AS generation,
                       m.residency_status AS status,
                       m.residency_lease_expires_at AS lease_expires_at,
                       m.residency_grant_id AS grant_id,
                       m.residency_binding_id AS ledger_binding_id
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1 AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > NOW()
                """,
                thread_id,
            )
        return None if row is None else dict(row)

    async def reap_expired_residencies(self) -> list[dict[str, Any]]:
        """Mark every hot residency whose lease lapsed as ``none``.

        The control plane's unilateral half of the fence: the host may still
        be running the sandbox, but its generation no longer opens anything —
        not heartbeats, not credentials — and the next turn goes COLD.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none'
                FROM agent_sandboxes g
                WHERE m.residency_status = 'hot' AND m.residency_lease_expires_at <= NOW()
                  AND g.id = m.sandbox_id
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          m.residency_grant_id AS grant_id
                """
            )
        return [dict(row) for row in rows]
