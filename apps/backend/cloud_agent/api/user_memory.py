"""Owner-only user preferences and separately controlled project memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_grant_client, get_storage, require_store
from cloud_agent.memory_consolidation import (
    MemoryConsolidationError,
    MemoryEdit,
    consolidate_memory,
    validate_memory_edits,
)
from cloud_agent.user_avatar import generate_user_avatar
from cloud_agent.user_memory import (
    MEMORY_CONTENT_LIMIT,
    MEMORY_LIMIT,
    MemoryRevisionConflict,
    ProjectMemoryView,
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

project_router = APIRouter(
    prefix="/v1/agent/project-memory", tags=["project-memory"], route_class=_MemoryValidationRoute
)


class _MemoryStorage(Protocol):
    """Both memory scopes use the owner-scoped memory repository."""

    user_memory: UserMemoryRepository


class UpdateMemorySettings(BaseModel):
    """Enable or pause automatic memory creation and use."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool | None = None
    revision: int | None = Field(default=None, ge=0)
    memories: list[MemoryEdit] | None = Field(default=None, max_length=MEMORY_LIMIT)

    @model_validator(mode="after")
    def valid_operation(self) -> UpdateMemorySettings:
        """Require either a setting change or a revision-checked edit."""
        if self.memories is not None:
            if self.revision is None or self.enabled is not None:
                raise ValueError("Memory edits require revision and cannot change enabled")
        elif self.enabled is None or self.revision is not None:
            raise ValueError("Supply enabled or revision and memories")
        return self


class MemoryEntryRevision(BaseModel):
    """Require the version the owner examined before changing an entry."""

    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


class UpdateMemoryEntry(MemoryEntryRevision):
    """Edit content while retaining the entry's original kind and project."""

    content: str = Field(min_length=1, max_length=MEMORY_CONTENT_LIMIT)


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
    """Create the owner's avatar and refresh their preferences across projects."""
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
    repository = require_store(store).user_memory
    if body.memories is None:
        assert body.enabled is not None
        values = await repository.set_enabled(user_id=user["user_id"], enabled=body.enabled)
    else:
        current = await repository.refresh(user_id=user["user_id"])
        if current["revision"] != body.revision:
            raise _conflict()
        try:
            memories = validate_memory_edits(body.memories, current["memories"])
            assert body.revision is not None
            values = await repository.replace_memories(
                user_id=user["user_id"], revision=body.revision, memories=memories
            )
        except MemoryRevisionConflict:
            raise _conflict() from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
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


class ConsolidateMemoryRequest(BaseModel):
    """Consolidate only the version the owner currently sees."""

    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)
    model: str | None = Field(default=None, min_length=1, max_length=200)


def _conflict() -> HTTPException:
    return HTTPException(409, "Memory changed. Refresh and try again.")


@router.post("/consolidate", response_model=UserMemoryView)
async def consolidate_user_memory(
    body: ConsolidateMemoryRequest,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
    gateway: Any = Depends(get_gateway_client),
    grants: Any = Depends(get_grant_client),
) -> UserMemoryView:
    """Merge and classify durable preferences using the owner's available model."""
    response.headers["Cache-Control"] = "no-store"
    repository = require_store(store).user_memory
    current = await repository.refresh(user_id=user["user_id"])
    if current["revision"] != body.revision:
        raise _conflict()
    if not current["memories"]:
        return _view(current, user["user_id"])
    try:
        memories = await consolidate_memory(
            current,
            user_id=user["user_id"],
            gateway=gateway,
            grants=grants,
            requested_model=body.model,
        )
        if not memories:
            values = await repository.refresh(user_id=user["user_id"])
            return _view(values, user["user_id"])
        values = await repository.replace_memories(
            user_id=user["user_id"], revision=body.revision, memories=memories
        )
    except MemoryRevisionConflict:
        raise _conflict() from None
    except MemoryConsolidationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except ValueError:
        raise HTTPException(
            422, "Consolidated memory could not be saved. Review its scope and limits."
        ) from None
    return _view(values, user["user_id"])


@project_router.get("/projects", response_model=list[str])
async def list_memory_projects(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> list[str]:
    """List only repositories with activity or memory owned by this user."""
    response.headers["Cache-Control"] = "no-store"
    return await require_store(store).user_memory.list_memory_projects(user_id=user["user_id"])


@project_router.get("", response_model=ProjectMemoryView)
async def read_project_memory(
    response: Response,
    repo: str = Query(..., min_length=1, max_length=140, pattern=r"\S"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> ProjectMemoryView:
    """Refresh memory from this owner's activity in the requested repository."""
    response.headers["Cache-Control"] = "no-store"
    values = await require_store(store).user_memory.refresh_project(
        user_id=user["user_id"], repo=repo
    )
    return ProjectMemoryView.model_validate(values)


@project_router.patch("", response_model=ProjectMemoryView)
async def update_project_memory(
    body: UpdateMemorySettings,
    response: Response,
    repo: str = Query(..., min_length=1, max_length=140, pattern=r"\S"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> ProjectMemoryView:
    """Change project settings or save revision-checked edits and scope changes."""
    response.headers["Cache-Control"] = "no-store"
    repository = require_store(store).user_memory
    if body.memories is None:
        assert body.enabled is not None
        values = await repository.set_project_enabled(
            user_id=user["user_id"], repo=repo, enabled=body.enabled
        )
    else:
        current = await repository.refresh_project(user_id=user["user_id"], repo=repo)
        if current["revision"] != body.revision:
            raise _conflict()
        try:
            memories = validate_memory_edits(body.memories, current["memories"])
            assert body.revision is not None
            values = await repository.replace_project_memories(
                user_id=user["user_id"], repo=repo, revision=body.revision, memories=memories
            )
        except MemoryRevisionConflict:
            raise _conflict() from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    return ProjectMemoryView.model_validate(values)


@project_router.delete("", response_model=ProjectMemoryView)
async def clear_project_memory(
    response: Response,
    repo: str = Query(..., min_length=1, max_length=140, pattern=r"\S"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> ProjectMemoryView:
    """Forget only this project's past activity, preserving its enabled setting."""
    response.headers["Cache-Control"] = "no-store"
    values = await require_store(store).user_memory.clear_project(
        user_id=user["user_id"], repo=repo
    )
    return ProjectMemoryView.model_validate(values)


@project_router.patch(
    "/entries/{memory_id}",
    response_model=ProjectMemoryView,
    responses={
        404: {"description": "The owner has no such visible memory entry."},
        409: {"description": "Memory changed since the expected revision."},
    },
)
async def edit_project_memory_entry(
    memory_id: str,
    body: UpdateMemoryEntry,
    response: Response,
    repo: str = Query(..., min_length=1, max_length=140, pattern=r"\S"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> ProjectMemoryView:
    """Save a durable correction to one of the owner's existing memories."""
    response.headers["Cache-Control"] = "no-store"
    try:
        content = normalize_memory_edit(body.content)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Memory must contain nonempty text without credentials."
        ) from exc
    try:
        values = await require_store(store).user_memory.update_project_entry(
            user_id=user["user_id"],
            repo=repo,
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
    return ProjectMemoryView.model_validate(values)


@project_router.delete(
    "/entries/{memory_id}",
    response_model=ProjectMemoryView,
    responses={
        404: {"description": "The owner has no such visible memory entry."},
        409: {"description": "Memory changed since the expected revision."},
    },
)
async def delete_project_memory_entry(
    memory_id: str,
    body: MemoryEntryRevision,
    response: Response,
    repo: str = Query(..., min_length=1, max_length=140, pattern=r"\S"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> ProjectMemoryView:
    """Forget an entry and suppress it during subsequent automatic refreshes."""
    response.headers["Cache-Control"] = "no-store"
    try:
        values = await require_store(store).user_memory.delete_project_entry(
            user_id=user["user_id"],
            repo=repo,
            memory_id=memory_id,
            expected_revision=body.expected_revision,
        )
    except UserMemoryConflict as exc:
        raise HTTPException(
            status_code=409, detail="Memory changed. Refresh and review it before removing."
        ) from exc
    except UserMemoryNotFound as exc:
        raise HTTPException(status_code=404, detail="Memory entry not found.") from exc
    return ProjectMemoryView.model_validate(values)


@project_router.post("/consolidate", response_model=ProjectMemoryView)
async def consolidate_project_memory(
    body: ConsolidateMemoryRequest,
    response: Response,
    repo: str = Query(..., min_length=1, max_length=140, pattern=r"\S"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
    gateway: Any = Depends(get_gateway_client),
    grants: Any = Depends(get_grant_client),
) -> ProjectMemoryView:
    """Merge and classify durable preferences using the owner's available model."""
    response.headers["Cache-Control"] = "no-store"
    repository = require_store(store).user_memory
    current = await repository.refresh_project(user_id=user["user_id"], repo=repo)
    if current["revision"] != body.revision:
        raise _conflict()
    if not current["memories"]:
        return ProjectMemoryView.model_validate(current)
    try:
        memories = await consolidate_memory(
            current,
            user_id=user["user_id"],
            gateway=gateway,
            grants=grants,
            requested_model=body.model,
        )
        if not memories:
            values = await repository.refresh_project(user_id=user["user_id"], repo=repo)
            return ProjectMemoryView.model_validate(values)
        values = await repository.replace_project_memories(
            user_id=user["user_id"], repo=repo, revision=body.revision, memories=memories
        )
    except MemoryRevisionConflict:
        raise _conflict() from None
    except MemoryConsolidationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except ValueError:
        raise HTTPException(
            422, "Consolidated memory could not be saved. Review its scope and limits."
        ) from None
    return ProjectMemoryView.model_validate(values)
