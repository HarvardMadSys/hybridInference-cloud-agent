"""Model time to first token, read from the gateway's own request log.

The number the chat bar and the admin run page call TTFT is the gateway's
``ttft_ms``: the time from the gateway receiving one model request to the first
streamed chunk it relayed back. Nothing on the platform's side of that request
is in it -- not sandbox or workspace startup, not the Agent CLI starting, and
not the wait for a whole message block to finish, all of which the event
ledger's timestamps include. The event-derived interval keeps its own name,
``first_output_seconds``, in :mod:`cloud_agent.thread_stats`.

Requests are read exactly as the admin request pages read them: each started
execution's stored ledger reference names a grant, a fixed window and a
gateway installation, and :func:`llm_request_source_view` decides whether that
reference may be queried at all. The read is bounded -- a fixed number of
references, one page of rows each, a deadline over the whole read -- and the
summary says when it saw less than everything, so a sample is never shown as
the whole.

A request is a sample only when the gateway measured a first token for it and
the request succeeded: ``status_code`` below 400 (the filter the gateway's own
latency rollups use) and a non-null ``ttft_ms``. Zero is a measured value at
millisecond resolution; only null means the gateway did not record a timing.
Non-streamed requests and failed ones are not samples. With no sample the mean
is ``None``; it is never replaced by an event-derived value.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from cloud_agent.admin_observability import expected_gateway_job_id, llm_request_source_view
from cloud_agent.domain.execution import ATTEMPT_ABORTED
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloud_agent.grant_usage import GrantUsageRequest, GrantUsageRequestPage
    from cloud_agent.grants import GrantClient

logger = get_logger(__name__)

#: Request rows read per stored reference. One page, never followed: a
#: reference with more rows marks the summary partial instead.
MODEL_TTFT_ROWS_PER_SOURCE = 100
#: Gateway reads one summary keeps in flight. The client's own request-page
#: slots bound every caller together; this keeps one summary from taking them all.
MODEL_TTFT_READ_CONCURRENCY = 4
#: The whole read's deadline. A summary that cannot finish in time reports the
#: gateway unavailable rather than whatever happened to arrive first.
MODEL_TTFT_DEADLINE_S = 10.0

#: Why a summary saw less than every request, worst first.
REASON_GATEWAY_UNAVAILABLE = "gateway_unavailable"
REASON_DETAILS_UNSUPPORTED = "request_details_unsupported"
REASON_SOURCES_TRUNCATED = "sources_truncated"
REASON_PAGE_TRUNCATED = "request_page_truncated"

_UNSUPPORTED_ERRORS = frozenset({"usage_unsupported_schema", "request_details_unsupported"})


class ModelTtftSummary(BaseModel):
    """Mean gateway time to first token over the requests that measured one.

    ``mean_ttft_ms`` weighs every sampled request equally. ``complete`` is true
    only when every started execution in scope was attributed, read, and fit in
    one page; otherwise ``reason`` names the first gap, and the mean describes
    the ``samples`` it was computed from and nothing more.
    """

    source: Literal["gateway_request_log"] = "gateway_request_log"
    status: Literal["available", "not_recorded", "unavailable"]
    mean_ttft_ms: float | None = Field(None, ge=0)
    #: Requests whose TTFT entered the mean.
    samples: int = Field(0, ge=0)
    #: Requests the gateway returned, sampled or not.
    requests_read: int = Field(0, ge=0)
    #: Started executions whose request page was read.
    sources_read: int = Field(0, ge=0)
    #: Started executions in scope, as far as this read looked.
    sources_total: int = Field(0, ge=0)
    complete: bool
    reason: str | None = None


def measured_ttft_ms(request: GrantUsageRequest) -> int | None:
    """The request's first-token time when it is a model TTFT sample, else ``None``."""
    status = request.status_code
    ttft = request.ttft_ms
    if status is None or status >= 400 or ttft is None:
        return None
    return ttft


def summarize_request_pages(
    pages: Sequence[GrantUsageRequestPage],
    *,
    sources_total: int,
    gap: str | None,
) -> ModelTtftSummary:
    """Fold already validated pages into the summary, deduplicating request ids.

    ``gap`` is the first reason a started execution was not read, or the
    reference list was cut short; a page that has more rows adds its own.
    """
    by_id: dict[str, int | None] = {}
    for page in pages:
        for request in page.requests:
            by_id.setdefault(request.request_id, measured_ttft_ms(request))
        if page.next_cursor is not None and gap is None:
            gap = REASON_PAGE_TRUNCATED
    samples = [value for value in by_id.values() if value is not None]
    if samples:
        status: Literal["available", "not_recorded", "unavailable"] = "available"
    elif pages or sources_total == 0:
        status = "not_recorded"
    else:
        status = "unavailable"
    return ModelTtftSummary(
        status=status,
        mean_ttft_ms=sum(samples) / len(samples) if samples else None,
        samples=len(samples),
        requests_read=len(by_id),
        sources_read=len(pages),
        sources_total=sources_total,
        complete=gap is None,
        reason=gap,
    )


async def read_model_ttft(
    sources: Sequence[Mapping[str, Any]],
    *,
    more_sources: bool,
    grants: GrantClient,
    scope: str,
) -> ModelTtftSummary:
    """Read and summarize the requests behind stored ledger references.

    Args:
        sources: Attempt rows with their ledger reference, in the shape the
            admin request-source query returns (``ledger_id``, ``grant_id``,
            ``binding_id``, ``thread_id``, the window, ``attempt_status``...).
            The caller has already scoped them to what its reader may see.
        more_sources: Whether the caller's bounded read left references out.
        grants: The gateway client; its binding decides which references are
            this installation's to query.
        scope: The job or thread id, for logs only.
    """
    started = [row for row in sources if row.get("attempt_status") != ATTEMPT_ABORTED]
    gap = REASON_SOURCES_TRUNCATED if more_sources else None
    queryable: list[tuple[Mapping[str, Any], str]] = []
    for row in started:
        external_job_id = expected_gateway_job_id(thread_id=row.get("thread_id"))
        view = llm_request_source_view(
            dict(row),
            current_binding_id=grants.binding_id,
            expected_external_job_id=external_job_id,
        )
        if view.availability != "queryable" or external_job_id is None:
            gap = gap or view.reason
            continue
        queryable.append((row, external_job_id))
    gate = asyncio.Semaphore(MODEL_TTFT_READ_CONCURRENCY)
    try:
        async with asyncio.timeout(MODEL_TTFT_DEADLINE_S):
            read = await asyncio.gather(
                *(_read_page(row, job, grants, gate, scope) for row, job in queryable)
            )
    except TimeoutError:
        read = [(None, REASON_GATEWAY_UNAVAILABLE) for _ in queryable]
    pages = [page for page, _ in read if page is not None]
    gap = gap or next((reason for page, reason in read if page is None), None)
    return summarize_request_pages(pages, sources_total=len(started), gap=gap)


async def _read_page(
    row: Mapping[str, Any],
    external_job_id: str,
    grants: GrantClient,
    gate: asyncio.Semaphore,
    scope: str,
) -> tuple[GrantUsageRequestPage | None, str | None]:
    """One reference's first page, or the reason it could not be read."""
    started = time.monotonic()
    try:
        async with gate:
            page = await grants.usage_requests(
                row["grant_id"],
                since=row["window_since"],
                until=row["window_until"],
                limit=MODEL_TTFT_ROWS_PER_SOURCE,
                external_job_id=external_job_id,
            )
    except GatewayError as exc:
        # The machine reason and correlation ids only, as on the request pages:
        # the gateway's own text can quote request data.
        logger.warning(
            "agent_model_ttft_read_failed",
            extra={
                "event": "agent_model_ttft_read_failed",
                "scope": scope,
                "source_id": row.get("ledger_id"),
                "attempt_id": row.get("attempt_id"),
                "error_type": exc.error_type,
                "status_code": exc.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        if exc.error_type in _UNSUPPORTED_ERRORS:
            return None, REASON_DETAILS_UNSUPPORTED
        return None, REASON_GATEWAY_UNAVAILABLE
    return page, None
