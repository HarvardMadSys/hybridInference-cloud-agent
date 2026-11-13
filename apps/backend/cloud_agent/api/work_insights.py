"""The home page's work insights: suggestions and the avatar's habitat."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Depends, Response

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.insights.insights_cache import current_insights
from cloud_agent.insights.work_insights import ACTIVITY_LIMIT, WorkInsights

router = APIRouter(prefix="/v1/agent/insights", tags=["insights"])


class RecentRequests(Protocol):
    """The owner's own latest requests, which the insights are derived from."""

    async def recent_requests(self, user_id: str, limit: int = 50) -> list[str]:
        """The owner's latest eligible requests, newest first."""
        ...


class _InsightsStorage(Protocol):
    work_insights: RecentRequests


@router.get("", response_model=WorkInsights)
async def read_insights(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _InsightsStorage | None = Depends(get_storage),
) -> WorkInsights:
    """Derive the owner's suggestions and avatar from their latest requests."""
    response.headers["Cache-Control"] = "no-store"
    messages = await require_store(store).work_insights.recent_requests(
        user["user_id"], ACTIVITY_LIMIT
    )
    return current_insights(user["user_id"], messages)
