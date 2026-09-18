"""The read-mostly catalogue behind the sidebar: sessions, conversations, projects.

Listing, archiving and deleting whole conversations, and the project rollup the
UI groups them by. `_raise_session_mutation_error` travels with the two write
endpoints that share it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query

from cloud_agent.api._job_views import _iso, _job_response
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.entitlement import REPO_PATTERN
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import (
    AgentConversation,
    AgentConversationListResponse,
    AgentProject,
    AgentProjectListResponse,
    AgentSessionArchiveRequest,
    AgentSessionArchiveResponse,
    AgentSessionDeleteResponse,
    AgentSessionListResponse,
    AgentSessionResponse,
    AgentSessionSelection,
    AgentSessionState,
    AgentThreadArchiveResponse,
)

if TYPE_CHECKING:
    from cloud_agent.ports import ConversationCommands, ConversationQueries


router = APIRouter(prefix="/v1/agent")

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _CatalogStorage(Protocol):
    """The two capabilities the catalogue reaches for."""

    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries


def _require_store(store: _CatalogStorage | None) -> _CatalogStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


@router.get("/sessions", response_model=AgentSessionListResponse)
async def list_agent_sessions(
    limit: int = Query(50, ge=1, le=200),
    archived: bool = Query(False),
    include_summary: bool = Query(True),
    cursor: str | None = Query(None, max_length=1024),
    search: str | None = Query(None, max_length=200, pattern=r"^[^\x00]*$"),
    repo: str | None = Query(None, pattern=REPO_PATTERN, max_length=140),
    state: AgentSessionState | None = Query(None),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _CatalogStorage | None = Depends(get_storage),
) -> AgentSessionListResponse:
    """Search complete conversations by stable title, project and display state."""
    job_store = _require_store(store)
    try:
        result = await job_store.conversation_queries.list_sessions(
            user_id=user["user_id"],
            archived=archived,
            limit=limit,
            cursor=cursor,
            search=(search.strip() or None) if search is not None else None,
            repo=repo,
            state=state,
            include_summary=include_summary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AgentSessionListResponse(
        sessions=[
            AgentSessionResponse(
                thread_id=session["thread_id"],
                title=session["title"],
                archived_at=_iso(session["archived_at"]),
                job=_job_response(session["job"]),
                can_delete=session["can_delete"],
                delete_block_reason=session.get("delete_block_reason"),
            )
            for session in result["sessions"]
        ],
        total=result["total"],
        next_cursor=result["next_cursor"],
        projects=result["projects"],
    )


def _raise_session_mutation_error(result: dict[str, Any]) -> None:
    """Translate atomic selection failures without exposing foreign ownership."""
    error = result.get("error")
    if error is None:
        return
    messages = {
        "not_found": "One or more selected sessions are no longer available.",
        "not_archived": "Archive every selected session before deleting it.",
        "session_active": "A selected session is still active. Wait for its work to finish.",
        "fork_in_progress": "A selected session is being forked. Wait for the fork to finish.",
        "publication_pending": "A selected session has unfinished publication. Wait for it to finish.",
    }
    raise HTTPException(
        status_code=404 if error == "not_found" else 409,
        detail={"error": {"type": error, "message": messages[error]}},
    )


@router.post("/sessions/archive", response_model=AgentSessionArchiveResponse)
async def archive_agent_sessions(
    body: AgentSessionArchiveRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _CatalogStorage | None = Depends(get_storage),
) -> AgentSessionArchiveResponse:
    """Archive or unarchive an explicit owner-selected snapshot atomically."""
    result = await _require_store(store).conversation_commands.set_sessions_archived(
        thread_ids=body.thread_ids, user_id=user["user_id"], archived=body.archived
    )
    _raise_session_mutation_error(result)
    return AgentSessionArchiveResponse(
        sessions=[
            AgentThreadArchiveResponse(
                thread_id=session["thread_id"],
                archived=body.archived,
                archived_at=_iso(session["archived_at"]),
            )
            for session in result["sessions"]
        ]
    )


@router.post("/sessions/delete", response_model=AgentSessionDeleteResponse)
async def delete_agent_sessions(
    body: AgentSessionSelection,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _CatalogStorage | None = Depends(get_storage),
) -> AgentSessionDeleteResponse:
    """Permanently remove archived sessions from owner history, retaining audit data."""
    result = await _require_store(store).conversation_commands.delete_sessions(
        thread_ids=body.thread_ids, user_id=user["user_id"]
    )
    _raise_session_mutation_error(result)
    return AgentSessionDeleteResponse(thread_ids=result["thread_ids"])


@router.get("/conversations", response_model=AgentConversationListResponse)
async def list_agent_conversations(
    q: str = Query("", max_length=200),
    repo: str | None = Query(None, pattern=REPO_PATTERN, max_length=140),
    include_archived: bool = Query(False),
    pinned: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, min_length=1, max_length=2048),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _CatalogStorage | None = Depends(get_storage),
) -> AgentConversationListResponse:
    """Search owned conversation titles and repositories across all history."""
    job_store = _require_store(store)
    try:
        page = await job_store.conversation_queries.list_conversations(
            user_id=user["user_id"],
            q=q.strip(),
            repo=repo,
            include_archived=include_archived,
            pinned=pinned,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AgentConversationListResponse(
        conversations=[
            AgentConversation(
                job=_job_response(item["job"]),
                title=item["title"],
                archived=item["archived"],
                last_activity_at=_iso(item["last_activity_at"]),
            )
            for item in page["conversations"]
        ],
        next_cursor=page["next_cursor"],
    )


@router.get("/projects", response_model=AgentProjectListResponse)
async def list_agent_projects(
    archived: bool = Query(False),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _CatalogStorage | None = Depends(get_storage),
) -> AgentProjectListResponse:
    """Summarize the caller's projects for the sidebar's task tree."""
    job_store = _require_store(store)
    projects = await job_store.conversation_queries.list_projects(
        user_id=user["user_id"], archived=archived
    )
    return AgentProjectListResponse(
        projects=[
            AgentProject(
                repo=project["repo"],
                task_count=project["task_count"],
                active_count=project["active_count"],
                last_activity_at=_iso(project["last_activity_at"]),
                pinned_count=project.get("pinned_count", 0),
                pinned_at=_iso(project.get("pinned_at")),
            )
            for project in projects
        ]
    )
