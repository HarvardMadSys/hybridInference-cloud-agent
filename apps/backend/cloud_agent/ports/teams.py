"""Persistence for teams, their members, invitations and join requests.

Every operation takes the acting account and decides inside its own
transaction whether that account may act: a caller who is not an active member
of an active team gets :class:`~cloud_agent.domain.teams.TeamNotFound`, and a
member whose role does not allow the requested change gets
:class:`~cloud_agent.domain.teams.TeamForbidden`. Gateway admission and any
other outside check happen before the call; the store re-reads membership
where it commits, so a decision made a moment earlier cannot outlive it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import timedelta

    from cloud_agent.domain.teams import (
        ApplicantRecord,
        AuditRecord,
        InvitationRecord,
        MemberRecord,
        MemberRole,
        OwnJoinRequestRecord,
        TeamRecord,
    )


class TeamRepository(Protocol):
    """Team membership and the invitation flow, fenced per acting account."""

    async def create_team(self, actor: str, *, name: str, request_key: str) -> TeamRecord:
        """Create a team owned by ``actor``; a repeated key returns the same team."""
        ...

    async def list_teams(self, actor: str) -> list[TeamRecord]:
        """Return the active teams ``actor`` is an active member of."""
        ...

    async def get_team(self, actor: str, team_id: str) -> TeamRecord:
        """Return one team ``actor`` is an active member of."""
        ...

    async def rename_team(
        self, actor: str, team_id: str, *, name: str, expected_revision: int
    ) -> TeamRecord:
        """Rename at the revision the owner or admin read."""
        ...

    async def delete_team(self, actor: str, team_id: str, *, expected_revision: int) -> None:
        """Delete a team at the revision the owner read, closing its invitations."""
        ...

    async def list_members(self, actor: str, team_id: str) -> list[MemberRecord]:
        """Return the team's active members, owner first."""
        ...

    async def leave_team(self, actor: str, team_id: str) -> None:
        """End ``actor``'s own membership; the owner must transfer or delete first."""
        ...

    async def remove_member(self, actor: str, team_id: str, user_id: str) -> None:
        """Owners remove non-owners; admins may only remove ordinary members."""
        ...

    async def set_member_role(
        self, actor: str, team_id: str, user_id: str, *, role: MemberRole, expected_revision: int
    ) -> TeamRecord:
        """Grant or revoke admin as the owner; ownership uses its own transfer flow."""
        ...

    async def transfer_ownership(
        self, actor: str, team_id: str, *, user_id: str, expected_revision: int
    ) -> TeamRecord:
        """Make an active member the owner and close pending invitations."""
        ...

    async def create_invitation(
        self,
        actor: str,
        team_id: str,
        *,
        request_key: str,
        token_hash: bytes,
        ttl: timedelta,
    ) -> tuple[InvitationRecord, bool]:
        """Store a new invitation, or return the one this key already made.

        The flag is ``True`` only when this call stored ``token_hash``; a replay
        returns the earlier invitation and ``False``, whose token is gone.
        """
        ...

    async def list_invitations(self, actor: str, team_id: str) -> list[InvitationRecord]:
        """Return all actionable invitations and recent history; owner or admin."""
        ...

    async def revoke_invitation(self, actor: str, team_id: str, invitation_id: str) -> None:
        """Revoke an invitation and cancel a pending request made with it."""
        ...

    async def claim_invitation(self, actor: str, *, token_hash: bytes) -> OwnJoinRequestRecord:
        """Turn a valid link into ``actor``'s join request; repeating returns it."""
        ...

    async def list_own_join_requests(self, actor: str) -> list[OwnJoinRequestRecord]:
        """Return ``actor``'s recent join requests across teams."""
        ...

    async def list_join_requests(self, actor: str, team_id: str) -> list[ApplicantRecord]:
        """Return the team's pending, unexpired requests; owner or admin."""
        ...

    async def get_join_request(self, actor: str, team_id: str, request_id: str) -> ApplicantRecord:
        """Return one of the team's requests; owner or admin."""
        ...

    async def decide_join_request(
        self, actor: str, team_id: str, request_id: str, *, approve: bool
    ) -> ApplicantRecord:
        """Approve or decline a pending request; repeating the decision returns it."""
        ...

    async def list_audit_events(
        self, actor: str, team_id: str, *, before: int | None, limit: int
    ) -> list[AuditRecord]:
        """Return administrative history newest first; owner only."""
        ...
