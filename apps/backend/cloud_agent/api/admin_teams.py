"""The admin Teams page: every team, who is in it, and what happened to it.

Read-only. A team's owner and admins manage it from Settings; the platform
administrator needs to see across all of them, deleted ones included, to
answer a support question or spot a team stuck without anyone able to run it.
Changing a team from here is a separate, audited operation this module does
not offer.

Members are named by their own profile and their Gateway account email, read
per request and never stored, the way the team's own pages name them. An email
the Gateway cannot read is left out rather than failing the page.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from cloud_agent.api.deps import (
    get_gateway_client,
    get_storage,
    require_store,
    verify_admin_access,
)
from cloud_agent.application.teams import (
    admin_audit_events,
    admin_team_detail,
    admin_team_summaries,
)
from cloud_agent.domain.teams import ADMIN_TEAM_PAGE_LIMIT, AUDIT_PAGE_LIMIT, TeamNotFound
from cloud_agent.ports import TeamAdminQueries  # noqa: TC001 — FastAPI resolves this at runtime.
from cloud_agent.schemas import (
    AdminTeamAuditListResponse,
    AdminTeamDetailResponse,
    AdminTeamListResponse,
)

#: The longest search a request may send: a team name or id, or a user id.
MAX_QUERY_CHARS = 256


class _AdminTeamsStorage(Protocol):
    """The one capability this module reaches the storage layer for."""

    team_admin: TeamAdminQueries


def get_team_admin_queries(
    store: _AdminTeamsStorage | None = Depends(get_storage),
) -> TeamAdminQueries:
    """Expose only the read-only team view to these routes."""
    return require_store(store).team_admin


def _no_store(response: Response) -> None:
    """Membership is current-state information; never serve it from a cache."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent/teams",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


def _not_found(exc: TeamNotFound) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"type": "not_found", "message": exc.message}}
    )


@router.get("", response_model=AdminTeamListResponse)
async def list_teams(
    q: str | None = Query(None, max_length=MAX_QUERY_CHARS),
    state: Literal["active", "deleted", "all"] = Query("active"),
    limit: int = Query(50, ge=1, le=ADMIN_TEAM_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    teams: TeamAdminQueries = Depends(get_team_admin_queries),
    gateway: Any = Depends(get_gateway_client),
) -> AdminTeamListResponse:
    """Teams newest first.

    ``q`` matches part of a team's name, a team id, or the user id of anyone
    who has been a member.
    """
    query = (q or "").strip() or None
    rows, more = await teams.list_teams_for_admin(
        query=query, state=state, limit=limit, offset=offset
    )
    return AdminTeamListResponse(
        teams=await admin_team_summaries(gateway, rows),
        next_offset=offset + len(rows) if more else None,
    )


@router.get("/{team_id}", response_model=AdminTeamDetailResponse)
async def get_team(
    team_id: str,
    teams: TeamAdminQueries = Depends(get_team_admin_queries),
    gateway: Any = Depends(get_gateway_client),
) -> AdminTeamDetailResponse:
    """One team in any state: members past and present, open requests, links."""
    try:
        detail = await teams.get_team_for_admin(team_id)
    except TeamNotFound as exc:
        raise _not_found(exc) from exc
    return await admin_team_detail(gateway, detail)


@router.get("/{team_id}/audit-events", response_model=AdminTeamAuditListResponse)
async def list_team_audit_events(
    team_id: str,
    before: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=AUDIT_PAGE_LIMIT),
    teams: TeamAdminQueries = Depends(get_team_admin_queries),
    gateway: Any = Depends(get_gateway_client),
) -> AdminTeamAuditListResponse:
    """A team's audit events, newest first, a page at a time."""
    try:
        events = await teams.list_team_audit_for_admin(team_id, before=before, limit=limit + 1)
    except TeamNotFound as exc:
        raise _not_found(exc) from exc
    page = events[:limit]
    return AdminTeamAuditListResponse(
        events=await admin_audit_events(gateway, page),
        next_before=page[-1].id if len(events) > limit else None,
    )
