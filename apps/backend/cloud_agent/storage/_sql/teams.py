"""Shared team applicant projection and row decoding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.domain.teams import ApplicantRecord

if TYPE_CHECKING:
    import asyncpg

APPLICANT_VIEW = """
    SELECT r.id, r.user_id, COALESCE(p.first_name, '') AS first_name,
           COALESCE(p.last_name, '') AS last_name, r.created_at, i.expires_at,
           CASE WHEN r.state = 'pending' AND i.expires_at <= NOW() THEN 'expired'
                ELSE r.state END AS state
      FROM agent_team_join_requests AS r
      JOIN agent_team_invitations AS i ON i.id = r.invitation_id
      LEFT JOIN agent_user_profiles AS p ON p.user_id = r.user_id
"""

TEAM_NOT_FOUND = "No such team."


def applicant_from_row(row: asyncpg.Record) -> ApplicantRecord:
    """Decode the applicant projection shared by member and admin views."""
    return ApplicantRecord(
        id=row["id"],
        user_id=row["user_id"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
