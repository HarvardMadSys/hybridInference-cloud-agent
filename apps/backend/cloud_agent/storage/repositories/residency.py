"""Postgres implementation of the shared-residency port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.hosts import LIVE_ATTEMPT_HOST
from cloud_agent.storage._sql.mcp_residency import mint_residency_sql
from cloud_agent.storage.deadlocks import retry_deadlock_victim

if TYPE_CHECKING:
    from collections.abc import Sequence

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
        attempt_id: int,
        lease_generation: int,
        host: str,
        runtime_id: str,
        runtime_version: str | None,
        ttl_seconds: float,
        sandbox_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Take (or rotate) a chat's residency; ``None`` = not earned.

        Residency is something the exact worker attempt earns by *currently*
        running the thread's turn on this host, while that host is still
        enrolled. Attempt id, lease generation, current-job ownership,
        membership ownership, status, lease expiry and the host's enrollment
        are checked in the same transaction as rotation, so a delayed request
        from a superseded attempt cannot revoke its successor's grant, and a
        removed machine's saved worker token cannot mint fresh member grants.
        The membership generation only ever increments, and every rotation
        clears the grant so the caller revokes the predecessor's
        (``previous_grant_id``) and mints anew.

        ``runtime_id`` and ``runtime_version`` are accepted for the worker
        contract and not stored: the sandbox the membership belongs to is the
        authority on what runs in it.
        """
        del runtime_id, runtime_version
        async with self._pool.acquire() as conn, conn.transaction():
            attempt = await conn.fetchrow(
                f"""
                SELECT a.id, a.job_id FROM agent_attempts a
                WHERE a.id = $1 AND a.lease_generation = $2 AND a.host_id = $3
                  AND a.status = 'running'
                  AND {LIVE_ATTEMPT_HOST}
                FOR SHARE OF a
                """,
                attempt_id,
                lease_generation,
                host,
            )
            if attempt is None:
                return None
            earns = await conn.fetchrow(
                """
                SELECT j.id AS job_id, j.user_id, j.model
                FROM agent_jobs j
                WHERE j.id = $1 AND j.thread_id = $2
                  AND j.current_attempt_id = $3
                FOR SHARE
                """,
                attempt["job_id"],
                thread_id,
                attempt_id,
            )
            if earns is None or not await conn.fetchval(
                "SELECT lease_expires_at > clock_timestamp() FROM agent_attempts WHERE id = $1",
                attempt_id,
            ):
                return None
            # Attempt, then membership: the order every admission writer keeps.
            member = await conn.fetchrow(
                """
                SELECT m.residency_grant_id, g.host, g.runtime_id, g.runtime_version
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1 AND g.host = $2
                  AND ($3::text IS NULL OR m.sandbox_id = $3)
                  AND m.active_attempt_id = $4
                FOR UPDATE OF m
                """,
                thread_id,
                host,
                sandbox_id,
                attempt_id,
            )
            if member is None:
                return None
            # A deleted chat earns nothing, even from a still-live attempt:
            # the delete released its residency, and a new generation would
            # be one it never closes. The check lives in this statement, not
            # the reads above. Owner deletion updates the running job before
            # releasing the membership, so either it committed before the
            # job lock above was granted and this fresh snapshot sees the
            # tombstone, or it waits for this commit and its release then
            # takes the generation minted here as well.
            row = await conn.fetchrow(
                f"""
                UPDATE agent_thread_sandbox_memberships AS m
                SET {mint_residency_sql("$2")},
                    updated_at = NOW()
                FROM agent_jobs j, agent_attempts a, agent_sandboxes g, agent_threads t
                WHERE m.thread_id = $1 AND m.active_attempt_id = $3
                  AND t.id = m.thread_id AND t.deleted_at IS NULL
                  AND g.id = m.sandbox_id AND g.host = $4
                  AND ($6::text IS NULL OR m.sandbox_id = $6)
                  AND j.thread_id = m.thread_id
                  AND j.current_attempt_id = a.id
                  AND a.id = $3 AND a.job_id = j.id
                  AND a.lease_generation = $5 AND a.host_id = $4
                  AND a.status = 'running'
                  AND a.lease_expires_at > clock_timestamp()
                  AND {LIVE_ATTEMPT_HOST}
                RETURNING m.thread_id, m.sandbox_id,
                          m.residency_generation AS generation,
                          m.residency_status AS status,
                          m.residency_lease_expires_at AS lease_expires_at,
                          m.residency_grant_id AS grant_id,
                          m.residency_binding_id AS ledger_binding_id,
                          m.residency_established_at AS established_at,
                          m.residency_last_active_at AS last_active_at
                """,
                thread_id,
                ttl_seconds,
                attempt_id,
                host,
                lease_generation,
                sandbox_id,
            )
            if row is None:
                return None
        result = dict(row)
        result["host"] = member["host"]
        result["runtime_id"] = member["runtime_id"]
        result["runtime_version"] = member["runtime_version"]
        result["previous_grant_id"] = member["residency_grant_id"]
        result["establishing_job_id"] = earns["job_id"]
        result["establishing_attempt_id"] = attempt["id"]
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
        Expiry is equally final: a hot row waiting for the reaper cannot use a
        late beat to regain authority.
        """
        extended = await self.heartbeat_residencies(
            residencies=[(thread_id, generation)], ttl_seconds=ttl_seconds, host=host
        )
        return extended.get((thread_id, generation))

    async def heartbeat_residencies(
        self,
        *,
        residencies: Sequence[tuple[str, int]],
        ttl_seconds: float,
        host: str | None = None,
    ) -> dict[tuple[str, int], str]:
        """Extend several residencies' leases at once, each as :meth:`heartbeat_residency` does.

        Returns the grant id of every residency extended, by ``(thread_id,
        generation)``; one missing from the answer was not extended. One
        transaction for the whole beat: it locks the rows in thread order
        first, the order every writer that takes several memberships keeps
        (host revocation does the same), and runs again if Postgres picks it
        as a deadlock's victim. Any other error is raised and never answered
        as "not extended": a host tears a sandbox down on that answer.
        """
        beats = list(
            dict.fromkeys((str(thread), int(generation)) for thread, generation in residencies)
        )
        if not beats:
            return {}
        threads = [thread for thread, _ in beats]
        generations = [generation for _, generation in beats]

        async def extend() -> dict[tuple[str, int], str]:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """
                    SELECT m.thread_id FROM agent_thread_sandbox_memberships m
                    JOIN unnest($1::text[], $2::bigint[]) AS beat(thread_id, generation)
                      ON beat.thread_id = m.thread_id
                     AND beat.generation = m.residency_generation
                    WHERE m.residency_status = 'hot'
                    ORDER BY m.thread_id
                    FOR UPDATE OF m
                    """,
                    threads,
                    generations,
                )
                rows = await conn.fetch(
                    """
                    UPDATE agent_thread_sandbox_memberships m
                    SET residency_lease_expires_at =
                            clock_timestamp() + make_interval(secs => $3),
                        residency_last_active_at = NOW()
                    FROM unnest($1::text[], $2::bigint[]) AS beat(thread_id, generation),
                         agent_sandboxes g
                    WHERE m.thread_id = beat.thread_id
                      AND m.residency_generation = beat.generation
                      AND m.residency_status = 'hot'
                      AND m.residency_lease_expires_at > clock_timestamp()
                      AND g.id = m.sandbox_id AND ($4::text IS NULL OR g.host = $4)
                    RETURNING m.thread_id, m.residency_generation,
                              COALESCE(m.residency_grant_id, '') AS grant_id
                    """,
                    threads,
                    generations,
                    ttl_seconds,
                    host,
                )
            return {
                (row["thread_id"], int(row["residency_generation"])): row["grant_id"]
                for row in rows
            }

        return await retry_deadlock_victim("heartbeat_residencies", extend)

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

    async def open_shared_residencies(
        self, *, residencies: Sequence[tuple[str, str, int]]
    ) -> set[tuple[str, str, int]]:
        """Return which ``(sandbox_id, thread_id, generation)`` residencies are still open.

        Open is the generation its member still holds, not yet released. Read
        without locks, and enough to rule a release out: a member never
        changes group, its generation only grows, and a released generation
        is never held again, so one this read finds closed stays closed.
        Whether an open one may be released is
        :meth:`release_shared_residency_if_quiescent`'s to decide.
        """
        if not residencies:
            return set()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.sandbox_id, m.thread_id, m.residency_generation
                FROM agent_thread_sandbox_memberships m
                JOIN unnest($1::text[], $2::text[], $3::bigint[])
                    AS asked(sandbox_id, thread_id, generation)
                  ON asked.thread_id = m.thread_id AND asked.sandbox_id = m.sandbox_id
                 AND asked.generation = m.residency_generation
                WHERE m.residency_status <> 'none'
                """,
                [sandbox_id for sandbox_id, _, _ in residencies],
                [thread_id for _, thread_id, _ in residencies],
                [int(generation) for _, _, generation in residencies],
            )
        return {
            (row["sandbox_id"], row["thread_id"], int(row["residency_generation"])) for row in rows
        }

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
                WHERE m.residency_status = 'hot'
                  AND m.residency_lease_expires_at <= clock_timestamp()
                  AND g.id = m.sandbox_id
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          m.residency_grant_id AS grant_id
                """
            )
        return [dict(row) for row in rows]
