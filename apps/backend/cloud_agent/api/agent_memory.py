"""Owner-only durable memories, one row each, for the user or one repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.agent_memory import (
    MEMORY_CONTENT_LIMIT,
    AgentMemoryConflict,
    AgentMemoryFull,
    AgentMemoryNotFound,
    AgentMemoryScopeView,
    AgentMemoryView,
    normalize_memory,
)
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from cloud_agent.ports.agent_memory import AgentMemoryRepository


_INVALID_MESSAGE = "Memory must be one lasting rule of at most 240 characters, without credentials."


class _MemoryValidationRoute(APIRoute):
    """Keep rejected memory text out of request-validation error responses."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Replace validation errors that would otherwise echo submitted input."""
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = exc.errors()
                # Text over the limit is refused here, before it is normalized,
                # and carries the same typed reason as text normalizing refuses.
                detail: Any = (
                    {"error": {"type": "memory_invalid", "message": _INVALID_MESSAGE}}
                    if errors
                    and all(tuple(error["loc"]) == ("body", "content") for error in errors)
                    else "Check the required memory fields and values."
                )
                return JSONResponse(
                    status_code=422,
                    content={"detail": detail},
                    headers={"Cache-Control": "no-store"},
                )

        return safe_handler


router = APIRouter(
    prefix="/v1/agent/memories", tags=["agent-memory"], route_class=_MemoryValidationRoute
)

_REPO = Query(None, min_length=1, max_length=140, pattern=r"\S")


class _MemoryStorage(Protocol):
    agent_memory: AgentMemoryRepository


class _Repo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repo: str | None = Field(min_length=1, max_length=140, pattern=r"\S")


class AddMemory(_Repo):
    """A memory the owner writes; ``repo`` is null for every project."""

    content: str = Field(min_length=1, max_length=MEMORY_CONTENT_LIMIT)


class UpdateMemory(AddMemory):
    """New text and scope for a memory, at the version the owner examined."""

    expected_version: int = Field(ge=0)


class MemoryVersion(BaseModel):
    """The version the owner examined before removing or restoring a memory."""

    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=0)


def _refused(status: int, kind: str, message: str) -> HTTPException:
    return HTTPException(status, {"error": {"type": kind, "message": message}})


def _content(content: str) -> str:
    try:
        return normalize_memory(content)
    except ValueError as exc:
        raise _failure(exc) from None


def _failure(exc: ValueError) -> HTTPException:
    """Answer with a typed reason and fixed text; never echo memory or submitted content."""
    if isinstance(exc, AgentMemoryNotFound):
        return _refused(404, "memory_not_found", "Memory not found.")
    if isinstance(exc, AgentMemoryConflict):
        return _refused(409, "memory_changed", "Memory changed. Refresh and try again.")
    if isinstance(exc, AgentMemoryFull):
        return _refused(
            409,
            "memory_scope_full",
            "This scope is full of memories you wrote. Remove one before adding another.",
        )
    return _refused(422, "memory_invalid", _INVALID_MESSAGE)


@router.get("", response_model=AgentMemoryScopeView)
async def list_memories(
    response: Response,
    repo: str | None = _REPO,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> AgentMemoryScopeView:
    """Read the owner's memories for every project, or for one repository."""
    response.headers["Cache-Control"] = "no-store"
    values = await require_store(store).agent_memory.list_memories(user["user_id"], repo)
    return AgentMemoryScopeView.model_validate(values)


class MemorySetting(_Repo):
    """Turn one scope's memory on or off; ``repo`` is null for every project."""

    enabled: bool


class MemoryProjects(BaseModel):
    """Repositories that can have project memory."""

    projects: list[str]


@router.get("/projects", response_model=MemoryProjects)
async def list_memory_projects(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> MemoryProjects:
    """List repositories the owner worked in or keeps memories for."""
    response.headers["Cache-Control"] = "no-store"
    projects = await require_store(store).agent_memory.memory_projects(user["user_id"])
    return MemoryProjects(projects=projects)


@router.put("/settings", response_model=AgentMemoryScopeView)
async def set_memory_enabled(
    body: MemorySetting,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> AgentMemoryScopeView:
    """Turn a scope off to stop showing its memories to tasks and refuse agent writes."""
    response.headers["Cache-Control"] = "no-store"
    values = await require_store(store).agent_memory.set_memory_enabled(
        user["user_id"], body.repo, body.enabled
    )
    return AgentMemoryScopeView.model_validate(values)


@router.post("", response_model=AgentMemoryView, status_code=201)
async def add_memory(
    body: AddMemory,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> AgentMemoryView:
    """Save a memory the owner wrote; it applies from the next task."""
    response.headers["Cache-Control"] = "no-store"
    try:
        values = await require_store(store).agent_memory.add_memory(
            user["user_id"], body.repo, _content(body.content)
        )
    except ValueError as exc:
        raise _failure(exc) from None
    return AgentMemoryView.model_validate(values)


@router.patch("/{memory_id}", response_model=AgentMemoryView)
async def update_memory(
    memory_id: str,
    body: UpdateMemory,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> AgentMemoryView:
    """Rewrite or move a memory; the owner's edit makes it manual."""
    response.headers["Cache-Control"] = "no-store"
    try:
        values = await require_store(store).agent_memory.update_memory(
            user["user_id"],
            memory_id,
            content=_content(body.content),
            repo=body.repo,
            expected_version=body.expected_version,
        )
    except ValueError as exc:
        raise _failure(exc) from None
    return AgentMemoryView.model_validate(values)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    body: MemoryVersion,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> Response:
    """Delete one memory permanently."""
    try:
        await require_store(store).agent_memory.delete_memory(
            user["user_id"], memory_id, expected_version=body.expected_version
        )
    except ValueError as exc:
        raise _failure(exc) from None
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/{memory_id}/restore", response_model=AgentMemoryView)
async def restore_memory(
    memory_id: str,
    body: MemoryVersion,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> AgentMemoryView:
    """Make an archived memory active again."""
    response.headers["Cache-Control"] = "no-store"
    try:
        values = await require_store(store).agent_memory.restore_memory(
            user["user_id"], memory_id, expected_version=body.expected_version
        )
    except ValueError as exc:
        raise _failure(exc) from None
    return AgentMemoryView.model_validate(values)


@router.delete("", status_code=204)
async def clear_memories(
    repo: str | None = _REPO,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _MemoryStorage | None = Depends(get_storage),
) -> Response:
    """Delete every memory for every project, or for one repository."""
    await require_store(store).agent_memory.clear_memories(user["user_id"], repo)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
