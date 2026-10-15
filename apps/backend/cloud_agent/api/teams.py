"""Teams: membership, single-use invitations, join requests and the owner's audit.

Every route requires the signed-in owner session and the Gateway's current
``agent.use``. Who may act on a team is then decided by the store, inside the
transaction that acts: a non-member, or a team that no longer exists, is 404 so
team ids cannot be probed; a member asking for an owner-only change is 403; a
state that forbids the change is 409 with a stable error type.

Nothing here is reachable from a sandbox: these routes live on the main
listener only, and a worker capability token is refused by the owner
dependency before any of them runs.

Responses are never cached. An invitation token appears in exactly one
response — the one that created it — and malformed requests are refused
without echoing their input, so a mistyped link never lands in an error body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_storage, require_store
from cloud_agent.application import teams as flows
from cloud_agent.domain.teams import (
    AUDIT_PAGE_LIMIT,
    InvitationUnavailable,
    TeamConflict,
    TeamError,
    TeamForbidden,
    TeamNotFound,
)
from cloud_agent.owner_access import AgentAccessUnavailable
from cloud_agent.schemas import (
    ClaimInvitationRequest,
    CreateInvitationRequest,
    CreateTeamRequest,
    DecideJoinRequest,
    InvitationCreatedResponse,
    InvitationListResponse,
    JoinRequestListResponse,
    JoinRequestResponse,
    OwnJoinRequestListResponse,
    OwnJoinRequestResponse,
    RenameTeamRequest,
    SetTeamMemberRoleRequest,
    TeamAuditEventListResponse,
    TeamAuditEventResponse,
    TeamListResponse,
    TeamMemberListResponse,
    TeamResponse,
    TransferOwnershipRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from cloud_agent.domain.teams import TeamRecord
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.ports.teams import TeamRepository

_NO_STORE = {"Cache-Control": "no-store"}
_STATUS: dict[type[TeamError], int] = {
    TeamNotFound: 404,
    TeamForbidden: 403,
    TeamConflict: 409,
    InvitationUnavailable: 410,
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"type": code, "message": message}},
        headers=_NO_STORE,
    )


class _TeamRoute(APIRoute):
    """Map team refusals to stable errors and keep every answer out of caches."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the generated handler; validation errors never echo the request."""
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                response = await handler(request)
            except TeamError as exc:
                return _error(_STATUS.get(type(exc), 409), exc.code, exc.message)
            except AgentAccessUnavailable:
                return _error(
                    503,
                    "unavailable",
                    "Account access cannot be confirmed right now. Try again shortly.",
                )
            except RequestValidationError:
                return _error(422, "invalid_request", "Check the request and try again.")
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), **_NO_STORE}
                raise
            response.headers.update(_NO_STORE)
            return response

        return guarded


router = APIRouter(prefix="/v1/agent", tags=["teams"], route_class=_TeamRoute)


class _TeamStorage(Protocol):
    """The team surface reaches only the team repository."""

    teams: TeamRepository


def _teams(store: _TeamStorage | None) -> TeamRepository:
    return require_store(store).teams


def _team(team: TeamRecord) -> TeamResponse:
    return TeamResponse(
        id=team.id,
        name=team.name,
        owner_user_id=team.owner_user_id,
        role=team.role,
        revision=team.revision,
        member_count=team.member_count,
        created_at=team.created_at,
    )


# ── Teams ─────────────────────────────────────────────────────────────


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team(
    body: CreateTeamRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> TeamResponse:
    """Create a team owned by the caller; the same ``request_key`` returns the same team."""
    team = await _teams(store).create_team(
        user["user_id"], name=body.name, request_key=body.request_key
    )
    return _team(team)


@router.get("/teams", response_model=TeamListResponse)
async def list_teams(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> TeamListResponse:
    """List only the teams the caller is an active member of."""
    teams = await _teams(store).list_teams(user["user_id"])
    return TeamListResponse(teams=[_team(team) for team in teams])


@router.get("/teams/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> TeamResponse:
    """Read one team the caller belongs to."""
    return _team(await _teams(store).get_team(user["user_id"], team_id))


@router.patch("/teams/{team_id}", response_model=TeamResponse)
async def rename_team(
    team_id: str,
    body: RenameTeamRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> TeamResponse:
    """Rename as an owner or admin at the revision the caller read."""
    team = await _teams(store).rename_team(
        user["user_id"], team_id, name=body.name, expected_revision=body.expected_revision
    )
    return _team(team)


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    expected_revision: int = Query(ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> Response:
    """Delete the team and close its outstanding invitations; owner only."""
    await _teams(store).delete_team(user["user_id"], team_id, expected_revision=expected_revision)
    return Response(status_code=204)


# ── Members ───────────────────────────────────────────────────────────


@router.get("/teams/{team_id}/members", response_model=TeamMemberListResponse)
async def list_members(
    team_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> TeamMemberListResponse:
    """List active members, owner first, with display names and account emails."""
    members = await _teams(store).list_members(user["user_id"], team_id)
    return TeamMemberListResponse(
        members=await flows.member_responses(gateway, user["user_id"], members)
    )


@router.post("/teams/{team_id}/leave", status_code=204)
async def leave_team(
    team_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> Response:
    """Leave the team; the owner must transfer ownership or delete it first."""
    await _teams(store).leave_team(user["user_id"], team_id)
    return Response(status_code=204)


@router.delete("/teams/{team_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    team_id: str,
    member_user_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> Response:
    """Remove a non-owner; admins may only remove ordinary members."""
    await _teams(store).remove_member(user["user_id"], team_id, member_user_id)
    return Response(status_code=204)


@router.patch("/teams/{team_id}/members/{member_user_id}/role", response_model=TeamResponse)
async def set_member_role(
    team_id: str,
    member_user_id: str,
    body: SetTeamMemberRoleRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> TeamResponse:
    """Grant or revoke admin; only the team owner may change roles."""
    team = await _teams(store).set_member_role(
        user["user_id"],
        team_id,
        member_user_id,
        role=body.role,
        expected_revision=body.expected_revision,
    )
    return _team(team)


@router.post("/teams/{team_id}/transfer-ownership", response_model=TeamResponse)
async def transfer_ownership(
    team_id: str,
    body: TransferOwnershipRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> TeamResponse:
    """Make an admitted active member the owner; the caller stays as a member."""
    team = await flows.transfer_ownership(
        _teams(store),
        gateway,
        user["user_id"],
        team_id,
        user_id=body.user_id,
        expected_revision=body.expected_revision,
    )
    return _team(team)


# ── Invitations ───────────────────────────────────────────────────────


@router.post(
    "/teams/{team_id}/invitations", response_model=InvitationCreatedResponse, status_code=201
)
async def create_invitation(
    team_id: str,
    body: CreateInvitationRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> InvitationCreatedResponse:
    """Create a single-use, 24-hour link; its token is returned only here, once."""
    return await flows.create_invitation(
        _teams(store), user["user_id"], team_id, request_key=body.request_key
    )


@router.get("/teams/{team_id}/invitations", response_model=InvitationListResponse)
async def list_invitations(
    team_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> InvitationListResponse:
    """List actionable invitations and recent history; owner or admin."""
    invitations = await _teams(store).list_invitations(user["user_id"], team_id)
    return InvitationListResponse(
        invitations=[flows.invitation_response(invitation) for invitation in invitations]
    )


@router.delete("/teams/{team_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    team_id: str,
    invitation_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> Response:
    """Revoke an invitation and any undecided request made with it."""
    await _teams(store).revoke_invitation(user["user_id"], team_id, invitation_id)
    return Response(status_code=204)


@router.post("/team-invitations/claim", response_model=OwnJoinRequestResponse)
async def claim_invitation(
    body: ClaimInvitationRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> OwnJoinRequestResponse:
    """Ask to join with a link; the owner or an admin still has to approve.

    Holding the link grants nothing by itself. Claiming makes it this
    account's: another account presenting it afterwards is refused.
    """
    request = await _teams(store).claim_invitation(
        user["user_id"], token_hash=flows.hash_invitation_token(body.token)
    )
    (response,) = await flows.own_request_responses(gateway, [request])
    return response


# ── Join requests ─────────────────────────────────────────────────────


@router.get("/team-join-requests", response_model=OwnJoinRequestListResponse)
async def list_own_join_requests(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> OwnJoinRequestListResponse:
    """The caller's own recent requests and where each stands."""
    requests = await _teams(store).list_own_join_requests(user["user_id"])
    return OwnJoinRequestListResponse(requests=await flows.own_request_responses(gateway, requests))


@router.get("/teams/{team_id}/join-requests", response_model=JoinRequestListResponse)
async def list_join_requests(
    team_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> JoinRequestListResponse:
    """List pending requests for an owner or admin to decide."""
    requests = await _teams(store).list_join_requests(user["user_id"], team_id)
    return JoinRequestListResponse(requests=await flows.applicant_responses(gateway, requests))


@router.post(
    "/teams/{team_id}/join-requests/{request_id}/decision", response_model=JoinRequestResponse
)
async def decide_join_request(
    team_id: str,
    request_id: str,
    body: DecideJoinRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> JoinRequestResponse:
    """Approve or decline; repeating the same decision returns its result."""
    return await flows.decide_join_request(
        _teams(store), gateway, user["user_id"], team_id, request_id, approve=body.approve
    )


# ── Audit ─────────────────────────────────────────────────────────────


@router.get("/teams/{team_id}/audit-events", response_model=TeamAuditEventListResponse)
async def list_audit_events(
    team_id: str,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=AUDIT_PAGE_LIMIT),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TeamStorage | None = Depends(get_storage),
) -> TeamAuditEventListResponse:
    """The team's administrative history, newest first; owner only."""
    events = await _teams(store).list_audit_events(
        user["user_id"], team_id, before=before, limit=limit + 1
    )
    page = events[:limit]
    return TeamAuditEventListResponse(
        events=[
            TeamAuditEventResponse(
                id=event.id,
                actor_user_id=event.actor_user_id,
                action=event.action,  # type: ignore[arg-type]
                target_user_id=event.target_user_id,
                target_id=event.target_id,
                created_at=event.created_at,
            )
            for event in page
        ],
        next_before=page[-1].id if len(events) > limit else None,
    )
