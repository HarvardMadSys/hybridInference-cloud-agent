"""Owner-only generated avatars, personal memory, and improvement suggestions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.user_avatar import generate_user_avatar
from cloud_agent.user_memory import (
    UserMemoryConflict,
    UserMemoryNotFound,
    UserMemoryView,
    normalize_memory_edit,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from cloud_agent.ports.user_memory import UserMemoryRepository


class _MemoryValidationRoute(APIRoute):
    """Keep rejected memory text out of request-validation error responses."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Replace validation errors that would otherwise echo submitted input."""
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "Check the required memory fields and values."},
                    headers={"Cache-Control": "no-store"},
                )

        return safe_handler


router = APIRouter(
    prefix="/v1/agent/user-memory", tags=["user-memory"], route_class=_MemoryValidationRoute
)


class _MemoryStorage(Protocol):
    """Only personal memory persistence is needed by these endpoints."""

    user_memory: UserMemoryRepository


class UpdateMemorySettings(BaseModel):
    """Enable or pause automatic memory creation and use."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class MemoryEntryRevision(BaseModel):
    """Require the version the owner examined before changing an entry."""

    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


class UpdateMemoryEntry(MemoryEntryRevision):
    """Edit content while retaining the entry's original kind and project."""

    content: str = Field(min_length=1, max_length=500)


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


@router.patch(
    "/entries/{memory_id}",
    response_model=UserMemoryView,
    responses={
        404: {"description": "The owner has no such visible memory entry."},
        409: {"description": "Memory changed since the expected revision."},
    },
)
async def edit_user_memory_entry(
    memory_id: str,
    body: UpdateMemoryEntry,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> UserMemoryView:
    """Save a durable correction to one of the owner's existing memories."""
    response.headers["Cache-Control"] = "no-store"
    try:
        content = normalize_memory_edit(body.content)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Memory must contain nonempty text without credentials."
        ) from exc
    try:
        values = await require_store(store).user_memory.update_entry(
            user_id=user["user_id"],
            memory_id=memory_id,
            content=content,
            expected_revision=body.expected_revision,
        )
    except UserMemoryConflict as exc:
        raise HTTPException(
            status_code=409, detail="Memory changed. Refresh and review it before saving."
        ) from exc
    except UserMemoryNotFound as exc:
        raise HTTPException(status_code=404, detail="Memory entry not found.") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Memory must be valid text; preferences must apply to future tasks.",
        ) from exc
    return _view(values, user["user_id"])


@router.delete(
    "/entries/{memory_id}",
    response_model=UserMemoryView,
    responses={
        404: {"description": "The owner has no such visible memory entry."},
        409: {"description": "Memory changed since the expected revision."},
    },
)
async def delete_user_memory_entry(
    memory_id: str,
    body: MemoryEntryRevision,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> UserMemoryView:
    """Forget an entry and suppress it during subsequent automatic refreshes."""
    response.headers["Cache-Control"] = "no-store"
    try:
        values = await require_store(store).user_memory.delete_entry(
            user_id=user["user_id"],
            memory_id=memory_id,
            expected_revision=body.expected_revision,
        )
    except UserMemoryConflict as exc:
        raise HTTPException(
            status_code=409, detail="Memory changed. Refresh and review it before removing."
        ) from exc
    except UserMemoryNotFound as exc:
        raise HTTPException(status_code=404, detail="Memory entry not found.") from exc
    return _view(values, user["user_id"])
