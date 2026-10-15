"""Team flows that need more than the store: link tokens and Gateway checks.

The store decides membership and ownership. Two questions only the Gateway can
answer are asked here, before the store commits:

- **Approving a request** asks whether the applicant may currently use Agent.
  Holding a link and having once signed in is not admission; the Gateway's
  current decision is.
- **Transferring ownership** asks the same of the new owner, so a team is never
  handed to an account that could not run anything in it.

Both fail closed: an explicit "no" is a refusal the owner can read, and a
Gateway that does not answer is :class:`~cloud_agent.owner_access.AgentAccessUnavailable`,
which the API reports as temporarily unavailable rather than as a verdict.

Display enrichment lives here too. Members and applicants are shown by the
names their own profile chose, then by their Gateway account email, read per
request and never stored; a Gateway that cannot answer leaves the email out
rather than failing the whole roster.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from typing import TYPE_CHECKING

from cloud_agent.domain.teams import INVITATION_TTL, TeamConflict, TeamForbidden, TeamNotFound
from cloud_agent.gateway import GatewayError
from cloud_agent.owner_access import owner_may_use_agent
from cloud_agent.schemas import (
    AdminTeamAuditEvent,
    AdminTeamDetailResponse,
    AdminTeamInvitationCounts,
    AdminTeamMember,
    AdminTeamSummary,
    InvitationCreatedResponse,
    InvitationResponse,
    JoinRequestResponse,
    OwnJoinRequestResponse,
    TeamMemberResponse,
    TeamPerson,
)
from cloud_agent.user_avatar import generate_user_avatar

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

    from cloud_agent.domain.teams import (
        AdminAuditRecord,
        AdminTeamDetail,
        AdminTeamRecord,
        ApplicantRecord,
        InvitationRecord,
        MemberRecord,
        OwnJoinRequestRecord,
        TeamRecord,
    )
    from cloud_agent.ports.teams import TeamRepository

#: Gateway email lookups one roster read runs at once.
_EMAIL_CONCURRENCY = 8


def new_invitation_token() -> tuple[str, bytes]:
    """Return a fresh 256-bit link token and the hash the store keeps of it."""
    token = secrets.token_urlsafe(32)
    return token, hash_invitation_token(token)


def hash_invitation_token(token: str) -> bytes:
    """Hash a link token for lookup; the token itself is never stored."""
    return hashlib.sha256(token.encode("ascii")).digest()


async def _emails(gateway: Any, user_ids: Iterable[str]) -> dict[str, str | None]:
    """Read account emails for display; one that cannot be read is ``None``."""
    limit = asyncio.Semaphore(_EMAIL_CONCURRENCY)

    async def one(user_id: str) -> tuple[str, str | None]:
        async with limit:
            try:
                return user_id, await gateway.user_email(user_id)
            except GatewayError:
                return user_id, None

    unique = list(dict.fromkeys(user_ids))
    return dict(await asyncio.gather(*(one(user_id) for user_id in unique)))


def _display_name(first_name: str, last_name: str, email: str | None) -> str:
    name = " ".join(part for part in (first_name, last_name) if part)
    return name or email or ""


async def _require_admitted(gateway: Any, user_id: str, *, message: str, code: str) -> None:
    if not await owner_may_use_agent(gateway, user_id=user_id):
        raise TeamConflict(message, code=code)


async def member_responses(
    gateway: Any, actor: str, members: list[MemberRecord]
) -> list[TeamMemberResponse]:
    """Attach display names, emails and companions to the roster."""
    emails = await _emails(gateway, (member.user_id for member in members))
    return [
        TeamMemberResponse(
            user_id=member.user_id,
            display_name=_display_name(member.first_name, member.last_name, emails[member.user_id]),
            email=emails[member.user_id],
            avatar_data_url=generate_user_avatar(member.user_id).data_url,
            role=member.role,
            joined_at=member.joined_at,
            you=member.user_id == actor,
        )
        for member in members
    ]


async def applicant_responses(
    gateway: Any, applicants: list[ApplicantRecord]
) -> list[JoinRequestResponse]:
    """Show each applicant the way the owner needs to recognize them."""
    emails = await _emails(gateway, (applicant.user_id for applicant in applicants))
    return [
        JoinRequestResponse(
            id=applicant.id,
            user_id=applicant.user_id,
            display_name=_display_name(
                applicant.first_name, applicant.last_name, emails[applicant.user_id]
            ),
            email=emails[applicant.user_id],
            avatar_data_url=generate_user_avatar(applicant.user_id).data_url,
            state=applicant.state,
            created_at=applicant.created_at,
            expires_at=applicant.expires_at,
        )
        for applicant in applicants
    ]


async def own_request_responses(
    gateway: Any, requests: list[OwnJoinRequestRecord]
) -> list[OwnJoinRequestResponse]:
    """Name each request's team and inviter, and nothing else of the team."""
    unnamed = (
        request.inviter_user_id
        for request in requests
        if not (request.inviter_first_name or request.inviter_last_name)
    )
    emails = await _emails(gateway, unnamed)
    return [
        OwnJoinRequestResponse(
            id=request.id,
            team_id=request.team_id,
            team_name=request.team_name,
            inviter_display_name=_display_name(
                request.inviter_first_name,
                request.inviter_last_name,
                emails.get(request.inviter_user_id),
            ),
            state=request.state,
            created_at=request.created_at,
            expires_at=request.expires_at,
        )
        for request in requests
    ]


def invitation_response(invitation: InvitationRecord) -> InvitationResponse:
    """The invitation's metadata, identical whether or not its token is shown."""
    return InvitationResponse(
        id=invitation.id,
        status=invitation.status,
        created_by_user_id=invitation.created_by_user_id,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        claimed_by_user_id=invitation.claimed_by_user_id,
        request_state=invitation.request_state,
    )


async def create_invitation(
    teams: TeamRepository, actor: str, team_id: str, *, request_key: str
) -> InvitationCreatedResponse:
    """Mint a link; a replayed key returns the earlier invitation without one."""
    token, token_hash = new_invitation_token()
    invitation, created = await teams.create_invitation(
        actor, team_id, request_key=request_key, token_hash=token_hash, ttl=INVITATION_TTL
    )
    return InvitationCreatedResponse(
        invitation=invitation_response(invitation), token=token if created else None
    )


async def decide_join_request(
    teams: TeamRepository,
    gateway: Any,
    actor: str,
    team_id: str,
    request_id: str,
    *,
    approve: bool,
) -> JoinRequestResponse:
    """Approve only an applicant the Gateway admits now; declining asks nothing."""
    if approve:
        request = await teams.get_join_request(actor, team_id, request_id)
        if request.state == "pending":
            await _require_admitted(
                gateway,
                request.user_id,
                message="This account cannot use Agent right now, so it cannot join.",
                code="applicant_not_admitted",
            )
    decided = await teams.decide_join_request(actor, team_id, request_id, approve=approve)
    (response,) = await applicant_responses(gateway, [decided])
    return response


async def transfer_ownership(
    teams: TeamRepository,
    gateway: Any,
    actor: str,
    team_id: str,
    *,
    user_id: str,
    expected_revision: int,
) -> TeamRecord:
    """Hand the team only to a member the Gateway admits now.

    The roster is read first so the Gateway is only ever asked about someone
    already in the team; an owner cannot use this to learn another account's
    standing. The store checks owner, revision and membership again as it commits.
    """
    team = await teams.get_team(actor, team_id)
    if team.role != "owner":
        raise TeamForbidden("Only the team owner can do this.")
    if user_id != actor:
        members = await teams.list_members(actor, team_id)
        if not any(member.user_id == user_id for member in members):
            raise TeamNotFound("No such member.")
        await _require_admitted(
            gateway,
            user_id,
            message="This member cannot use Agent right now, so they cannot own the team.",
            code="owner_not_admitted",
        )
    return await teams.transfer_ownership(
        actor, team_id, user_id=user_id, expected_revision=expected_revision
    )


# ── The platform administrator's read-only view ─────────────────────────────


def _person(
    user_id: str, first_name: str, last_name: str, emails: dict[str, str | None]
) -> TeamPerson:
    email = emails.get(user_id)
    return TeamPerson(
        user_id=user_id,
        display_name=_display_name(first_name, last_name, email),
        email=email,
        avatar_data_url=generate_user_avatar(user_id).data_url,
    )


def _summary(team: AdminTeamRecord, emails: dict[str, str | None]) -> AdminTeamSummary:
    return AdminTeamSummary(
        id=team.id,
        name=team.name,
        state=team.state,
        owner=_person(team.owner_user_id, team.owner_first_name, team.owner_last_name, emails),
        member_count=team.member_count,
        admin_count=team.admin_count,
        pending_request_count=team.pending_request_count,
        created_at=team.created_at,
        deleted_at=team.deleted_at,
    )


async def admin_team_summaries(
    gateway: Any, teams: list[AdminTeamRecord]
) -> list[AdminTeamSummary]:
    """Name each team's owner; an email the Gateway cannot read is left out."""
    emails = await _emails(gateway, (team.owner_user_id for team in teams))
    return [_summary(team, emails) for team in teams]


async def admin_team_detail(gateway: Any, detail: AdminTeamDetail) -> AdminTeamDetailResponse:
    """The roster in every membership state, open requests and link counts."""
    emails = await _emails(
        gateway,
        [
            detail.team.owner_user_id,
            *(member.user_id for member in detail.members),
            *(request.user_id for request in detail.join_requests),
        ],
    )
    return AdminTeamDetailResponse(
        team=_summary(detail.team, emails),
        members=[
            AdminTeamMember(
                **_person(member.user_id, member.first_name, member.last_name, emails).model_dump(),
                role=member.role,
                state=member.state,
                joined_at=member.joined_at,
                departed_at=member.departed_at,
            )
            for member in detail.members
        ],
        join_requests=[
            JoinRequestResponse(
                **_person(
                    request.user_id, request.first_name, request.last_name, emails
                ).model_dump(),
                id=request.id,
                state=request.state,
                created_at=request.created_at,
                expires_at=request.expires_at,
            )
            for request in detail.join_requests
        ],
        invitations=AdminTeamInvitationCounts(**detail.invitations),
    )


async def admin_audit_events(
    gateway: Any, events: list[AdminAuditRecord]
) -> list[AdminTeamAuditEvent]:
    """Name each event's actor and target the way the roster names them."""
    emails = await _emails(
        gateway,
        [
            *(event.actor_user_id for event in events),
            *(event.target_user_id for event in events if event.target_user_id),
        ],
    )
    return [
        AdminTeamAuditEvent(
            id=event.id,
            action=event.action,  # type: ignore[arg-type]
            actor=_person(
                event.actor_user_id, event.actor_first_name, event.actor_last_name, emails
            ),
            target_user=(
                _person(
                    event.target_user_id, event.target_first_name, event.target_last_name, emails
                )
                if event.target_user_id
                else None
            ),
            target_id=event.target_id,
            created_at=event.created_at,
        )
        for event in events
    ]
