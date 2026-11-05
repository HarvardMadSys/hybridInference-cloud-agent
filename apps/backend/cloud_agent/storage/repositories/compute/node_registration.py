"""Compute node registration and host keys."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase, _json


class NodeRegistrationMixin(ComputeRepositoryBase):
    """Node registration tokens and the host keys they record."""

    async def create_node_registration(
        self,
        *,
        allocation_id: str,
        token_hash: str,
        ttl_seconds: float,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> dict[str, Any]:
        """Open the one registration a new node may complete; only the hash is kept."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            row = await conn.fetchrow(
                """INSERT INTO agent_compute_node_registrations
                (id,allocation_id,operation_id,token_hash,expires_at)
                VALUES ($1,$2,$3,$4,clock_timestamp()+make_interval(secs=>$5))
                RETURNING id,expires_at,created_at""",
                str(uuid4()),
                allocation_id,
                operation_id,
                token_hash,
                ttl_seconds,
            )
            return dict(row)

    async def complete_node_registration(self, *, token_hash: str, host_keys: list[str]) -> str:
        """Record a node's host keys once: registered, replayed, conflict or invalid.

        Only the newest registration of a live allocation is open. The same
        keys again get the same answer; a different set is never swapped in.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT r.id,r.allocation_id,r.host_keys,r.registered_at,
                    r.expires_at,a.desired_state,a.release_committed_at
                FROM agent_compute_node_registrations r
                JOIN agent_compute_allocations a ON a.id=r.allocation_id
                WHERE r.token_hash=$1 FOR UPDATE OF r""",
                token_hash,
            )
            if row is None:
                return "invalid"
            live = await conn.fetchval(
                "SELECT expires_at > clock_timestamp() "
                "FROM agent_compute_node_registrations WHERE id=$1",
                row["id"],
            )
            if row["registered_at"] is not None:
                return "replayed" if load_json(row["host_keys"]) == host_keys else "conflict"
            newest = await conn.fetchval(
                """SELECT id FROM agent_compute_node_registrations WHERE allocation_id=$1
                ORDER BY created_at DESC,id DESC LIMIT 1""",
                row["allocation_id"],
            )
            if (
                not live
                or row["desired_state"] != "present"
                or row["release_committed_at"] is not None
                or newest != row["id"]
            ):
                return "invalid"
            await conn.execute(
                """UPDATE agent_compute_node_registrations
                SET host_keys=$2::jsonb,registered_at=NOW() WHERE id=$1""",
                row["id"],
                _json(host_keys),
            )
            return "registered"

    async def node_registration(self, *, allocation_id: str) -> dict[str, Any] | None:
        """Read the newest registration of an allocation, without its token."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id,expires_at,host_keys,registered_at,created_at,
                    expires_at<=clock_timestamp() AS expired
                FROM agent_compute_node_registrations WHERE allocation_id=$1
                ORDER BY created_at DESC,id DESC LIMIT 1""",
                allocation_id,
            )
        if row is None:
            return None
        result = dict(row)
        result["host_keys"] = load_json(result["host_keys"]) or []
        return result

    async def reopen_node_registration(
        self,
        *,
        allocation_id: str,
        ttl_seconds: float,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> bool:
        """Give an allocation's newest, never-completed registration another window.

        The node received its one token when it was created, so only that
        token's validity moves. A completed or superseded registration, or one
        of a retiring allocation, is never reopened.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            return bool(
                await conn.fetchval(
                    """UPDATE agent_compute_node_registrations r
                    SET expires_at=clock_timestamp()+make_interval(secs=>$2)
                    FROM agent_compute_allocations a
                    WHERE r.id=(SELECT id FROM agent_compute_node_registrations
                                WHERE allocation_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1)
                      AND a.id=r.allocation_id AND r.registered_at IS NULL
                      AND a.desired_state='present' AND a.release_committed_at IS NULL
                    RETURNING r.id""",
                    allocation_id,
                    ttl_seconds,
                )
            )

    async def record_host_key(
        self,
        *,
        node_id: str,
        host_key: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Pin the host key a node registered, for this exact node instance only."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id)
            updated = await conn.fetchval(
                """UPDATE agent_compute_nodes
                SET connection=connection || jsonb_build_object('host_key',$2::text),updated_at=NOW()
                WHERE id=$1 AND connection <> '{}'::jsonb RETURNING id""",
                node_id,
                host_key,
            )
            if updated is None:
                raise StorageConflict(ConflictCode.RESOURCE_IDENTITY_CHANGED)
