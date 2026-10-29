"""Administrator mail to accounts: the snapshot, the claim and each recipient's outcome.

A broadcast's recipients are fixed in the transaction that creates it, from
accounts whose state and application state the administrator chose and that
have a verified email. One process at a time sends it: a claim moves it to
``sending`` with ``FOR UPDATE SKIP LOCKED``, and a claim older than
:data:`STALE_CLAIM` (a process that died mid-send) can be taken over, which
resumes with the recipients still pending. Each claim carries a fresh
token: the sender holds it before every message and records each
recipient's outcome right after handing that message over, both only while
the token is still the broadcast's (each also refreshes the claim). A
sender that stalled past the stale bound and resumes finds its claim taken
and stops, so nobody is mailed twice except at most the one message in
flight when the claim changed hands. A recipient whose
account was deleted, or whose address changed, before the send is skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.local_accounts import write_audit

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg

#: A claim this old belongs to a process that stopped; another may resume it.
STALE_CLAIM = "15 minutes"

_AUDIENCE_SQL = """
FROM agent_local_accounts a
JOIN agent_local_admissions m ON m.account_id = a.id
JOIN agent_local_login_identities e ON e.account_id = a.id AND e.kind = 'email'
    AND e.verified_at IS NOT NULL
WHERE a.status = ANY($1::text[]) AND a.status <> 'deleted' AND m.status = ANY($2::text[])
  AND a.erased_at IS NULL
"""

#: Refresh a claim that is still the given sender's.
_HOLD_SQL = """
UPDATE agent_local_broadcasts SET claimed_at = NOW()
WHERE id = $1 AND claim_token = $2 AND status = 'sending'
RETURNING 1
"""

_ROW = (
    "id, subject, status, recipient_count, scheduled_at, sent_at, created_by, created_at, "
    "template_key, target_statuses, target_admissions"
)


class BroadcastsMixin:
    """Administrator mail; mixed into the local-account repository."""

    _pool: asyncpg.Pool

    async def broadcast_audience(self, *, statuses: list[str], admissions: list[str]) -> int:
        """How many accounts a broadcast to these states would reach now."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(f"SELECT count(*) {_AUDIENCE_SQL}", statuses, admissions)
            )

    async def create_broadcast(
        self,
        *,
        broadcast_id: str,
        subject: str,
        body_html: str,
        body_text: str,
        template_key: str | None,
        statuses: list[str],
        admissions: list[str],
        scheduled_at: datetime | None,
        created_by: str,
        admin_ip: str,
    ) -> int:
        """Create a scheduled broadcast with its recipients fixed now; how many."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_local_broadcasts
                    (id, subject, body_html, body_text, template_key, target_statuses,
                     target_admissions, scheduled_at, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                broadcast_id,
                subject,
                body_html,
                body_text,
                template_key,
                statuses,
                admissions,
                scheduled_at,
                created_by,
            )
            status = await conn.execute(
                f"""
                INSERT INTO agent_local_broadcast_recipients (broadcast_id, account_id, email)
                SELECT $3, a.id, e.value {_AUDIENCE_SQL}
                """,
                statuses,
                admissions,
                broadcast_id,
            )
            count = int(status.split()[-1])
            await conn.execute(
                "UPDATE agent_local_broadcasts SET recipient_count = $2 WHERE id = $1",
                broadcast_id,
                count,
            )
            await write_audit(
                conn,
                action="agent.local_broadcast.create",
                actor_id=created_by,
                admin_ip=admin_ip,
                account_id=None,
                details={"broadcast_id": broadcast_id, "recipients": count},
            )
            return count

    async def list_broadcasts(self, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        """Broadcasts, newest first, and how many there are."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_ROW} FROM agent_local_broadcasts "
                "ORDER BY created_at DESC, id DESC LIMIT $1 OFFSET $2",
                limit,
                offset,
            )
            total = await conn.fetchval("SELECT count(*) FROM agent_local_broadcasts")
        return [dict(row) for row in rows], int(total)

    async def broadcast_detail(
        self, *, broadcast_id: str, limit: int, offset: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int] | None:
        """One broadcast, a page of its recipients and their total; ``None`` if unknown."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_ROW} FROM agent_local_broadcasts WHERE id = $1", broadcast_id
            )
            if row is None:
                return None
            recipients = await conn.fetch(
                "SELECT account_id, email, status, error, sent_at "
                "FROM agent_local_broadcast_recipients WHERE broadcast_id = $1 "
                "ORDER BY account_id LIMIT $2 OFFSET $3",
                broadcast_id,
                limit,
                offset,
            )
            total = await conn.fetchval(
                "SELECT count(*) FROM agent_local_broadcast_recipients WHERE broadcast_id = $1",
                broadcast_id,
            )
        return dict(row), [dict(r) for r in recipients], int(total)

    async def cancel_broadcast(
        self, *, broadcast_id: str, actor_id: str, admin_ip: str
    ) -> str | None:
        """Cancel a scheduled broadcast; its status after, or ``None`` if unknown.

        Only a broadcast still waiting to be sent can be cancelled; the status
        returned says what it is otherwise.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM agent_local_broadcasts WHERE id = $1 FOR UPDATE", broadcast_id
            )
            if status != "scheduled":
                return status
            await conn.execute(
                "UPDATE agent_local_broadcasts SET status = 'cancelled' WHERE id = $1", broadcast_id
            )
            await write_audit(
                conn,
                action="agent.local_broadcast.cancel",
                actor_id=actor_id,
                admin_ip=admin_ip,
                account_id=None,
                details={"broadcast_id": broadcast_id},
            )
            return "cancelled"

    async def claim_due_broadcast(self) -> dict[str, Any] | None:
        """Claim one broadcast that is due, or whose sender stopped; ``None`` if none."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agent_local_broadcasts
                SET status = 'sending', claimed_at = NOW(), claim_token = gen_random_uuid()::text
                WHERE id = (
                    SELECT id FROM agent_local_broadcasts
                    WHERE (status = 'scheduled' AND (scheduled_at IS NULL OR scheduled_at <= NOW()))
                       OR (status = 'sending' AND claimed_at < NOW() - INTERVAL '{STALE_CLAIM}')
                    ORDER BY coalesce(scheduled_at, created_at), id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                RETURNING id, subject, body_html, body_text, claim_token
                """
            )
        return dict(row) if row is not None else None

    async def pending_broadcast_recipients(
        self, *, broadcast_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """The next recipients still pending, with whether each may still be mailed."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.account_id, r.email,
                       (a.status <> 'deleted' AND a.erased_at IS NULL
                        AND e.value = r.email AND e.verified_at IS NOT NULL) AS deliverable
                FROM agent_local_broadcast_recipients r
                JOIN agent_local_accounts a ON a.id = r.account_id
                LEFT JOIN agent_local_login_identities e
                    ON e.account_id = r.account_id AND e.kind = 'email'
                WHERE r.broadcast_id = $1 AND r.status = 'pending'
                ORDER BY r.account_id LIMIT $2
                """,
                broadcast_id,
                limit,
            )
        return [dict(row) for row in rows]

    async def hold_broadcast_claim(self, *, broadcast_id: str, claim_token: str) -> bool:
        """Refresh the claim if it is still this sender's; ``False`` once another took it."""
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(_HOLD_SQL, broadcast_id, claim_token))

    async def record_broadcast_results(
        self,
        *,
        broadcast_id: str,
        claim_token: str,
        outcomes: list[tuple[str, str, str | None]],
    ) -> bool:
        """Record ``(account_id, status, error)`` for recipients just handled.

        Only while the claim is still this sender's, which it refreshes;
        ``False`` (nothing recorded) once another sender took it over.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval(_HOLD_SQL, broadcast_id, claim_token):
                return False
            await conn.execute(
                """
                UPDATE agent_local_broadcast_recipients AS r
                SET status = u.status, error = u.error,
                    sent_at = CASE WHEN u.status = 'sent' THEN NOW() END
                FROM unnest($2::text[], $3::text[], $4::text[]) AS u(account_id, status, error)
                WHERE r.broadcast_id = $1 AND r.account_id = u.account_id AND r.status = 'pending'
                """,
                broadcast_id,
                [outcome[0] for outcome in outcomes],
                [outcome[1] for outcome in outcomes],
                [outcome[2] for outcome in outcomes],
            )
            return True

    async def finish_broadcast(self, *, broadcast_id: str, claim_token: str) -> str | None:
        """Close a fully handled broadcast: ``failed`` if nothing was sent and something failed.

        ``None`` when the claim is no longer this sender's: its new holder closes it.
        """
        async with self._pool.acquire() as conn:
            status = await conn.fetchval(
                """
                    UPDATE agent_local_broadcasts SET sent_at = NOW(), status = CASE
                        WHEN NOT EXISTS (SELECT 1 FROM agent_local_broadcast_recipients
                                         WHERE broadcast_id = $1 AND status = 'sent')
                         AND EXISTS (SELECT 1 FROM agent_local_broadcast_recipients
                                     WHERE broadcast_id = $1 AND status = 'failed')
                        THEN 'failed' ELSE 'sent' END
                    WHERE id = $1 AND status = 'sending' AND claim_token = $2
                    RETURNING status
                    """,
                broadcast_id,
                claim_token,
            )
        return None if status is None else str(status)


__all__ = ["STALE_CLAIM", "BroadcastsMixin"]
