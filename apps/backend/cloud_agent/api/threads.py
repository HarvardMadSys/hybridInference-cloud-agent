"""Reading a chat's transcript: thread, stats, event pages, and the SSE stream.

The event log is append-only and the stream resumable — a client reconnects
with ``Last-Event-ID`` and continues from the global cursor — so the SSE
constants travel with the writer that uses them. ``_sse_frame`` validates the
stored event name before it reaches an ``event:`` field: rows written before
the schema constraint existed, or by any future non-HTTP writer, must not be
able to inject frames into an owner's stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from cloud_agent.api._job_views import (
    _EVENT_PAGE_SIZE,
    _SAFE_EVENT_TYPE,
    _event_response,
    _iso,
    _job_response,
    _pending_response,
)
from cloud_agent.api._owner_auth import _owned_job, require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import (
    AgentJobEventsResponse,
    AgentThreadEvent,
    AgentThreadEventsResponse,
    AgentThreadMessageResponse,
    AgentThreadResponse,
    AgentThreadStatsResponse,
)
from cloud_agent.thread_stats import summarize_thread

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.ports import ConversationQueries, ExecutionQueries


router = APIRouter(prefix="/v1/agent")

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _ThreadsStorage(Protocol):
    """The two capabilities the reading surface reaches for."""

    conversation_queries: ConversationQueries
    execution_queries: ExecutionQueries


def _require_store(store: _ThreadsStorage | None) -> _ThreadsStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


# Same anti-buffering headers the completions SSE path uses: `no-transform`
# stops intermediary CDNs from buffering to compress, `X-Accel-Buffering: no`
# disables nginx buffering.
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


_POLL_INTERVAL_S = 1.0


_KEEPALIVE_EVERY_N_POLLS = 15


def _sse_frame(event: dict[str, Any]) -> str:
    """Render one stored event as an SSE frame with a safe event name."""
    event_type = event["event_type"]
    # fullmatch, not match: `$` also matches before a trailing newline, so
    # `match()` would accept "message\n" — exactly the value this guard
    # exists to reject, since the newline splits the SSE frame.
    if not _SAFE_EVENT_TYPE.fullmatch(event_type or ""):
        event_type = "malformed"
    data = json.dumps(_event_response(event).model_dump(), separators=(",", ":"))
    return f"id: {event['id']}\nevent: {event_type}\ndata: {data}\n\n"


@router.get("/jobs/{job_id}/thread", response_model=AgentThreadResponse)
async def get_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ThreadsStorage | None = Depends(get_storage),
) -> AgentThreadResponse:
    """Return the durable conversation containing one of the caller's jobs."""
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    thread = await job_store.conversation_queries.get_thread_for_job(
        job_id=job_id, user_id=user["user_id"]
    )
    if thread is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No thread for {job_id}"}},
        )
    return AgentThreadResponse(
        thread_id=thread["id"],
        repo=thread["repo"],
        title=thread["title"],
        messages=[
            AgentThreadMessageResponse(
                id=message["id"],
                role=message["role"],
                content=message["content"],
                job_id=message["job_id"],
                created_at=_iso(message["created_at"]),
                mid_turn=bool(message.get("mid_turn")),
            )
            for message in thread["messages"]
        ],
        jobs=[_job_response(job) for job in thread["jobs"]],
        created_at=_iso(thread["created_at"]),
        updated_at=_iso(thread["updated_at"]),
        archived_at=_iso(thread.get("archived_at")),
        pending_messages=[
            _pending_response(message) for message in thread.get("pending_messages") or []
        ],
        queue_paused_reason=thread.get("queue_paused_reason"),
    )


@router.get("/jobs/{job_id}/thread/stats", response_model=AgentThreadStatsResponse)
async def get_agent_thread_stats(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ThreadsStorage | None = Depends(get_storage),
) -> AgentThreadStatsResponse:
    """Summarize the thread containing a job: turns, timings, tokens.

    Every number is derived from rows the platform already writes, so a
    thread that predates this endpoint answers it in full. Measures the
    thread's runtime does not report come back ``null`` rather than zero.
    """
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    inputs = await job_store.execution_queries.thread_stats_inputs(
        job_id=job_id, user_id=user["user_id"]
    )
    if inputs is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No thread for {job_id}"}},
        )
    stats = summarize_thread(
        job_ids=inputs["job_ids"],
        attempts=inputs["attempts"],
        events=inputs["events"],
    )
    return AgentThreadStatsResponse(thread_id=inputs["thread_id"], **asdict(stats))


@router.get("/jobs/{job_id}/thread/events", response_model=AgentThreadEventsResponse)
async def list_agent_thread_events(
    job_id: str,
    after: int = Query(0, ge=0, description="Return events with a global id greater than this."),
    limit: int = Query(_EVENT_PAGE_SIZE, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ThreadsStorage | None = Depends(get_storage),
) -> AgentThreadEventsResponse:
    """Page every turn's event log, with payloads and turn labels intact."""
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    events = await job_store.execution_queries.list_thread_events_after(
        job_id=job_id, user_id=user["user_id"], after_id=after, limit=limit
    )
    if events is None:
        raise HTTPException(status_code=404, detail="job not found")
    return AgentThreadEventsResponse(
        events=[
            AgentThreadEvent(
                id=event["id"],
                job_id=event["job_id"],
                turn_no=event.get("turn_no") or 1,
                attempt_id=event["attempt_id"],
                seq=event["seq"],
                event_type=event["event_type"],
                payload=event["payload"],
                created_at=_iso(event["created_at"]),
            )
            for event in events
        ],
        next_cursor=events[-1]["id"] if events else after,
    )


@router.get("/jobs/{job_id}/events", response_model=AgentJobEventsResponse)
async def list_agent_job_events(
    job_id: str,
    after: int = Query(0, ge=0, description="Return events with a global id greater than this."),
    limit: int = Query(_EVENT_PAGE_SIZE, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ThreadsStorage | None = Depends(get_storage),
) -> AgentJobEventsResponse:
    """Page through a job's append-only event log (non-streaming)."""
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    events = await job_store.execution_queries.list_events_after(
        job_id=job_id, after_id=after, limit=limit
    )
    return AgentJobEventsResponse(
        events=[_event_response(event) for event in events],
        next_cursor=events[-1]["id"] if events else after,
    )


@router.get("/jobs/{job_id}/stream")
async def stream_agent_job_events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    after: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ThreadsStorage | None = Depends(get_storage),
) -> StreamingResponse:
    """Stream a job's events as SSE, resumable via ``Last-Event-ID``.

    Each SSE frame carries the event's global id, so a client that reconnects
    with ``Last-Event-ID`` resumes exactly where it left off — which is what
    makes the finite stream cap (applied by the timeout middleware to
    ``text/event-stream``) safe for jobs that outlive one connection.
    """
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)

    cursor = after
    if last_event_id:
        with contextlib.suppress(ValueError):
            cursor = int(last_event_id)

    async def event_source() -> AsyncIterator[Any]:
        nonlocal cursor
        idle_polls = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                events = await job_store.execution_queries.list_events_after(
                    job_id=job_id, after_id=cursor, limit=_EVENT_PAGE_SIZE
                )
                if events:
                    idle_polls = 0
                    for event in events:
                        cursor = event["id"]
                        yield _sse_frame(event)
                else:
                    idle_polls += 1
                    if idle_polls % _KEEPALIVE_EVERY_N_POLLS == 0:
                        # Comment frame: keeps proxies from reaping an idle
                        # connection without polluting the event sequence.
                        yield ": keepalive\n\n"

                job = await job_store.conversation_queries.get_job(job_id)
                if job is None:
                    return
                if job["state"] in TERMINAL_STATES:
                    # Drain to exhaustion before announcing the end. A single
                    # extra page is not enough: a client attaching to a
                    # finished job with more than two pages of backlog would
                    # see job_finished, close, and lose the rest forever.
                    while True:
                        tail = await job_store.execution_queries.list_events_after(
                            job_id=job_id, after_id=cursor, limit=_EVENT_PAGE_SIZE
                        )
                        if not tail:
                            break
                        for event in tail:
                            cursor = event["id"]
                            yield _sse_frame(event)
                    final = json.dumps(
                        {
                            "state": job["state"],
                            "published_pr_url": job["published_pr_url"],
                            "last_event_id": cursor,
                        },
                        separators=(",", ":"),
                    )
                    yield f"event: job_finished\ndata: {final}\n\n"
                    return

                await asyncio.sleep(_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            # Client disconnect or the stream cap firing: nothing to clean up,
            # the event log is durable and the client can resume by cursor.
            raise

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
