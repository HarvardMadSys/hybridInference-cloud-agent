"""The external inference provisioning ledger (migration 0116).

One row per account says where its invitation stands. The worker in
:mod:`cloud_agent.application.account_provisioning` is the only writer in
the running service; the operator backfill command writes ``pending`` rows
for accounts approved before activation. Nothing here holds a credential:
the invitation token lives only in the email and at the inference service,
and the issued API key never reaches this service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.local_accounts import write_audit

if TYPE_CHECKING:
    import asyncpg

_ACTOR = "inference-provisioning"

#: Accounts whose Agent use the shared access view currently grants.
_ELIGIBLE = """
    a.status = 'active' AND m.status = 'approved' AND x.identity_verified
    AND e.value IS NOT NULL
"""

_FROM = """
    FROM agent_local_accounts a
    JOIN agent_local_admissions m ON m.account_id = a.id
    JOIN agent_local_account_access x ON x.account_id = a.id
    LEFT JOIN agent_local_login_identities e ON e.account_id = a.id AND e.kind = 'email'
"""


class PostgresAccountProvisioningRepository:
    """Provisioning rows over the control plane's pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def ensure_activation(self) -> Any:
        """Record first activation if absent; return when provisioning began."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_account_provisioning_activation (id) VALUES (1) "
                "ON CONFLICT (id) DO NOTHING"
            )
            return await conn.fetchval(
                "SELECT activated_at FROM agent_account_provisioning_activation WHERE id = 1"
            )

    async def enqueue_new_approvals(self, *, activated_at: Any, limit: int = 50) -> int:
        """Queue accounts approved at or after activation; return how many.

        A revoked row whose account was approved again after the revocation
        is queued afresh; every other existing row is left as it stands, so
        accounts approved before activation are never invited from here.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO agent_account_provisioning (account_id, email)
                SELECT a.id, e.value {_FROM}
                WHERE {_ELIGIBLE} AND m.reviewed_at >= $1
                ON CONFLICT (account_id) DO NOTHING
                """,
                activated_at,
            )
            # An account approved again after its revocation is queued afresh:
            # its new review is later than the revocation on its row.
            revived = await conn.execute(
                f"""
                UPDATE agent_account_provisioning p
                SET state = 'pending', attempts = 0, last_error = NULL,
                    revoked_at = NULL, updated_at = NOW()
                {_FROM}
                WHERE p.account_id = a.id AND p.state = 'revoked'
                  AND {_ELIGIBLE} AND m.reviewed_at > p.revoked_at
                """
            )
        inserted = int(result.split()[-1]) if result and result.split()[-1].isdigit() else 0
        revived_count = int(revived.split()[-1]) if revived and revived.split()[-1].isdigit() else 0
        return inserted + revived_count

    async def enqueue_accounts(self, account_ids: list[str]) -> int:
        """Queue named eligible accounts regardless of approval time (backfill)."""
        if not account_ids:
            return 0
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO agent_account_provisioning (account_id, email)
                SELECT a.id, e.value {_FROM}
                WHERE {_ELIGIBLE} AND a.id = ANY($1::text[])
                ON CONFLICT (account_id) DO UPDATE
                    SET state = 'pending', email = EXCLUDED.email, updated_at = NOW()
                    WHERE agent_account_provisioning.state = 'revoked'
                """,
                account_ids,
            )
        return int(result.split()[-1]) if result else 0

    async def eligible_account_ids(self, *, limit: int = 100000) -> list[str]:
        """Every currently eligible account id (the backfill's dry-run list)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT a.id {_FROM} WHERE {_ELIGIBLE} ORDER BY a.created_at LIMIT $1",
                limit,
            )
        return [row["id"] for row in rows]

    async def pending(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Invitation rows waiting to be minted and mailed, oldest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT account_id, email, attempts FROM agent_account_provisioning "
                "WHERE state = 'pending' ORDER BY created_at LIMIT $1",
                limit,
            )
        return [dict(row) for row in rows]

    async def invited(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Rows whose mail went out, for completion polling."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT account_id, email FROM agent_account_provisioning "
                "WHERE state = 'invited' ORDER BY invited_at LIMIT $1",
                limit,
            )
        return [dict(row) for row in rows]

    async def access_ended(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Live rows whose account no longer has Agent use."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT p.account_id, p.state
                FROM agent_account_provisioning p
                JOIN agent_local_accounts a ON a.id = p.account_id
                JOIN agent_local_admissions m ON m.account_id = a.id
                JOIN agent_local_account_access x ON x.account_id = a.id
                LEFT JOIN agent_local_login_identities e
                    ON e.account_id = a.id AND e.kind = 'email'
                WHERE p.state IN ('pending', 'invited', 'provisioned')
                  AND NOT ({_ELIGIBLE})
                ORDER BY p.updated_at LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def record_attempt(self, account_id: str, *, error: str) -> None:
        """Note a failed pass against a row; it stays queued for the next one."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_account_provisioning SET attempts = attempts + 1, "
                "last_error = $2, updated_at = NOW() WHERE account_id = $1",
                account_id,
                error[:500],
            )

    async def mark_invited(self, account_id: str) -> None:
        """Record that the invitation mail went out, with its audit row."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_account_provisioning SET state = 'invited', "
                "invited_at = NOW(), last_error = NULL, updated_at = NOW() "
                "WHERE account_id = $1",
                account_id,
            )
            await write_audit(
                conn,
                action="agent.local_account.provision_invite",
                actor_id=_ACTOR,
                admin_ip="worker",
                account_id=account_id,
                details={"state": "invited"},
            )

    async def mark_provisioned(self, account_id: str) -> None:
        """Record that the inference service holds a live account for this one."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_account_provisioning SET state = 'provisioned', "
                "provisioned_at = COALESCE(provisioned_at, NOW()), last_error = NULL, "
                "updated_at = NOW() WHERE account_id = $1",
                account_id,
            )

    async def mark_revoked(self, account_id: str, *, audited: bool) -> None:
        """Record that the scoped key is revoked (or was never issued)."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_account_provisioning SET state = 'revoked', "
                "revoked_at = NOW(), last_error = NULL, updated_at = NOW() "
                "WHERE account_id = $1",
                account_id,
            )
            if audited:
                await write_audit(
                    conn,
                    action="agent.local_account.provision_revoke",
                    actor_id=_ACTOR,
                    admin_ip="worker",
                    account_id=account_id,
                    details={"state": "revoked"},
                )
