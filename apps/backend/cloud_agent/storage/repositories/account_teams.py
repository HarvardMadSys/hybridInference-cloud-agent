"""The teams a deleted account still owns, and handing them to one of their members.

A team belongs to its members as much as to its owner, so erasing an owner
never deletes a team others still use (see
:func:`cloud_agent.storage._sql.local_erasure.settle_teams`). Only the owner
can transfer a team, and a deleted account cannot sign in to do so: an
administrator hands each such team to one of its active members here, as the
owner's own ownership transfer would, before the account is erased.

Lock order: the account row first, as every local-account edit, then the
team (``FOR UPDATE``), then its members, as every team write does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.domain.accounts import AccountTransitionRefused
from cloud_agent.storage._sql.local_accounts import locked_account, write_audit

if TYPE_CHECKING:
    import asyncpg

_OWNED_SQL = """
SELECT t.id, t.name, t.revision FROM agent_teams t
WHERE t.owner_user_id = $1 AND t.state = 'active'
ORDER BY t.id
"""

#: Each active member but the owner, with the identifier their account shows.
_MEMBERS_SQL = """
SELECT m.team_id, m.user_id, m.role,
       coalesce(e.value, n.value, p.value) AS label
FROM agent_team_members m
LEFT JOIN agent_local_login_identities e ON e.account_id = m.user_id AND e.kind = 'email'
LEFT JOIN agent_local_login_identities n ON n.account_id = m.user_id AND n.kind = 'login_name'
LEFT JOIN agent_local_login_identities p ON p.account_id = m.user_id AND p.kind = 'phone'
WHERE m.team_id = ANY($1::text[]) AND m.user_id <> $2 AND m.state = 'active'
ORDER BY m.team_id, (m.role = 'admin') DESC, m.joined_at, m.user_id
"""


class TeamsMixin:
    """Teams of a deleted owner; mixed into the local-account repository."""

    _pool: asyncpg.Pool

    async def owned_teams(self, account_id: str) -> list[dict[str, Any]]:
        """The active teams the account owns, each with its other active members."""
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            return await _owned(conn, account_id)

    async def transfer_owned_team(
        self, *, account_id: str, team_id: str, user_id: str, actor_id: str, admin_ip: str
    ) -> list[dict[str, Any]]:
        """Hand a deleted account's team to one of its active members; the teams it still owns.

        The new owner takes the team as from the owner's own transfer: the
        team's pending links and requests are closed, its revision moves on,
        and the team's audit records the administrator who did it. The old
        owner stays a member until the account is erased.

        Raises:
            AccountTransitionRefused: ``invalid_transition`` for an account
                that is not deleted (or already erased), ``team_not_owned``
                for a team it does not own, ``not_a_member`` for someone not
                an active member of it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await locked_account(conn, account_id)
            if current.status != "deleted" or current.erased_at is not None:
                raise AccountTransitionRefused(
                    "Only a deleted account's teams are handed over by an administrator; an "
                    "active owner transfers its own teams."
                )
            team = await conn.fetchrow(
                "SELECT id FROM agent_teams WHERE id = $1 AND owner_user_id = $2 "
                "AND state = 'active' FOR UPDATE",
                team_id,
                account_id,
            )
            if team is None:
                raise AccountTransitionRefused(
                    "The account does not own this team.", code="team_not_owned"
                )
            if user_id == account_id or not await conn.fetchval(
                "SELECT TRUE FROM agent_team_members "
                "WHERE team_id = $1 AND user_id = $2 AND state = 'active' FOR UPDATE",
                team_id,
                user_id,
            ):
                raise AccountTransitionRefused(
                    "Choose an active member of the team.", code="not_a_member"
                )
            await conn.execute(
                "UPDATE agent_team_members SET role = 'member', updated_at = NOW() "
                "WHERE team_id = $1 AND user_id IN ($2, $3)",
                team_id,
                account_id,
                user_id,
            )
            await conn.execute(
                "UPDATE agent_teams SET owner_user_id = $2, revision = revision + 1, "
                "updated_at = NOW() WHERE id = $1",
                team_id,
                user_id,
            )
            # As the owner's own transfer: what the old owner's team had open
            # is closed, and the new owner starts from a clean slate.
            await conn.execute(
                """
                WITH cancelled AS (
                    UPDATE agent_team_join_requests
                       SET state = 'cancelled', decided_at = NOW(), decided_by_user_id = $2
                     WHERE team_id = $1 AND state = 'pending'
                 RETURNING invitation_id
                )
                UPDATE agent_team_invitations SET state = 'revoked', revoked_at = NOW()
                WHERE team_id = $1
                  AND (state = 'open' OR id IN (SELECT invitation_id FROM cancelled))
                """,
                team_id,
                account_id,
            )
            await conn.execute(
                "INSERT INTO agent_team_audit_events "
                "(team_id, actor_user_id, action, target_user_id, details) "
                "VALUES ($1, $2, 'ownership_transferred', $3, $4::jsonb)",
                team_id,
                actor_id,
                user_id,
                '{"previous_owner_deleted": true}',
            )
            await write_audit(
                conn,
                action="agent.local_account.transfer_team",
                actor_id=actor_id,
                admin_ip=admin_ip,
                account_id=account_id,
                details={"team_id": team_id, "new_owner": user_id},
            )
            return await _owned(conn, account_id)


async def _owned(conn: Any, account_id: str) -> list[dict[str, Any]]:
    teams = [dict(row) | {"members": []} for row in await conn.fetch(_OWNED_SQL, account_id)]
    by_id = {team["id"]: team for team in teams}
    for row in await conn.fetch(_MEMBERS_SQL, list(by_id), account_id):
        by_id[row["team_id"]]["members"].append(
            {"user_id": row["user_id"], "role": row["role"], "label": row["label"]}
        )
    return teams


__all__ = ["TeamsMixin"]
