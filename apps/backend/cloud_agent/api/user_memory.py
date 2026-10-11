"""Owner-only generated avatars, personal memory, and improvement suggestions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.user_avatar import generate_user_avatar
from cloud_agent.user_memory import UserMemoryView

if TYPE_CHECKING:
    from cloud_agent.ports.user_memory import UserMemoryRepository

router = APIRouter(prefix="/v1/agent/user-memory", tags=["user-memory"])


class _MemoryStorage(Protocol):
    """Only personal memory persistence is needed by these endpoints."""

    user_memory: UserMemoryRepository


class UpdateMemorySettings(BaseModel):
    """Enable or pause automatic memory creation and use."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def _view(values: dict[str, Any], user_id: str) -> UserMemoryView:
    view = UserMemoryView.model_validate(values)
    view.avatar = generate_user_avatar(user_id, focus_areas=view.portrait.focus_areas)
    return view


@router.get("", response_model=UserMemoryView)
async def read_user_memory(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> UserMemoryView:
    """Automatically create the owner's avatar and refresh their recent memory."""
    response.headers["Cache-Control"] = "no-store"
    values = await require_store(store).user_memory.refresh(user_id=user["user_id"])
    return _view(values, user["user_id"])


@router.patch("", response_model=UserMemoryView)
async def update_user_memory(
    body: UpdateMemorySettings,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> UserMemoryView:
    """Pause generation and prompt personalization, or resume future updates."""
    response.headers["Cache-Control"] = "no-store"
    values = await require_store(store).user_memory.set_enabled(
        user_id=user["user_id"], enabled=body.enabled
    )
    return _view(values, user["user_id"])


@router.delete("", response_model=UserMemoryView)
async def clear_user_memory(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> UserMemoryView:
    """Forget historical memory; only newer requests can build it again."""
    response.headers["Cache-Control"] = "no-store"
    values = await require_store(store).user_memory.clear(user_id=user["user_id"])
    return _view(values, user["user_id"])
