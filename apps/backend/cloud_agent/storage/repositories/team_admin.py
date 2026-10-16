"""The platform administrator's read-only view of every team.

Separate from :mod:`cloud_agent.storage.repositories.teams`, whose every read
and write is fenced by the acting account's membership. Nothing here is: the
route in front of it requires an Agent administrator, and nothing here changes
a team. Keeping the two apart means no member-facing path can reach an
unfenced read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.domain.teams import (
    LISTING_LIMIT,
    AdminAuditRecord,
    AdminMemberRecord,
    AdminTeamDetail,
    AdminTeamRecord,
    TeamNotFound,
)
from cloud_agent.storage.repositories.teams import _APPLICANT_VIEW, _NOT_FOUND, _applicant

if TYPE_CHECKING:
    import asyncpg

_ADMIN_TEAM_VIEW = """
    SELECT t.id, t.name, t.state, t.owner_user_id, t.created_at, t.deleted_at,
           COALESCE(p.first_name, '') AS owner_first_name,
           COALESCE(p.last_name, '') AS owner_last_name,
           (SELECT count(*) FROM agent_team_members AS m
             WHERE m.team_id = t.id AND m.state = 'active') AS member_count,
           (SELECT count(*) FROM agent_team_members AS m
             WHERE m.team_id = t.id AND m.state = 'active' AND m.role = 'admin'
               AND m.user_id <> t.owner_user_id) AS admin_count,
           (SELECT count(*) FROM agent_team_join_requests AS r
              JOIN agent_team_invitations AS i ON i.id = r.invitation_id
             WHERE r.team_id = t.id AND r.state = 'pending'
               AND i.expires_at > NOW()) AS pending_request_count
      FROM agent_teams AS t
      LEFT JOIN agent_user_profiles AS p ON p.user_id = t.owner_user_id
"""


def _admin_team(row: asyncpg.Record) -> AdminTeamRecord:
    return AdminTeamRecord(
        id=row["id"],
        name=row["name"],
        state=row["state"],
        owner_user_id=row["owner_user_id"],
        owner_first_name=row["owner_first_name"],
        owner_last_name=row["owner_last_name"],
        member_count=row["member_count"],
        admin_count=row["admin_count"],
        pending_request_count=row["pending_request_count"],
        created_at=row["created_at"],
        deleted_at=row["deleted_at"],
    )


def _like_pattern(text: str) -> str:
    """Match ``text`` anywhere, with LIKE's own wildcards taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class PostgresTeamAdminQueries:
    """Every team, in every state, for the platform administrator; reads only."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool

    async def list_teams_for_admin(
        self, *, query: str | None, state: str, limit: int, offset: int
    ) -> tuple[list[AdminTeamRecord], bool]:
        """Teams newest first; one row past ``limit`` says whether more follow."""
        rows = await self._pool.fetch(
            _ADMIN_TEAM_VIEW
            + """
             WHERE ($1::text = 'all' OR t.state = $1)
               AND ($2::text IS NULL
                    OR t.name ILIKE $3 ESCAPE '\\'
                    OR t.id = $2
                    OR EXISTS (SELECT 1 FROM agent_team_members AS member
                                WHERE member.team_id = t.id AND member.user_id = $2))
             ORDER BY t.created_at DESC, t.id DESC
             LIMIT $4 OFFSET $5
            """,
            state,
            query,
            _like_pattern(query) if query is not None else None,
            limit + 1,
            offset,
        )
        return [_admin_team(row) for row in rows[:limit]], len(rows) > limit

    async def get_team_for_admin(self, team_id: str) -> AdminTeamDetail:
        """Read the team, its roster, open requests and link counts in one snapshot."""
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(_ADMIN_TEAM_VIEW + " WHERE t.id = $1", team_id)
            if row is None:
                raise TeamNotFound(_NOT_FOUND)
            team = _admin_team(row)
            members = await conn.fetch(
                """
                SELECT m.user_id, m.role, m.state, m.joined_at, m.departed_at,
                       COALESCE(p.first_name, '') AS first_name,
                       COALESCE(p.last_name, '') AS last_name
                  FROM agent_team_members AS m
                  LEFT JOIN agent_user_profiles AS p ON p.user_id = m.user_id
                 WHERE m.team_id = $1
                 ORDER BY (m.state = 'active') DESC, (m.user_id = $2) DESC,
                          (m.role = 'admin') DESC, m.joined_at, m.user_id
                """,
                team_id,
                team.owner_user_id,
            )
            requests = await conn.fetch(
                _APPLICANT_VIEW
                + " WHERE r.team_id = $1 AND r.state = 'pending' AND i.expires_at > NOW()"
                " ORDER BY r.created_at, r.id LIMIT $2",
                team_id,
                LISTING_LIMIT,
            )
            counts = await conn.fetch(
                """
                SELECT CASE WHEN state = 'open' AND expires_at <= NOW() THEN 'expired'
                            ELSE state END AS status,
                       count(*) AS count
                  FROM agent_team_invitations
                 WHERE team_id = $1
                 GROUP BY 1
                """,
                team_id,
            )
        invitations = dict.fromkeys(("open", "expired", "claimed", "revoked"), 0)
        invitations.update({row["status"]: row["count"] for row in counts})
        return AdminTeamDetail(
            team=team,
            members=[
                AdminMemberRecord(
                    user_id=member["user_id"],
                    first_name=member["first_name"],
                    last_name=member["last_name"],
                    # The owner is whoever the team names, while still a member.
                    role=(
                        "owner"
                        if member["user_id"] == team.owner_user_id and member["state"] == "active"
                        else member["role"]
                    ),
                    state=member["state"],
                    joined_at=member["joined_at"],
                    departed_at=member["departed_at"],
                )
                for member in members
            ],
            join_requests=[_applicant(request) for request in requests],
            invitations=invitations,
        )

    async def list_team_audit_for_admin(
        self, team_id: str, *, before: int | None, limit: int
    ) -> list[AdminAuditRecord]:
        """Page backwards from ``before`` (exclusive), with each person's profile name."""
        async with self._pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM agent_teams WHERE id = $1", team_id) is None:
                raise TeamNotFound(_NOT_FOUND)
            rows = await conn.fetch(
                """
                SELECT e.id, e.actor_user_id, e.action, e.target_user_id, e.target_id,
                       e.created_at,
                       COALESCE(actor.first_name, '') AS actor_first_name,
                       COALESCE(actor.last_name, '') AS actor_last_name,
                       COALESCE(target.first_name, '') AS target_first_name,
                       COALESCE(target.last_name, '') AS target_last_name
                  FROM agent_team_audit_events AS e
                  LEFT JOIN agent_user_profiles AS actor ON actor.user_id = e.actor_user_id
                  LEFT JOIN agent_user_profiles AS target ON target.user_id = e.target_user_id
                 WHERE e.team_id = $1 AND ($2::bigint IS NULL OR e.id < $2)
                 ORDER BY e.id DESC
                 LIMIT $3
                """,
                team_id,
                before,
                limit,
            )
        return [
            AdminAuditRecord(
                id=row["id"],
                actor_user_id=row["actor_user_id"],
                actor_first_name=row["actor_first_name"],
                actor_last_name=row["actor_last_name"],
                action=row["action"],
                target_user_id=row["target_user_id"],
                target_first_name=row["target_first_name"],
                target_last_name=row["target_last_name"],
                target_id=row["target_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
