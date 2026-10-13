"""An owner's change stream: one SSE connection per tab, for their whole account.

The chat list, the sidebar and an open conversation used to ask for state,
title and pull request link every few seconds. A tab now holds this stream, and
it sends each changed job or thread the moment Postgres says it changed
(migration ``0031``): its id, its ``revision`` and the values the tab shows.
A job's usage is sent when it changes (migration ``0032``), as its usage read
returns it. The message queue, tool approvals and publication progress are
named as hints, and the tab reads that one thing again: the first two are
lists, and the last is the newest snapshot of a workspace, which has no
version a tab could order against the turn row it reads it with. Lists still
read their own pages; what they no longer do is ask on a clock.

The stream replays nothing. It says ``hello`` once it collects the owner's
changes, so a tab that reads its lists after that misses none, and
``resync`` when changes may have been missed while the process was not
listening. ``live`` in both says whether pushes are flowing at all; while it is
false a tab keeps reading on its own clock.

It ends after ``OWNER_STREAM_MAX_S``, as every owner stream does: admission is
checked when it opens, and the tab's reconnect, whose ``hello`` has it read its
lists again, is admitted afresh.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from cloud_agent.api._job_views import _iso
from cloud_agent.api._owner_auth import OWNER_STREAM_MAX_S, require_agent_owner
from cloud_agent.api._stream_slots import STREAM_SLOTS
from cloud_agent.api.deps import get_owner_change_signal, get_storage, require_store
from cloud_agent.api.usage import job_usage_response
from cloud_agent.workers.owner_change_signal import (
    HINT_KINDS,
    OwnerChangeSignal,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.ports import ConversationQueries, LedgerRepository


router = APIRouter(prefix="/v1/agent")

#: A comment frame after this long without a frame keeps proxies from reaping
#: an idle connection.
_KEEPALIVE_S = 15.0

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class _ChangesStorage(Protocol):
    """The capabilities the change stream reads through: rows, and usage."""

    conversation_queries: ConversationQueries
    ledger: LedgerRepository


def _frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "revision": row["revision"],
        "state": row["state"],
        "conversation_state": row.get("conversation_state"),
        "published_pr_url": row.get("published_pr_url"),
    }


def _thread(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "revision": row["revision"],
        "title": row["title"],
        "title_source": row["title_source"],
        "pinned_at": _iso(row.get("pinned_at")),
        "archived_at": _iso(row.get("archived_at")),
        "deleted_at": _iso(row.get("deleted_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


async def _usage(store: _ChangesStorage, user_id: str, job_id: str) -> dict[str, Any]:
    """A job's usage as its usage read returns it; only its id if it is not theirs."""
    usage = await job_usage_response(store, job_id=job_id, user_id=user_id)
    if usage is None:
        return {"id": job_id}
    return {"id": job_id, "usage": usage.model_dump(mode="json")}


@router.get("/changes")
async def stream_owner_changes(
    request: Request,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ChangesStorage | None = Depends(get_storage),
    signal: OwnerChangeSignal | None = Depends(get_owner_change_signal),
) -> StreamingResponse:
    """Stream the caller's changed jobs and conversations as SSE.

    Events: ``hello`` and ``resync`` carry ``{"live": bool}``; ``job`` and
    ``thread`` carry the row's current values and its ``revision``, or
    ``{"id": ..., "deleted": true}`` once it is gone; ``usage`` carries the
    job's ``{"id": ..., "usage": ...}`` as ``GET /v1/agent/jobs/{id}/usage``
    returns it; ``mcp`` (a job) and ``queue``, ``publication`` (a
    conversation) carry only ``{"id": ...}``: read that again. 404 where this
    process pushes nothing, which a tab treats as "keep reading on a clock".
    The stream ends after at most ``OWNER_STREAM_MAX_S`` (15 minutes); the tab
    reconnects, is admitted again, and its ``hello`` has it read its lists.
    429 when the account already holds as many streams as this process allows
    one account (:mod:`cloud_agent.api._stream_slots`).
    """
    job_store = require_store(store)
    if signal is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": "This listener does not push changes; read on a clock instead.",
                }
            },
        )
    user_id = str(user["user_id"])
    slot = STREAM_SLOTS.acquire(user_id)

    async def frames() -> AsyncIterator[str]:
        clock = asyncio.get_running_loop().time
        # Admission was checked once, for this connection, so the connection
        # does not outlive the cap.
        closes_at = clock() + OWNER_STREAM_MAX_S
        with signal.subscribe(user_id) as subscription:
            yield _frame("hello", {"live": signal.listening})
            while True:
                remaining = closes_at - clock()
                if remaining <= 0 or await request.is_disconnected():
                    return
                woken = await subscription.wait(min(_KEEPALIVE_S, remaining))
                changes = subscription.take()
                if changes.resync:
                    yield _frame("resync", {"live": signal.listening})
                if changes.jobs or changes.threads:
                    rows = await job_store.conversation_queries.owner_changes(
                        user_id=user_id,
                        job_ids=sorted(changes.jobs),
                        thread_ids=sorted(changes.threads),
                    )
                    found_threads = {row["id"]: row for row in rows["threads"]}
                    for thread_id in sorted(changes.threads):
                        row = found_threads.get(thread_id)
                        yield _frame(
                            "thread",
                            _thread(row) if row else {"id": thread_id, "deleted": True},
                        )
                    found_jobs = {row["id"]: row for row in rows["jobs"]}
                    for job_id in sorted(changes.jobs):
                        row = found_jobs.get(job_id)
                        yield _frame("job", _job(row) if row else {"id": job_id, "deleted": True})
                for kind in HINT_KINDS:
                    for row_id in sorted(changes.hints.get(kind, ())):
                        if kind == "usage":
                            yield _frame(kind, await _usage(job_store, user_id, row_id))
                        else:
                            yield _frame(kind, {"id": row_id})
                if not (woken or changes.resync) and clock() < closes_at:
                    yield ": keepalive\n\n"

    return StreamingResponse(
        slot.hold(frames()), media_type="text/event-stream", headers=_SSE_HEADERS
    )
