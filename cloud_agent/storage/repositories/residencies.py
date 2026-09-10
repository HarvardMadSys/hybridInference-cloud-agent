"""Residency lifecycles that outlive a single attempt."""

from __future__ import annotations

from typing import Any

from cloud_agent.storage.repositories._base import PostgresRepository


class PostgresResidencyRepository(PostgresRepository):
    """Residency lifecycles that outlive a single attempt."""

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
        """Take (or take over) a thread's residency; ``None`` = not earned.

        Residency is something a host earns by *currently* running the
        thread's turn — attempt on this host, ``running``, lease unexpired —
        and the check shares a transaction and a row lock with the write, so
        a finish or a supersede cannot slip between them, and an expired
        attempt the reaper has not reached yet earns nothing (review
        finding 4). Atomic across racers: two hosts serialize on the row,
        each gets a distinct generation, and the larger one owns the thread.
        """
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
            # The predecessor's grant, captured under the same transaction: a
            # takeover is a credential rotation, and the old generation's
            # grant must be revocable by the caller that just superseded it.
            previous_grant_id = await conn.fetchval(
                "SELECT grant_id FROM agent_thread_residencies WHERE thread_id = $1 FOR UPDATE",
                thread_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO agent_thread_residencies
                    (thread_id, host, sandbox_id, runtime_id, runtime_version,
                     status, lease_expires_at)
                VALUES ($1, $2, $3, $4, $5, 'hot',
                        NOW() + make_interval(secs => $6))
                ON CONFLICT (thread_id) DO UPDATE SET
                    generation = agent_thread_residencies.generation + 1,
                    host = EXCLUDED.host,
                    sandbox_id = EXCLUDED.sandbox_id,
                    runtime_id = EXCLUDED.runtime_id,
                    runtime_version = EXCLUDED.runtime_version,
                    status = 'hot',
                    grant_id = NULL, ledger_binding_id = NULL,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    established_at = NOW(),
                    last_active_at = NOW()
                RETURNING *
                """,
                thread_id,
                host,
                sandbox_id,
                runtime_id,
                runtime_version,
                ttl_seconds,
            )
        result = dict(row)
        result["previous_grant_id"] = previous_grant_id
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
                UPDATE agent_thread_residencies
                SET lease_expires_at = NOW() + make_interval(secs => $3),
                    last_active_at = NOW()
                WHERE thread_id = $1 AND generation = $2 AND status = 'hot'
                  AND ($4::text IS NULL OR host = $4)
                RETURNING COALESCE(grant_id, '')
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
                UPDATE agent_thread_residencies
                SET status = 'none', last_active_at = NOW()
                WHERE thread_id = $1 AND generation = $2 AND status <> 'none'
                  AND ($3::text IS NULL OR host = $3)
                RETURNING COALESCE(grant_id, '')
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
                UPDATE agent_thread_residencies r
                SET status = 'none', last_active_at = NOW()
                WHERE r.thread_id = $1 AND r.generation = $2
                  AND r.host = $3 AND r.sandbox_id = $4
                  AND r.status <> 'none'
                  AND EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships m
                      WHERE m.thread_id = $1 AND m.sandbox_id = $4
                        AND m.residency_generation = $2
                        AND m.process_generation <= $5
                        AND m.resident_state IN ('stopped', 'lost')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_sandboxes g
                      WHERE g.id = $4
                        AND (g.active_operation_count > 0
                             OR g.snapshot_state = 'snapshotting')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships m
                      WHERE m.thread_id = $1 AND m.sandbox_id = $4
                        AND m.active_attempt_id IS NOT NULL
                  )
                RETURNING COALESCE(r.grant_id, '')
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
                SELECT * FROM agent_thread_residencies
                WHERE thread_id = $1 AND status = 'hot' AND lease_expires_at > NOW()
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
                UPDATE agent_thread_residencies
                SET status = 'none'
                WHERE status = 'hot' AND lease_expires_at <= NOW()
                RETURNING thread_id, generation, host, grant_id
                """
            )
        return [dict(row) for row in rows]
