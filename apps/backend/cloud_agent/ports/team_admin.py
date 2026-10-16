"""The platform administrator's read-only view of every team.

Unlike :class:`~cloud_agent.ports.teams.TeamRepository`, nothing here is fenced
by membership: the route that reaches it requires an Agent administrator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cloud_agent.domain.teams import AdminAuditRecord, AdminTeamDetail, AdminTeamRecord


class TeamAdminQueries(Protocol):
    """The platform administrator's read-only view of every team.

    Not fenced by membership: the route that reaches it requires an Agent
    administrator. Nothing here changes a team.
    """

    async def list_teams_for_admin(
        self, *, query: str | None, state: str, limit: int, offset: int
    ) -> tuple[list[AdminTeamRecord], bool]:
        """Teams newest first, and whether more follow.

        ``query`` matches part of a name, a team id, or a member's user id;
        ``state`` is ``active``, ``deleted`` or ``all``.
        """
        ...

    async def get_team_for_admin(self, team_id: str) -> AdminTeamDetail:
        """One team in any state, its memberships in every state, open requests and links."""
        ...

    async def list_team_audit_for_admin(
        self, team_id: str, *, before: int | None, limit: int
    ) -> list[AdminAuditRecord]:
        """Page a team's audit events backwards from ``before`` (exclusive)."""
        ...
