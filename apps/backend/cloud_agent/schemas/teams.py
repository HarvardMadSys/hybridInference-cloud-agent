"""Wire models for teams, members, invitations and join requests.

Requests are strict and closed: an unknown field, a boolean where a revision
belongs, or a control character in a name is a 422, never a silent coercion.
Responses carry display data only. A member's email comes from the Gateway at
read time and is ``None`` when it cannot be read; no response ever carries an
invitation token except the one that created it.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime  # noqa: TC003 - Pydantic resolves response fields at runtime.
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_agent.domain.teams import (
    MAX_TEAM_NAME_CHARS,
    InvitationStatus,
    JoinRequestState,
    MemberRole,
    TeamRole,
)

#: Characters a link token can contain: ``secrets.token_urlsafe(32)`` output.
INVITATION_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43}$"
_REQUEST_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
_USER_ID_MAX_CHARS = 256


def _clean_name(value: str) -> str:
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError("Team names must not contain control characters")
    value = value.strip()
    if not value:
        raise ValueError("Enter a team name")
    return value


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _NamedTeam(_Request):
    name: str = Field(max_length=MAX_TEAM_NAME_CHARS * 2)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        """Trim, then require a visible name no longer than the stored limit."""
        value = _clean_name(value)
        if len(value) > MAX_TEAM_NAME_CHARS:
            raise ValueError(f"Team names are at most {MAX_TEAM_NAME_CHARS} characters")
        return value


class CreateTeamRequest(_NamedTeam):
    """Create a team owned by the caller; ``request_key`` makes a retry safe."""

    request_key: str = Field(pattern=_REQUEST_KEY_PATTERN)


class RenameTeamRequest(_NamedTeam):
    """Rename at the revision the owner or admin read."""

    expected_revision: int = Field(ge=1)


class TransferOwnershipRequest(_Request):
    """Hand the team to an active member at the revision the owner read."""

    user_id: str = Field(min_length=1, max_length=_USER_ID_MAX_CHARS)
    expected_revision: int = Field(ge=1)


class CreateInvitationRequest(_Request):
    """Create one single-use link; resending the same key never makes a second."""

    request_key: str = Field(pattern=_REQUEST_KEY_PATTERN)


class ClaimInvitationRequest(_Request):
    """Turn a link's token into the caller's join request."""

    token: str = Field(pattern=INVITATION_TOKEN_PATTERN)


class SetTeamMemberRoleRequest(_Request):
    """An owner may grant or revoke admin at the team's read revision."""

    role: MemberRole
    expected_revision: int = Field(ge=1)


class DecideJoinRequest(_Request):
    """An owner or admin's decision on one join request."""

    approve: bool


class TeamResponse(BaseModel):
    """A team as one of its members sees it."""

    id: str
    name: str
    owner_user_id: str
    role: TeamRole
    revision: int
    member_count: int
    created_at: datetime


class TeamListResponse(BaseModel):
    """The caller's active teams."""

    teams: list[TeamResponse]


class TeamPerson(BaseModel):
    """How a member or applicant is shown: names they chose, never their settings."""

    user_id: str
    display_name: str
    email: str | None
    avatar_data_url: str


class TeamMemberResponse(TeamPerson):
    """One active member."""

    role: TeamRole
    joined_at: datetime
    you: bool


class TeamMemberListResponse(BaseModel):
    """The roster, owner first."""

    members: list[TeamMemberResponse]


class InvitationResponse(BaseModel):
    """An invitation's metadata; the link itself is shown once, on creation."""

    id: str
    status: InvitationStatus
    created_by_user_id: str
    created_at: datetime
    expires_at: datetime
    claimed_by_user_id: str | None
    request_state: JoinRequestState | None


class InvitationCreatedResponse(BaseModel):
    """The new invitation and its token.

    ``token`` is ``None`` when the request key was already used: the stored
    hash cannot give the link back, so the client revokes and creates anew.
    """

    invitation: InvitationResponse
    token: str | None


class InvitationListResponse(BaseModel):
    """The team's newest invitations."""

    invitations: list[InvitationResponse]


class JoinRequestResponse(TeamPerson):
    """A join request as an owner or admin reviews it."""

    id: str
    state: JoinRequestState
    created_at: datetime
    expires_at: datetime


class JoinRequestListResponse(BaseModel):
    """Requests waiting for an owner or admin."""

    requests: list[JoinRequestResponse]


class OwnJoinRequestResponse(BaseModel):
    """The applicant's view: which team, who invited them, and where it stands."""

    id: str
    team_id: str
    team_name: str
    inviter_display_name: str
    state: JoinRequestState
    created_at: datetime
    expires_at: datetime


class OwnJoinRequestListResponse(BaseModel):
    """The caller's recent join requests."""

    requests: list[OwnJoinRequestResponse]


class TeamAuditEventResponse(BaseModel):
    """One administrative action, without tokens or secrets."""

    id: int
    actor_user_id: str
    action: Literal[
        "team_created",
        "team_renamed",
        "team_deleted",
        "invitation_created",
        "invitation_revoked",
        "join_requested",
        "join_approved",
        "join_declined",
        "member_left",
        "member_removed",
        "member_role_changed",
        "ownership_transferred",
        "environment_created",
        "environment_updated",
        "environment_archived",
    ]
    target_user_id: str | None
    target_id: str | None
    created_at: datetime


class TeamAuditEventListResponse(BaseModel):
    """A page of audit events, newest first; pass ``next_before`` for the next."""

    events: list[TeamAuditEventResponse]
    next_before: int | None
