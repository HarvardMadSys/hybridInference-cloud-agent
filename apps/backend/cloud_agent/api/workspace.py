"""The retained workspace: files, terminals and git state, through the broker.

Every handler resolves the owning broker for the job (`_terminal_owner_workspace`,
`_terminal_broker_http_error`) and fences retained mutations through the durable
thread-wide barrier (`_require_mutable_workspace_ready`). Two of the helpers are
general enough to be shared with the job and worker surfaces — `_workspace_id`,
`_workspace_not_ready` and `_workspace_broker_for_job` already live in
`_workspace_access` — but these four are reached only from here.

`_require_workspace_entitlement` is one of them: it re-checks, at use time, that
the owner may still reach the repository the workspace was cloned from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from cloud_agent import measurement_writer
from cloud_agent.api._operation_measurements import (
    FILE_READ_STAGE,
    measured_call,
    measured_response,
    profiling_answer_headers,
    record_measurement,
)
from cloud_agent.api._owner_auth import _owned_job, require_agent_owner
from cloud_agent.api._workspace_access import (
    _TERMINAL_ID_PATTERN,
    _workspace_broker_for_job,
    _workspace_id,
    _workspace_not_ready,
)
from cloud_agent.api.deps import get_agent_app_credentials, get_storage, require_store
from cloud_agent.api.threads import _SSE_HEADERS
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.logging_utils import get_logger
from cloud_agent.operation_measurement import (
    FILE_CANDIDATES,
    FILES_LIST,
    ROUTE_FILES,
    ROUTE_TERMINALS,
    TERMINAL_CREATE,
    TERMINAL_LIST,
    TERMINAL_OPEN,
    TERMINAL_STREAM,
    continuation,
    operation as measured_operation,
    root_scope,
)
from cloud_agent.schemas import (
    AgentGitWorkspaceResponse,
    AgentTerminalRequest,
    AgentTerminalResponse,
    AgentTerminalSessionCreateRequest,
    AgentTerminalSessionInputRequest,
    AgentTerminalSessionListResponse,
    AgentTerminalSessionResizeRequest,
    AgentTerminalSessionResponse,
    AgentWorkspaceResponse,
    AgentWorkspaceWriteRequest,
)
from cloud_agent.terminal_coordination import resume_settled_terminal
from cloud_agent.workspace_broker_client import WorkspaceBrokerError
from cloud_agent.workspace_errors import (
    _workspace_broker_http_error,
    _workspace_not_materialized,
)
from cloud_agent_shared.workspace_browser import WorkspacePathError, normalize_workspace_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.ports import (
        ConversationQueries,
        HostPoolRepository,
        OperationMeasurements,
        SandboxRepository,
        SourceControlRepository as SourceControlPort,
    )


router = APIRouter(prefix="/v1/agent")

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _WorkspaceStorage(Protocol):
    """The five capabilities the workspace surface reaches for."""

    conversation_queries: ConversationQueries
    host_pool: HostPoolRepository
    operation_measurements: OperationMeasurements
    sandbox: SandboxRepository
    source_control: SourceControlPort


def _require_store(store: _WorkspaceStorage | None) -> _WorkspaceStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


def _terminal_broker_http_error(exc: WorkspaceBrokerError) -> HTTPException:
    """Preserve terminal errors while presenting a missing workspace as a conflict."""
    if _workspace_not_materialized(exc):
        return HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This chat has no live terminal workspace.",
                }
            },
        )
    return _workspace_broker_http_error(exc)


async def _require_mutable_workspace_ready(
    *, job: dict[str, Any], store: SandboxRepository
) -> None:
    """Fence retained mutations through the durable thread-wide barrier."""
    if job.get("thread_sandbox") is None:
        raise _workspace_not_ready()
    thread_id = job.get("thread_id")
    if not thread_id or not await store.thread_workspace_ready(thread_id=str(thread_id)):
        raise _workspace_not_ready()


async def _require_workspace_entitlement(
    *,
    job: dict[str, Any],
    user: dict[str, Any],
    store: Any,
    app_credentials: Any | None,
) -> None:
    """Recheck that the owner may still access the workspace repository."""
    try:
        await require_entitled_repo(
            job["repo"],
            user["user_id"],
            store=store.source_control,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc


async def _terminal_owner_workspace(
    *,
    job_id: str,
    user: dict[str, Any],
    store: _WorkspaceStorage | None,
    app_credentials: Any | None,
    require_ready: bool = False,
    recheck_entitlement: bool = True,
) -> tuple[dict[str, Any], Any]:
    """Authorize one terminal operation and return its private broker.

    Creating or attaching to a terminal rechecks repository entitlement. Once
    attached, the opaque terminal id acts as a short-lived capability scoped
    to the already-authenticated job owner. Input and resize must not enumerate
    GitHub installations for every keystroke; they still recheck ownership and
    the broker-side workspace/session binding. Delete skips entitlement as well
    so a revoked owner can always clean up a live process.
    """
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    if require_ready:
        ready = job.get(
            "thread_sandbox"
        ) is not None and await job_store.sandbox.thread_workspace_ready(
            thread_id=str(job["thread_id"])
        )
        if not ready:
            raise _workspace_not_ready()
    if recheck_entitlement:
        await _require_workspace_entitlement(
            job=job,
            user=user,
            store=job_store,
            app_credentials=app_credentials,
        )
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This deployment has no live workspace broker.",
                }
            },
        )
    if job.get("terminal_resume_pending"):
        # The broker resolved a few lines up is this job's own host's, so the
        # resume goes to the machine holding the retained workspace rather than to
        # whatever the deployment's single-host variable names.
        if not await resume_settled_terminal(
            _workspace_id(job),
            job_id=job_id,
            broker=broker,
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": "The settled terminal workspace is still resuming.",
                    }
                },
            )
        await job_store.sandbox.mark_terminal_resume_complete(job_id=job_id)
        job["terminal_resume_pending"] = False
    return job, broker


@router.get("/jobs/{job_id}/files", response_model=AgentWorkspaceResponse)
async def get_agent_job_workspace_file(
    job_id: str,
    request: Request,
    path: str = Query("", max_length=4096),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> Response:
    """Browse the chat's retained workspace through its owning host broker."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    try:
        safe_path = normalize_workspace_path(path)
    except WorkspacePathError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_path", "message": str(exc)}},
        ) from exc

    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )

    candidates = frozenset(FILE_CANDIDATES if safe_path else (FILES_LIST,))
    scope = root_scope(FILES_LIST, job, service=ROUTE_FILES, candidates=candidates)
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This chat has no owning workspace broker.",
                }
            },
        )
    recorded = False
    with measured_operation(FILES_LIST, scope, continuing=continuation(request.headers)) as measure:
        try:
            if measure is not None:
                measure.mark(FILE_READ_STAGE)
                if len(candidates) > 1:
                    # Until the answer arrives a non-root path may be either a
                    # directory expansion or a file preview.
                    measure.name_request("files.read")
            frame = measure.begin_call(FILE_READ_STAGE) if measure is not None else None
            try:
                body = await measured_call(
                    measure,
                    FILE_READ_STAGE,
                    call=lambda profiling: broker.files(
                        _workspace_id(job), safe_path, profiling=profiling
                    ),
                    record=False,
                    call_id=frame,
                )
            except WorkspaceBrokerError as exc:
                if _workspace_not_materialized(exc):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": {
                                "type": "workspace_unavailable",
                                "message": "This chat has no retained workspace.",
                            }
                        },
                    ) from exc
                raise _workspace_broker_http_error(exc) from exc
            if measure is not None:
                kind = str(body.get("kind") or "")
                kind_operation = measure.result_operation(body, default=FILES_LIST)
                broker_operation = str(body.get("profiling_operation") or "").strip()
                result_operation = (
                    ""
                    if broker_operation and broker_operation != kind_operation
                    else kind_operation
                )
                if not measure.confirm(result_operation):
                    measure.discard()
                    recorded = True
                    return measured_response(AgentWorkspaceResponse, None, body)
                measure.record_answer(FILE_READ_STAGE, frame, body)
                measure.measurement.facts["kind"] = kind
                if kind == "directory":
                    measure.measurement.facts["entry_count"] = len(body.get("entries") or [])
                else:
                    measure.measurement.facts["content_truncated"] = bool(body.get("truncated"))
                    measure.measurement.facts["binary"] = bool(body.get("binary"))
                measure.outcome = "completed"
            response = measured_response(AgentWorkspaceResponse, measure, body)
            record_measurement(
                store=job_store,
                job=job,
                measure=measure,
            )
            recorded = True
            return response
        finally:
            if not recorded:
                if measure is not None and safe_path and measure.request_kind == "files.read":
                    # The failed read never produced the kind that decides
                    # list versus open. Filing it under the provisional first
                    # candidate corrupts that operation's failure rate.
                    measure.discard()
                    local_writer = measurement_writer.writer()
                    local_writer.counts.rejected += 1
                    local_writer.counts.note("ambiguous_file_kind")
                else:
                    record_measurement(
                        store=job_store,
                        job=job,
                        measure=measure,
                        failure="failed",
                    )


@router.put("/jobs/{job_id}/files", response_model=AgentWorkspaceResponse)
async def write_agent_job_workspace_file(
    job_id: str,
    body: AgentWorkspaceWriteRequest,
    path: str = Query(..., min_length=1, max_length=4096),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentWorkspaceResponse:
    """Save one text file directly into the retained chat workspace."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    try:
        safe_path = normalize_workspace_path(path)
    except WorkspacePathError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_path", "message": str(exc)}},
        ) from exc
    if not safe_path:
        raise HTTPException(status_code=400, detail="A file path is required.")
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    await _require_mutable_workspace_ready(job=job, store=job_store.sandbox)
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This deployment has no live workspace broker.",
                }
            },
        )
    try:
        return AgentWorkspaceResponse(
            **(await broker.write_file(_workspace_id(job), safe_path, body.content))
        )
    except WorkspaceBrokerError as exc:
        if _workspace_not_materialized(exc):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": "This retained workspace is not materialized.",
                    }
                },
            ) from exc
        raise _workspace_broker_http_error(exc) from exc


@router.post("/jobs/{job_id}/terminal", response_model=AgentTerminalResponse)
async def run_agent_job_terminal_command(
    job_id: str,
    body: AgentTerminalRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalResponse:
    """Run an owner-entered command inside the retained chat sandbox."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    await _require_mutable_workspace_ready(job=job, store=job_store.sandbox)
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(status_code=409, detail="This deployment has no live workspace broker.")
    try:
        result = await broker.terminal(
            _workspace_id(job),
            command=body.command,
            cwd=body.cwd,
            timeout_seconds=body.timeout_seconds,
        )
        return AgentTerminalResponse(**result)
    except WorkspaceBrokerError as exc:
        if _workspace_not_materialized(exc):
            raise HTTPException(
                status_code=409,
                detail="This retained terminal workspace is not materialized.",
            ) from exc
        raise _workspace_broker_http_error(exc) from exc


@router.post(
    "/jobs/{job_id}/terminals",
    response_model=AgentTerminalSessionResponse,
)
async def create_agent_job_terminal(
    job_id: str,
    request: Request,
    body: AgentTerminalSessionCreateRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> Response:
    """Open an interactive PTY in the agent's workspace."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
    )
    scope = root_scope(TERMINAL_OPEN, job, service=ROUTE_TERMINALS)
    with measured_operation(
        TERMINAL_OPEN, scope, continuing=continuation(request.headers)
    ) as measure:
        try:
            if measure is not None:
                measure.name_request(TERMINAL_CREATE)
                measure.mark(TERMINAL_CREATE)
            try:
                payload = await measured_call(
                    measure,
                    TERMINAL_CREATE,
                    call=lambda profiling: broker.create_terminal(
                        _workspace_id(job),
                        rows=body.rows,
                        cols=body.cols,
                        profiling=profiling,
                    ),
                )
            except WorkspaceBrokerError as exc:
                raise _terminal_broker_http_error(exc) from exc
            if measure is not None:
                measure.outcome = "completed"
            response = measured_response(AgentTerminalSessionResponse, measure, payload)
            record_measurement(
                store=_require_store(store),
                job=job,
                measure=measure,
            )
            return response
        except BaseException:
            record_measurement(
                store=_require_store(store),
                job=job,
                measure=measure,
                failure="failed",
            )
            raise


@router.get(
    "/jobs/{job_id}/terminals",
    response_model=AgentTerminalSessionListResponse,
)
async def list_agent_job_terminals(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> Response:
    """List interactive terminals, including exited sessions kept for replay."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
    )
    scope = root_scope(TERMINAL_OPEN, job, service=ROUTE_TERMINALS)
    with measured_operation(
        TERMINAL_OPEN, scope, continuing=continuation(request.headers)
    ) as measure:
        try:
            if measure is not None:
                measure.name_request(TERMINAL_LIST)
                measure.mark(TERMINAL_LIST)
            try:
                payload = await measured_call(
                    measure,
                    TERMINAL_LIST,
                    call=lambda profiling: broker.list_terminals(
                        _workspace_id(job), profiling=profiling
                    ),
                )
            except WorkspaceBrokerError as exc:
                raise _terminal_broker_http_error(exc) from exc
            if measure is not None:
                measure.outcome = "completed"
                measure.measurement.facts["terminal_count"] = len(payload.get("terminals") or [])
            response = measured_response(AgentTerminalSessionListResponse, measure, payload)
            record_measurement(
                store=_require_store(store),
                job=job,
                measure=measure,
            )
            return response
        except BaseException:
            record_measurement(
                store=_require_store(store),
                job=job,
                measure=measure,
                failure="failed",
            )
            raise


@router.get("/jobs/{job_id}/terminals/{terminal_id}/stream")
async def stream_agent_job_terminal(
    job_id: str,
    request: Request,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    after: int = Query(0, ge=0, le=2**63 - 1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> StreamingResponse:
    """Relay a resumable private PTY stream without exposing its broker token."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
    )
    scope = root_scope(TERMINAL_OPEN, job, service=ROUTE_TERMINALS)
    with measured_operation(
        TERMINAL_OPEN, scope, continuing=continuation(request.headers)
    ) as measure:
        try:
            if measure is not None:
                measure.name_request(TERMINAL_STREAM)
                measure.mark(TERMINAL_STREAM)
            stream_call = measure.begin_call(TERMINAL_STREAM) if measure is not None else None
            try:
                upstream = await measured_call(
                    measure,
                    TERMINAL_STREAM,
                    call=lambda profiling: broker.stream_terminal(
                        _workspace_id(job), terminal_id, after=after, profiling=profiling
                    ),
                    record=False,
                    call_id=stream_call,
                )
            except WorkspaceBrokerError as exc:
                raise _terminal_broker_http_error(exc) from exc
            if measure is not None:
                measure.record_answer_from_headers(
                    TERMINAL_STREAM, stream_call, upstream.profiling_answer
                )
                measure.outcome = "completed"

            async def relay() -> AsyncIterator[Any]:
                try:
                    async for chunk in upstream:
                        if await request.is_disconnected():
                            break
                        yield chunk
                finally:
                    await upstream.aclose()

            response = StreamingResponse(
                relay(),
                media_type="text/event-stream",
                headers=(
                    _SSE_HEADERS
                    if measure is None
                    else {**_SSE_HEADERS, **profiling_answer_headers(measure)}
                ),
            )
            # This operation is the attach handshake, not the lifetime of the
            # terminal byte stream.  Persist now so an hour-long SSE does not
            # turn one short attach into a late, rejected delivery.
            record_measurement(
                store=_require_store(store),
                job=job,
                measure=measure,
            )
            return response
        except BaseException:
            record_measurement(
                store=_require_store(store),
                job=job,
                measure=measure,
                failure="failed",
            )
            raise


@router.post(
    "/jobs/{job_id}/terminals/{terminal_id}/input",
    response_model=AgentTerminalSessionResponse,
)
async def write_agent_job_terminal_input(
    job_id: str,
    body: AgentTerminalSessionInputRequest,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Write bounded base64 input to an active interactive terminal."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(await broker.terminal_input(_workspace_id(job), terminal_id, data=body.data))
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.post(
    "/jobs/{job_id}/terminals/{terminal_id}/resize",
    response_model=AgentTerminalSessionResponse,
)
async def resize_agent_job_terminal(
    job_id: str,
    body: AgentTerminalSessionResizeRequest,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Resize an active interactive terminal."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(
                await broker.resize_terminal(
                    _workspace_id(job), terminal_id, rows=body.rows, cols=body.cols
                )
            )
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.delete(
    "/jobs/{job_id}/terminals/{terminal_id}",
    response_model=AgentTerminalSessionResponse,
)
async def delete_agent_job_terminal(
    job_id: str,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Idempotently kill an interactive terminal, even while a new job runs."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(await broker.delete_terminal(_workspace_id(job), terminal_id))
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.get("/jobs/{job_id}/git", response_model=AgentGitWorkspaceResponse)
async def get_agent_job_git_workspace(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _WorkspaceStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentGitWorkspaceResponse:
    """Read live retained-workspace status, diff and recent commits."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        return AgentGitWorkspaceResponse(available=False)
    try:
        return AgentGitWorkspaceResponse(
            **(await broker.git(_workspace_id(job), base_sha=job.get("base_sha")))
        )
    except WorkspaceBrokerError as exc:
        if _workspace_not_materialized(exc):
            return AgentGitWorkspaceResponse(available=False)
        raise _workspace_broker_http_error(exc) from exc
