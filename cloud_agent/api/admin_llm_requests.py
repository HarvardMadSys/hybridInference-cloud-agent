"""Admin-only per-request LLM detail, read on demand from the gateway ledger.

The rows live in the gateway's ``api_logs``. This service stores only the
attribution reference that says which grant and window a turn's requests
belong to, so these two routes answer two separate questions and never merge
them: which stored references exist, and what one of them contains.

Nothing here writes. Opening the panel must not renew a grant, queue a
snapshot refresh, mint anything, or change what accounting reports; the
aggregate snapshot the run page already shows keeps its own refresh path.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from cloud_agent import identity
from cloud_agent.admin_observability import (
    MAX_ADMIN_RESPONSE_BYTES,
    MAX_LLM_REQUEST_PAGE,
    MAX_REQUEST_CURSOR_BYTES,
    SOURCE_REASON_BINDING_CHANGED,
    SOURCE_REASON_INVALID_WINDOW,
    AdminLlmRequest,
    AdminLlmRequestPage,
    AdminLlmRequestSource,
    AdminLlmRequestSourceList,
    AdminLlmRequestWindow,
    LlmRequestCursor,
    LlmRequestSourceCursor,
    RequestCursorError,
    RequestCursorTooLong,
    cursor_scope,
    decode_cursor,
    decode_llm_request_cursor,
    encode_cursor,
    encode_llm_request_cursor,
    expected_gateway_job_id,
    llm_request_source_view,
    request_source_fingerprint,
    response_body_bytes,
)
from cloud_agent.api.admin_runs import _no_store, _run, get_admin_run_queries
from cloud_agent.api.deps import get_grant_client, verify_admin_access
from cloud_agent.api.errors import AgentConfigurationError
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage.admin_queries import (  # noqa: TC001 — FastAPI resolves this at runtime.
    AdminRunQueries,
)

if TYPE_CHECKING:
    from cloud_agent.grant_usage import GrantUsageRequestPage
    from cloud_agent.grants import GrantClient

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/agent/runs", dependencies=[Depends(_no_store)])

#: Query parameters each route accepts. Anything else is refused rather than
#: ignored: a silently dropped ``grant_id`` or ``since`` would let a caller
#: believe it had chosen the query when the stored reference did.
_SOURCE_LIST_QUERY = frozenset({"attempt_id", "cursor", "limit"})
_REQUEST_PAGE_QUERY = frozenset({"source_id", "cursor", "limit"})


def _refuse(status_code: int, error_type: str, message: str) -> HTTPException:
    """Build a refusal whose machine reason is stable and whose text is ours."""
    return HTTPException(
        status_code=status_code, detail={"error": {"type": error_type, "message": message}}
    )


def _reject_unknown_query(request: Request, allowed: frozenset[str]) -> None:
    """Refuse a selector this endpoint does not accept, before any work."""
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise _refuse(
            422,
            "unexpected_query_parameter",
            f"Unsupported query parameter: {unknown[0]}.",
        )


def _source_refusal(source: AdminLlmRequestSource) -> HTTPException:
    """Map an unqueryable stored reference onto a stable refusal."""
    if source.reason == SOURCE_REASON_BINDING_CHANGED:
        return _refuse(
            409,
            SOURCE_REASON_BINDING_CHANGED,
            "This execution was attributed by a different gateway installation.",
        )
    if source.reason == SOURCE_REASON_INVALID_WINDOW:
        return _refuse(
            422,
            "request_window_unavailable",
            "This execution has no usable attribution window yet.",
        )
    return _refuse(
        422,
        "request_source_unavailable",
        "This execution has no stored gateway attribution.",
    )


def _gateway_refusal(exc: GatewayError) -> HTTPException:
    """Map a gateway failure onto a stable refusal without quoting it.

    The gateway's message can quote request data, so only its machine reason is
    translated. A gateway that answers ``invalid_cursor`` is saying this
    service's stored continuation no longer names a query it will resume:
    restarting from page one is the only honest recovery.
    """
    if exc.error_type == "invalid_cursor":
        return _refuse(
            409,
            "request_cursor_expired",
            "That page expired at the gateway. Load the first page again.",
        )
    mapped = {
        "usage_unsupported_schema": (
            503,
            "request_details_unsupported",
            "This gateway does not return per-request detail; the aggregate usage above is unaffected.",
        ),
        "request_details_unsupported": (
            503,
            "request_details_unsupported",
            "This gateway does not return per-request detail; the aggregate usage above is unaffected.",
        ),
        "usage_response_too_large": (
            502,
            "request_page_too_large",
            "The gateway returned more request detail than one page may carry. Retry with a smaller page.",
        ),
        "usage_invalid_response": (
            502,
            "request_page_invalid",
            "The gateway returned request detail this service cannot verify; the page was refused whole.",
        ),
        "usage_response_mismatch": (
            502,
            "request_page_invalid",
            "The gateway returned request detail this service cannot verify; the page was refused whole.",
        ),
        # The window and page size are validated before the call, so a gateway
        # that rejects them is answering a request this service did not build.
        "usage_query_invalid": (
            502,
            "request_page_invalid",
            "The gateway rejected a request-page query this service built; the page was refused whole.",
        ),
    }
    status_code, error_type, message = mapped.get(
        exc.error_type or "",
        (503, "request_details_unavailable", "Per-request detail is unavailable right now."),
    )
    return _refuse(status_code, error_type, message)


@router.get("/{job_id}/llm-request-sources", response_model=AdminLlmRequestSourceList)
async def list_admin_llm_request_sources(
    request: Request,
    job_id: str = Path(max_length=256),
    attempt_id: int | None = Query(None, ge=1, le=2**63 - 1),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(50, ge=1, le=MAX_LLM_REQUEST_PAGE),
    _admin: str = Depends(verify_admin_access),
    queries: AdminRunQueries = Depends(get_admin_run_queries),
    grants: GrantClient = Depends(get_grant_client),
) -> AdminLlmRequestSourceList:
    """List the stored gateway attribution references this run's executions hold.

    An execution with no reference keeps its own row, marked unavailable: an
    execution that was never attributed is a different answer from one that
    made no requests. The list is current metadata, not a snapshot shared with
    any gateway read that follows it.
    """
    _reject_unknown_query(request, _SOURCE_LIST_QUERY)
    scope = cursor_scope([job_id, attempt_id])
    try:
        parsed = decode_cursor(cursor, LlmRequestSourceCursor, scope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await _run(queries, job_id)
    rows, more = await queries.llm_request_sources(
        job_id, attempt_id=attempt_id, cursor=parsed, limit=limit
    )
    if attempt_id is not None and parsed is None and not rows:
        # A filter that names nothing is a mismatch, not an empty page. A
        # continuation is never empty: a cursor is only issued when another
        # row exists beyond the page that produced it.
        raise _refuse(404, "run_execution_not_found", "Run execution not found.")
    sources = [
        llm_request_source_view(
            row,
            current_binding_id=grants.binding_id,
            expected_external_job_id=expected_gateway_job_id(thread_id=row["thread_id"]),
        )
        for row in rows
    ]
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            LlmRequestSourceCursor(
                scope=scope,
                attempt_no=last["attempt_no"],
                attempt_id=last["attempt_id"],
                ledger_key=last["ledger_key"],
            )
        )
    return AdminLlmRequestSourceList(
        job_id=job_id,
        observed_at=datetime.now(UTC),
        sources=sources,
        next_cursor=next_cursor,
        reason=None if rows else "not_started",
    )


@router.get("/{job_id}/llm-requests", response_model=AdminLlmRequestPage)
async def list_admin_llm_requests(
    request: Request,
    job_id: str = Path(max_length=256),
    source_id: int = Query(..., ge=1, le=2**63 - 1),
    cursor: str | None = Query(None, max_length=MAX_REQUEST_CURSOR_BYTES),
    limit: int = Query(50, ge=1, le=MAX_LLM_REQUEST_PAGE),
    admin: str = Depends(verify_admin_access),
    queries: AdminRunQueries = Depends(get_admin_run_queries),
    grants: GrantClient = Depends(get_grant_client),
) -> AdminLlmRequestPage:
    """Read one page of a stored reference's request rows from the gateway.

    Every page re-reads the reference and re-checks the gateway installation:
    a signature proves the continuation was minted here, never that the
    attribution is still the one it was minted against.
    """
    _reject_unknown_query(request, _REQUEST_PAGE_QUERY)
    row = await queries.llm_request_source(job_id, source_id)
    if row is None:
        raise _refuse(404, "request_source_not_found", "Run request source not found.")
    expected_external_job_id = expected_gateway_job_id(thread_id=row["thread_id"])
    source = llm_request_source_view(
        row,
        current_binding_id=grants.binding_id,
        expected_external_job_id=expected_external_job_id,
    )
    if source.availability != "queryable" or expected_external_job_id is None:
        raise _source_refusal(source)
    assert source.window_since is not None and source.window_until is not None
    fingerprint = request_source_fingerprint(row)
    since, until = source.window_since, source.window_until
    gateway_cursor: str | None = None
    after: tuple[datetime, str] | None = None
    if cursor is not None:
        try:
            continuation = decode_llm_request_cursor(
                cursor,
                admin=admin,
                job_id=job_id,
                source_id=str(source_id),
                fingerprint=fingerprint,
                limit=limit,
            )
        except identity.IdentityNotConfigured:
            raise AgentConfigurationError() from None
        except RequestCursorError as exc:
            if exc.reason == "expired":
                raise _refuse(
                    409,
                    "request_cursor_expired",
                    "That page expired. Load the first page again.",
                ) from None
            if exc.reason == "changed":
                raise _refuse(
                    409,
                    "request_window_changed",
                    "This execution's attribution changed; load the first page again.",
                ) from None
            raise HTTPException(
                status_code=422, detail="Invalid pagination cursor for this query."
            ) from None
        if until < continuation.until:
            # A later execution began and clamped this window. Continuing would
            # silently answer a narrower interval than the pages before it.
            raise _refuse(
                409,
                "request_window_changed",
                "This execution's attribution window was shortened; load the first page again.",
            )
        since, until = continuation.since, continuation.until
        gateway_cursor = continuation.gateway_cursor
        after = (continuation.request_started_at, continuation.request_id)
    started = time.monotonic()
    try:
        page = await grants.usage_requests(
            row["grant_id"],
            since=since,
            until=until,
            limit=limit,
            external_job_id=expected_external_job_id,
            cursor=gateway_cursor,
            after=after,
        )
    except GatewayError as exc:
        # Only the machine reason, the correlation ids and the time it took are
        # recorded: the gateway's own text can quote request data, and the
        # cursor is a query descriptor the logs have no use for.
        logger.warning(
            "agent_admin_llm_requests_failed",
            extra={
                "event": "agent_admin_llm_requests_failed",
                "job_id": job_id,
                "source_id": row["ledger_id"],
                "attempt_id": row["attempt_id"],
                "error_type": exc.error_type,
                "status_code": exc.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        raise _gateway_refusal(exc) from None
    try:
        next_cursor = _encode_next_cursor(
            page,
            admin=admin,
            job_id=job_id,
            source_id=str(source_id),
            fingerprint=fingerprint,
            limit=limit,
            since=since,
            until=until,
        )
    except RequestCursorTooLong:
        # The continuation is part of the page. Returning the rows without one
        # would strand the reader mid-window, and returning a cursor that fails
        # on the next request is worse, so the page is refused whole.
        raise _refuse(
            502,
            "request_page_too_large",
            "This page's identifiers are too large to continue from; the page was refused whole.",
        ) from None
    response = AdminLlmRequestPage(
        job_id=job_id,
        source_id=str(source_id),
        attempt_id=row["attempt_id"],
        attempt_no=row["attempt_no"],
        window=AdminLlmRequestWindow(since=since, until=until),
        fetched_at=datetime.now(UTC),
        gateway_as_of=page.as_of,
        requests=[AdminLlmRequest.model_validate(item.model_dump()) for item in page.requests],
        next_cursor=next_cursor,
    )
    if response_body_bytes(response) > MAX_ADMIN_RESPONSE_BYTES:
        # Measured on the finished body, continuation included: the ceiling is
        # over what the browser receives, not over part of it.
        raise _refuse(
            502,
            "request_page_too_large",
            "The gateway returned more request detail than one page may carry; the page was refused whole.",
        )
    return response


def _encode_next_cursor(
    page: GrantUsageRequestPage,
    *,
    admin: str,
    job_id: str,
    source_id: str,
    fingerprint: str,
    limit: int,
    since: datetime,
    until: datetime,
) -> str | None:
    """Mint this service's continuation for the page the gateway just returned.

    The last row is stored here as well as inside the gateway's opaque cursor,
    so a continuation is checked locally instead of by decoding a format this
    service does not own.
    """
    if page.next_cursor is None or not page.requests:
        return None
    last = page.requests[-1]
    try:
        return encode_llm_request_cursor(
            LlmRequestCursor(
                admin=admin,
                job_id=job_id,
                source_id=source_id,
                fingerprint=fingerprint,
                since=since,
                until=until,
                limit=limit,
                request_started_at=last.request_started_at,
                request_id=last.request_id,
                gateway_cursor=page.next_cursor,
            )
        )
    except identity.IdentityNotConfigured:
        raise AgentConfigurationError() from None
