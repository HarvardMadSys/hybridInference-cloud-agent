"""Postgres storage for teams, memberships, invitations and join requests.

**One lock order.** Every write locks the team row first (``FOR UPDATE``), then
the invitation, then the request, then memberships. Claiming starts from a
link token, so it reads the invitation's team without a lock, locks that team,
and only then locks the invitation. Ownership transfer, departures, approvals
and revocations of one team therefore run one at a time, and two of them can
never wait on each other in opposite orders.

**The caller's membership is read where the write commits.** Each operation
re-checks that the actor is an active member with the required role
inside the transaction holding the team lock, so a membership that ended a
moment earlier cannot authorize anything.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.ids import new_team_id, new_team_invitation_id, new_team_join_request_id
from cloud_agent.domain.teams import (
    LISTING_LIMIT,
    MAX_OPEN_INVITATIONS,
    MAX_OWNED_TEAMS,
    MAX_TEAM_MEMBERS,
    ApplicantRecord,
    AuditRecord,
    InvitationRecord,
    InvitationUnavailable,
    MemberRecord,
    MemberRole,
    OwnJoinRequestRecord,
    TeamConflict,
    TeamForbidden,
    TeamNotFound,
    TeamRecord,
)

if TYPE_CHECKING:
    from datetime import timedelta

_TEAM_VIEW = """
    SELECT t.id, t.name, t.owner_user_id, t.revision, t.created_at, m.role AS member_role,
           (SELECT count(*) FROM agent_team_members AS active
             WHERE active.team_id = t.id AND active.state = 'active') AS member_count
      FROM agent_teams AS t
      JOIN agent_team_members AS m
        ON m.team_id = t.id AND m.user_id = $1 AND m.state = 'active'
     WHERE t.state = 'active'
"""

_INVITATION_VIEW = """
    SELECT i.id, i.created_by_user_id, i.expires_at, i.created_at,
           CASE WHEN i.state = 'open' AND i.expires_at <= NOW() THEN 'expired'
                ELSE i.state END AS status,
           r.user_id AS claimed_by_user_id,
           CASE WHEN r.state = 'pending' AND i.expires_at <= NOW() THEN 'expired'
                ELSE r.state END AS request_state
      FROM agent_team_invitations AS i
      LEFT JOIN agent_team_join_requests AS r ON r.invitation_id = i.id
"""

_APPLICANT_VIEW = """
    SELECT r.id, r.user_id, COALESCE(p.first_name, '') AS first_name,
           COALESCE(p.last_name, '') AS last_name, r.created_at, i.expires_at,
           CASE WHEN r.state = 'pending' AND i.expires_at <= NOW() THEN 'expired'
                ELSE r.state END AS state
      FROM agent_team_join_requests AS r
      JOIN agent_team_invitations AS i ON i.id = r.invitation_id
      LEFT JOIN agent_user_profiles AS p ON p.user_id = r.user_id
"""

_OWN_REQUEST_VIEW = """
    SELECT r.id, r.team_id, t.name AS team_name, i.created_by_user_id AS inviter_user_id,
           COALESCE(p.first_name, '') AS inviter_first_name,
           COALESCE(p.last_name, '') AS inviter_last_name, r.created_at, i.expires_at,
           CASE WHEN r.state = 'pending' AND i.expires_at <= NOW() THEN 'expired'
                ELSE r.state END AS state
      FROM agent_team_join_requests AS r
      JOIN agent_team_invitations AS i ON i.id = r.invitation_id
      JOIN agent_teams AS t ON t.id = r.team_id
      LEFT JOIN agent_user_profiles AS p ON p.user_id = i.created_by_user_id
"""

_NOT_FOUND = "No such team."
_OWNER_ONLY = "Only the team owner can do this."


def _team(row: asyncpg.Record, actor: str) -> TeamRecord:
    return TeamRecord(
        id=row["id"],
        name=row["name"],
        owner_user_id=row["owner_user_id"],
        revision=row["revision"],
        member_count=row["member_count"],
        created_at=row["created_at"],
        role="owner" if row["owner_user_id"] == actor else row["member_role"],
    )


def _invitation(row: asyncpg.Record) -> InvitationRecord:
    return InvitationRecord(
        id=row["id"],
        created_by_user_id=row["created_by_user_id"],
        status=row["status"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        claimed_by_user_id=row["claimed_by_user_id"],
        request_state=row["request_state"],
    )


def _applicant(row: asyncpg.Record) -> ApplicantRecord:
    return ApplicantRecord(
        id=row["id"],
        user_id=row["user_id"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _own_request(row: asyncpg.Record) -> OwnJoinRequestRecord:
    return OwnJoinRequestRecord(
        id=row["id"],
        team_id=row["team_id"],
        team_name=row["team_name"],
        inviter_user_id=row["inviter_user_id"],
        inviter_first_name=row["inviter_first_name"],
        inviter_last_name=row["inviter_last_name"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _revision_conflict() -> TeamConflict:
    return TeamConflict("This team changed. Reload it and try again.", code="revision_conflict")


class PostgresTeamRepository:
    """Teams and the invitation flow over the assembled pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool

    # ── Shared steps ──────────────────────────────────────────────────

    async def _lock_team(
        self,
        conn: asyncpg.Connection,
        actor: str,
        team_id: str,
        *,
        owner: bool = False,
        manager: bool = False,
        lock: bool = True,
    ) -> asyncpg.Record:
        """Lock an active team and optionally require owner or management rights.

        Reads pass ``lock=False``: they check the same membership without
        queueing behind the team's writers.
        """
        team = await conn.fetchrow(
            "SELECT id, name, owner_user_id, revision FROM agent_teams "
            "WHERE id = $1 AND state = 'active'" + (" FOR UPDATE" if lock else ""),
            team_id,
        )
        member_role = team is not None and await conn.fetchval(
            "SELECT role FROM agent_team_members "
            "WHERE team_id = $1 AND user_id = $2 AND state = 'active'",
            team_id,
            actor,
        )
        if team is None or not member_role:
            raise TeamNotFound(_NOT_FOUND)
        if owner and team["owner_user_id"] != actor:
            raise TeamForbidden(_OWNER_ONLY)
        if manager and team["owner_user_id"] != actor and member_role != "admin":
            raise TeamForbidden("Only a team owner or admin can do this.")
        return team

    async def _team_view(self, conn: asyncpg.Connection, actor: str, team_id: str) -> TeamRecord:
        row = await conn.fetchrow(_TEAM_VIEW + " AND t.id = $2", actor, team_id)
        if row is None:
            raise TeamNotFound(_NOT_FOUND)
        return _team(row, actor)

    @staticmethod
    async def _audit(
        conn: asyncpg.Connection,
        team_id: str,
        actor: str,
        action: str,
        *,
        target_user_id: str | None = None,
        target_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            "INSERT INTO agent_team_audit_events "
            "(team_id, actor_user_id, action, target_user_id, target_id, details) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            team_id,
            actor,
            action,
            target_user_id,
            target_id,
            json.dumps(details or {}),
        )

    @staticmethod
    async def _close_pending(
        conn: asyncpg.Connection, team_id: str, actor: str, *, inviter: str | None = None
    ) -> None:
        """Cancel undecided requests and revoke every invitation that could still be used."""
        await conn.execute(
            """
            WITH cancelled AS (
                UPDATE agent_team_join_requests
                   SET state = 'cancelled', decided_at = NOW(), decided_by_user_id = $2
                 WHERE team_id = $1 AND state = 'pending'
                   AND invitation_id IN (
                       SELECT id FROM agent_team_invitations
                        WHERE team_id = $1 AND ($3::text IS NULL OR created_by_user_id = $3)
                   )
             RETURNING invitation_id
            )
            UPDATE agent_team_invitations
               SET state = 'revoked', revoked_at = NOW()
             WHERE team_id = $1
               AND ($3::text IS NULL OR created_by_user_id = $3)
               AND (state = 'open' OR id IN (SELECT invitation_id FROM cancelled))
            """,
            team_id,
            actor,
            inviter,
        )

    async def _applicant_view(
        self, conn: asyncpg.Connection, team_id: str, request_id: str
    ) -> ApplicantRecord:
        row = await conn.fetchrow(
            _APPLICANT_VIEW + " WHERE r.team_id = $1 AND r.id = $2", team_id, request_id
        )
        if row is None:
            raise TeamNotFound("No such join request.")
        return _applicant(row)

    # ── Teams ─────────────────────────────────────────────────────────

    async def create_team(self, actor: str, *, name: str, request_key: str) -> TeamRecord:
        """Create the team and its owner's membership in one transaction."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Serializes one account's creates, so the owned-team limit holds
            # under concurrency and a replayed key finds the first create.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('agent-team-owner:' || $1, 0))",
                actor,
            )
            existing = await conn.fetchval(
                "SELECT id FROM agent_teams WHERE created_by_user_id = $1 AND create_request_key = $2",
                actor,
                request_key,
            )
            if existing is not None:
                return await self._team_view(conn, actor, existing)
            owned = await conn.fetchval(
                "SELECT count(*) FROM agent_teams WHERE owner_user_id = $1 AND state = 'active'",
                actor,
            )
            if owned >= MAX_OWNED_TEAMS:
                raise TeamConflict(
                    f"You can own at most {MAX_OWNED_TEAMS} teams.", code="team_limit_reached"
                )
            team_id = new_team_id()
            await conn.execute(
                "INSERT INTO agent_teams "
                "(id, name, owner_user_id, created_by_user_id, create_request_key) "
                "VALUES ($1, $2, $3, $3, $4)",
                team_id,
                name,
                actor,
                request_key,
            )
            await conn.execute(
                "INSERT INTO agent_team_members (team_id, user_id) VALUES ($1, $2)",
                team_id,
                actor,
            )
            await self._audit(conn, team_id, actor, "team_created")
            return await self._team_view(conn, actor, team_id)

    async def list_teams(self, actor: str) -> list[TeamRecord]:
        """List by name, then id, so equal names keep a stable order."""
        rows = await self._pool.fetch(_TEAM_VIEW + " ORDER BY t.name, t.id", actor)
        return [_team(row, actor) for row in rows]

    async def get_team(self, actor: str, team_id: str) -> TeamRecord:
        """Read one team the actor belongs to."""
        async with self._pool.acquire() as conn:
            return await self._team_view(conn, actor, team_id)

    async def rename_team(
        self, actor: str, team_id: str, *, name: str, expected_revision: int
    ) -> TeamRecord:
        """Rename as an owner or admin; the same name changes nothing."""
        async with self._pool.acquire() as conn, conn.transaction():
            team = await self._lock_team(conn, actor, team_id, manager=True)
            if team["revision"] != expected_revision:
                raise _revision_conflict()
            if team["name"] != name:
                await conn.execute(
                    "UPDATE agent_teams SET name = $2, revision = revision + 1, "
                    "updated_at = NOW() WHERE id = $1",
                    team_id,
                    name,
                )
                await self._audit(conn, team_id, actor, "team_renamed", details={"name": name})
            return await self._team_view(conn, actor, team_id)

    async def delete_team(self, actor: str, team_id: str, *, expected_revision: int) -> None:
        """Mark the team deleted; nobody can read it or claim its links afterwards."""
        async with self._pool.acquire() as conn, conn.transaction():
            team = await self._lock_team(conn, actor, team_id, owner=True)
            if team["revision"] != expected_revision:
                raise _revision_conflict()
            await self._close_pending(conn, team_id, actor)
            await conn.execute(
                "UPDATE agent_teams SET state = 'deleted', deleted_at = NOW(), "
                "revision = revision + 1, updated_at = NOW() WHERE id = $1",
                team_id,
            )
            await self._audit(conn, team_id, actor, "team_deleted")

    # ── Members ───────────────────────────────────────────────────────

    async def list_members(self, actor: str, team_id: str) -> list[MemberRecord]:
        """Read the roster in the same transaction that confirms the actor is on it."""
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            team = await self._team_view(conn, actor, team_id)
            rows = await conn.fetch(
                """
                SELECT m.user_id, m.role, m.joined_at, COALESCE(p.first_name, '') AS first_name,
                       COALESCE(p.last_name, '') AS last_name
                  FROM agent_team_members AS m
                  LEFT JOIN agent_user_profiles AS p ON p.user_id = m.user_id
                 WHERE m.team_id = $1 AND m.state = 'active'
                 ORDER BY (m.user_id = $2) DESC, (m.role = 'admin') DESC, m.joined_at, m.user_id
                """,
                team_id,
                team.owner_user_id,
            )
        return [
            MemberRecord(
                user_id=row["user_id"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                joined_at=row["joined_at"],
                role="owner" if row["user_id"] == team.owner_user_id else row["role"],
            )
            for row in rows
        ]

    @staticmethod
    async def _depart(conn: asyncpg.Connection, team_id: str, user_id: str, state: str) -> bool:
        """End an active membership and advance its access generation."""
        ended = await conn.fetchval(
            "UPDATE agent_team_members SET state = $3, departed_at = NOW(), "
            "access_generation = access_generation + 1, updated_at = NOW() "
            "WHERE team_id = $1 AND user_id = $2 AND state = 'active' RETURNING TRUE",
            team_id,
            user_id,
            state,
        )
        return bool(ended)

    async def leave_team(self, actor: str, team_id: str) -> None:
        """Leave as a member; the owner keeps the team until it has another owner."""
        async with self._pool.acquire() as conn, conn.transaction():
            team = await self._lock_team(conn, actor, team_id)
            if team["owner_user_id"] == actor:
                raise TeamConflict(
                    "Transfer ownership or delete the team before leaving.",
                    code="owner_cannot_leave",
                )
            await self._depart(conn, team_id, actor, "left")
            await self._close_pending(conn, team_id, actor, inviter=actor)
            await self._audit(conn, team_id, actor, "member_left", target_user_id=actor)

    async def remove_member(self, actor: str, team_id: str, user_id: str) -> None:
        """Remove a non-owner; an admin may only remove ordinary members."""
        async with self._pool.acquire() as conn, conn.transaction():
            team = await self._lock_team(conn, actor, team_id, manager=True)
            if user_id == team["owner_user_id"]:
                if user_id != actor:
                    raise TeamForbidden(_OWNER_ONLY)
                raise TeamConflict(
                    "Transfer ownership or delete the team before leaving.",
                    code="owner_cannot_leave",
                )
            target_role = await conn.fetchval(
                "SELECT role FROM agent_team_members "
                "WHERE team_id = $1 AND user_id = $2 AND state = 'active'",
                team_id,
                user_id,
            )
            if target_role is None:
                raise TeamNotFound("No such member.")
            if team["owner_user_id"] != actor and target_role != "member":
                raise TeamForbidden("Admins may only remove ordinary members.")
            if not await self._depart(conn, team_id, user_id, "removed"):
                raise TeamNotFound("No such member.")
            await self._close_pending(conn, team_id, actor, inviter=user_id)
            await self._audit(conn, team_id, actor, "member_removed", target_user_id=user_id)

    async def set_member_role(
        self, actor: str, team_id: str, user_id: str, *, role: MemberRole, expected_revision: int
    ) -> TeamRecord:
        """Grant or revoke admin, checking owner and target under the team lock."""
        async with self._pool.acquire() as conn, conn.transaction():
            team = await self._lock_team(conn, actor, team_id, owner=True)
            if team["revision"] != expected_revision:
                raise _revision_conflict()
            if user_id == team["owner_user_id"]:
                raise TeamConflict(
                    "Use ownership transfer to change the owner.", code="already_owner"
                )
            previous = await conn.fetchval(
                "SELECT role FROM agent_team_members "
                "WHERE team_id = $1 AND user_id = $2 AND state = 'active'",
                team_id,
                user_id,
            )
            if previous is None:
                raise TeamNotFound("No such member.")
            if previous != role:
                await conn.execute(
                    "UPDATE agent_team_members SET role = $3, updated_at = NOW() "
                    "WHERE team_id = $1 AND user_id = $2",
                    team_id,
                    user_id,
                    role,
                )
                await conn.execute(
                    "UPDATE agent_teams SET revision = revision + 1, updated_at = NOW() WHERE id = $1",
                    team_id,
                )
                if role == "member":
                    await self._close_pending(conn, team_id, actor, inviter=user_id)
                await self._audit(
                    conn,
                    team_id,
                    actor,
                    "member_role_changed",
                    target_user_id=user_id,
                    details={"from": previous, "to": role},
                )
            return await self._team_view(conn, actor, team_id)

    async def transfer_ownership(
        self, actor: str, team_id: str, *, user_id: str, expected_revision: int
    ) -> TeamRecord:
        """Hand the team to an active member; the old owner stays as a member."""
        async with self._pool.acquire() as conn, conn.transaction():
            team = await self._lock_team(conn, actor, team_id, owner=True)
            if team["revision"] != expected_revision:
                raise _revision_conflict()
            if user_id == actor:
                raise TeamConflict("You already own this team.", code="already_owner")
            target = await conn.fetchval(
                "SELECT TRUE FROM agent_team_members "
                "WHERE team_id = $1 AND user_id = $2 AND state = 'active' FOR UPDATE",
                team_id,
                user_id,
            )
            if not target:
                raise TeamNotFound("No such member.")
            await conn.execute(
                "UPDATE agent_team_members SET role = 'member', updated_at = NOW() "
                "WHERE team_id = $1 AND user_id IN ($2, $3)",
                team_id,
                actor,
                user_id,
            )
            await conn.execute(
                "UPDATE agent_teams SET owner_user_id = $2, revision = revision + 1, "
                "updated_at = NOW() WHERE id = $1",
                team_id,
                user_id,
            )
            # Links and requests the previous owner issued are theirs to vouch
            # for; the new owner starts from a clean slate.
            await self._close_pending(conn, team_id, actor)
            await self._audit(conn, team_id, actor, "ownership_transferred", target_user_id=user_id)
            return await self._team_view(conn, actor, team_id)

    # ── Invitations ───────────────────────────────────────────────────

    async def create_invitation(
        self,
        actor: str,
        team_id: str,
        *,
        request_key: str,
        token_hash: bytes,
        ttl: timedelta,
    ) -> tuple[InvitationRecord, bool]:
        """Store an invitation's hash, or return the one this request key made."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_team(conn, actor, team_id, manager=True)
            existing = await conn.fetchrow(
                _INVITATION_VIEW
                + " WHERE i.team_id = $1 AND i.created_by_user_id = $2 AND i.request_key = $3",
                team_id,
                actor,
                request_key,
            )
            if existing is not None:
                return _invitation(existing), False
            outstanding = await conn.fetchval(
                "SELECT count(*) FROM agent_team_invitations "
                "WHERE team_id = $1 AND state = 'open' AND expires_at > NOW()",
                team_id,
            )
            if outstanding >= MAX_OPEN_INVITATIONS:
                raise TeamConflict(
                    f"This team already has {MAX_OPEN_INVITATIONS} open invitations. "
                    "Revoke one before creating another.",
                    code="invitation_limit_reached",
                )
            invitation_id = new_team_invitation_id()
            await conn.execute(
                "INSERT INTO agent_team_invitations "
                "(id, team_id, created_by_user_id, request_key, token_hash, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, NOW() + $6::interval)",
                invitation_id,
                team_id,
                actor,
                request_key,
                token_hash,
                ttl,
            )
            await self._audit(conn, team_id, actor, "invitation_created", target_id=invitation_id)
            row = await conn.fetchrow(_INVITATION_VIEW + " WHERE i.id = $1", invitation_id)
            return _invitation(row), True

    async def list_invitations(self, actor: str, team_id: str) -> list[InvitationRecord]:
        """Keep every actionable link visible alongside bounded recent history."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_team(conn, actor, team_id, manager=True, lock=False)
            rows = await conn.fetch(
                _INVITATION_VIEW
                + """ WHERE i.team_id = $1 AND (
                    (i.expires_at > NOW() AND (i.state = 'open' OR
                        (i.state = 'claimed' AND r.state = 'pending')))
                    OR i.id IN (SELECT id FROM agent_team_invitations WHERE team_id = $1
                                ORDER BY created_at DESC, id LIMIT $2)
                ) ORDER BY i.created_at DESC, i.id""",
                team_id,
                LISTING_LIMIT,
            )
        return [_invitation(row) for row in rows]

    async def revoke_invitation(self, actor: str, team_id: str, invitation_id: str) -> None:
        """Revoke; a pending request made with the link is cancelled with it."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_team(conn, actor, team_id, manager=True)
            state = await conn.fetchval(
                "SELECT state FROM agent_team_invitations WHERE team_id = $1 AND id = $2 FOR UPDATE",
                team_id,
                invitation_id,
            )
            if state is None:
                raise TeamNotFound("No such invitation.")
            if state == "revoked":
                return
            if state == "claimed":
                request_state = await conn.fetchval(
                    "SELECT state FROM agent_team_join_requests WHERE invitation_id = $1 FOR UPDATE",
                    invitation_id,
                )
                if request_state != "pending":
                    raise TeamConflict(
                        "This invitation has already been used.", code="invitation_closed"
                    )
                await conn.execute(
                    "UPDATE agent_team_join_requests SET state = 'cancelled', "
                    "decided_at = NOW(), decided_by_user_id = $2 WHERE invitation_id = $1",
                    invitation_id,
                    actor,
                )
            await conn.execute(
                "UPDATE agent_team_invitations SET state = 'revoked', revoked_at = NOW() WHERE id = $1",
                invitation_id,
            )
            await self._audit(conn, team_id, actor, "invitation_revoked", target_id=invitation_id)

    async def claim_invitation(self, actor: str, *, token_hash: bytes) -> OwnJoinRequestRecord:
        """Create the claimant's join request; a link becomes the claimant's alone."""
        async with self._pool.acquire() as conn, conn.transaction():
            team_id = await conn.fetchval(
                "SELECT team_id FROM agent_team_invitations WHERE token_hash = $1", token_hash
            )
            if team_id is None:
                raise InvitationUnavailable("This invitation link is no longer valid.")
            team_active = await conn.fetchval(
                "SELECT state = 'active' FROM agent_teams WHERE id = $1 FOR UPDATE", team_id
            )
            invitation = await conn.fetchrow(
                "SELECT id, state, expires_at <= NOW() AS expired "
                "FROM agent_team_invitations WHERE token_hash = $1 FOR UPDATE",
                token_hash,
            )
            if not team_active or invitation is None:
                raise InvitationUnavailable("This invitation link is no longer valid.")
            if invitation["state"] == "claimed":
                own = await conn.fetchrow(
                    _OWN_REQUEST_VIEW + " WHERE r.invitation_id = $1 AND r.user_id = $2",
                    invitation["id"],
                    actor,
                )
                if own is None:
                    raise InvitationUnavailable("This invitation link is no longer valid.")
                return _own_request(own)
            if invitation["state"] == "revoked" or invitation["expired"]:
                raise InvitationUnavailable("This invitation link is no longer valid.")
            if await conn.fetchval(
                "SELECT TRUE FROM agent_team_members "
                "WHERE team_id = $1 AND user_id = $2 AND state = 'active'",
                team_id,
                actor,
            ):
                raise TeamConflict("You are already a member of this team.", code="already_member")
            # A request whose link has expired can no longer be approved; close
            # it so it does not hold the one pending slot this account has here.
            await conn.execute(
                """
                UPDATE agent_team_join_requests AS r SET state = 'expired', decided_at = NOW()
                  FROM agent_team_invitations AS i
                 WHERE i.id = r.invitation_id AND r.team_id = $1 AND r.user_id = $2
                   AND r.state = 'pending' AND i.expires_at <= NOW()
                """,
                team_id,
                actor,
            )
            request_id = new_team_join_request_id()
            try:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO agent_team_join_requests "
                        "(id, team_id, invitation_id, user_id) VALUES ($1, $2, $3, $4)",
                        request_id,
                        team_id,
                        invitation["id"],
                        actor,
                    )
            except asyncpg.UniqueViolationError:
                raise TeamConflict(
                    "You already asked to join this team. Wait for an owner or admin to decide.",
                    code="request_pending",
                ) from None
            await conn.execute(
                "UPDATE agent_team_invitations SET state = 'claimed' WHERE id = $1",
                invitation["id"],
            )
            await self._audit(conn, team_id, actor, "join_requested", target_id=request_id)
            row = await conn.fetchrow(_OWN_REQUEST_VIEW + " WHERE r.id = $1", request_id)
            return _own_request(row)

    # ── Join requests ─────────────────────────────────────────────────

    async def list_own_join_requests(self, actor: str) -> list[OwnJoinRequestRecord]:
        """The applicant's last 30 days of requests, newest first."""
        rows = await self._pool.fetch(
            _OWN_REQUEST_VIEW
            + " WHERE r.user_id = $1 AND r.created_at > NOW() - INTERVAL '30 days'"
            " ORDER BY r.created_at DESC, r.id LIMIT $2",
            actor,
            LISTING_LIMIT,
        )
        return [_own_request(row) for row in rows]

    async def list_join_requests(self, actor: str, team_id: str) -> list[ApplicantRecord]:
        """Requests still waiting for an owner or admin, oldest first."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_team(conn, actor, team_id, manager=True, lock=False)
            rows = await conn.fetch(
                _APPLICANT_VIEW
                + " WHERE r.team_id = $1 AND r.state = 'pending' AND i.expires_at > NOW()"
                " ORDER BY r.created_at, r.id LIMIT $2",
                team_id,
                LISTING_LIMIT,
            )
        return [_applicant(row) for row in rows]

    async def get_join_request(self, actor: str, team_id: str, request_id: str) -> ApplicantRecord:
        """Read one request as an owner or admin."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_team(conn, actor, team_id, manager=True, lock=False)
            return await self._applicant_view(conn, team_id, request_id)

    async def decide_join_request(
        self, actor: str, team_id: str, request_id: str, *, approve: bool
    ) -> ApplicantRecord:
        """Approve into membership or decline, within the link's lifetime."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_team(conn, actor, team_id, manager=True)
            request = await conn.fetchrow(
                "SELECT r.user_id, r.state, i.expires_at <= NOW() AS expired "
                "FROM agent_team_join_requests AS r "
                "JOIN agent_team_invitations AS i ON i.id = r.invitation_id "
                "WHERE r.team_id = $1 AND r.id = $2 FOR UPDATE OF r",
                team_id,
                request_id,
            )
            if request is None:
                raise TeamNotFound("No such join request.")
            decided = "approved" if approve else "declined"
            if request["state"] == decided:
                return await self._applicant_view(conn, team_id, request_id)
            if request["state"] != "pending":
                raise TeamConflict(
                    "This request was already decided or withdrawn.", code="request_closed"
                )
            if request["expired"]:
                raise TeamConflict(
                    "This request expired with its invitation. Send a new invitation.",
                    code="request_expired",
                )
            applicant = request["user_id"]
            if approve:
                members = await conn.fetchval(
                    "SELECT count(*) FROM agent_team_members WHERE team_id = $1 AND state = 'active'",
                    team_id,
                )
                if members >= MAX_TEAM_MEMBERS:
                    raise TeamConflict(
                        f"A team can have at most {MAX_TEAM_MEMBERS} members.", code="team_full"
                    )
                # A returning member keeps the row and moves to a new access
                # generation, so nothing fenced to the old one comes back.
                await conn.execute(
                    """
                    INSERT INTO agent_team_members (team_id, user_id) VALUES ($1, $2)
                    ON CONFLICT (team_id, user_id) DO UPDATE
                       SET state = 'active', role = 'member', departed_at = NULL, joined_at = NOW(),
                           access_generation = agent_team_members.access_generation + 1,
                           updated_at = NOW()
                     WHERE agent_team_members.state <> 'active'
                    """,
                    team_id,
                    applicant,
                )
            await conn.execute(
                "UPDATE agent_team_join_requests SET state = $2, decided_at = NOW(), "
                "decided_by_user_id = $3 WHERE id = $1",
                request_id,
                decided,
                actor,
            )
            await self._audit(
                conn,
                team_id,
                actor,
                "join_approved" if approve else "join_declined",
                target_user_id=applicant,
                target_id=request_id,
            )
            return await self._applicant_view(conn, team_id, request_id)

    # ── Audit ─────────────────────────────────────────────────────────

    async def list_audit_events(
        self, actor: str, team_id: str, *, before: int | None, limit: int
    ) -> list[AuditRecord]:
        """Page backwards from ``before`` (exclusive)."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_team(conn, actor, team_id, owner=True, lock=False)
            rows = await conn.fetch(
                "SELECT id, actor_user_id, action, target_user_id, target_id, created_at "
                "FROM agent_team_audit_events "
                "WHERE team_id = $1 AND ($2::bigint IS NULL OR id < $2) "
                "ORDER BY id DESC LIMIT $3",
                team_id,
                before,
                limit,
            )
        return [
            AuditRecord(
                id=row["id"],
                actor_user_id=row["actor_user_id"],
                action=row["action"],
                target_user_id=row["target_user_id"],
                target_id=row["target_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
