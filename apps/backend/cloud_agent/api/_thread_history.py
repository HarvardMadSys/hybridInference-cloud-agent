"""Shape bounded transcript pages without losing a cursor at the byte boundary."""

from __future__ import annotations

from typing import Any

from cloud_agent.api._job_views import _iso, _job_response, _pending_response
from cloud_agent.domain.thread_history import THREAD_PAGE_BYTES, entry_key, history_cursor
from cloud_agent.schemas import AgentJobResponse, AgentThreadMessageResponse, AgentThreadResponse


def message_response(message: dict[str, Any], offset: int = 0) -> AgentThreadMessageResponse:
    """Expose previews and detail chunks explicitly; offsets count Unicode characters."""
    length = message.get("content_length", len(message["content"]))
    end = offset + len(message["content"])
    return AgentThreadMessageResponse(
        id=message["id"],
        role=message["role"],
        content=message["content"],
        job_id=message["job_id"],
        created_at=_iso(message["created_at"]),
        mid_turn=bool(message.get("mid_turn")),
        turn_no=message.get("turn_no"),
        content_length=length,
        content_truncated=end < length,
        next_offset=end if end < length else None,
    )


def _preview_job(job: dict[str, Any]) -> AgentJobResponse:
    """Exceptional wide metadata must not prevent a page making progress."""
    result = _job_response(job)
    if len(result.model_dump_json().encode()) <= 48 * 1024:
        return result
    return result.model_copy(
        update={
            "thread_preview": True,
            "metadata": None,
            "skills": [],
            "attachments": [],
            "plugins": [],
            "mcp_bindings": [],
            "goal": None,
            "task_prompt": result.task_prompt[:4096],
            "detail": (result.detail or "")[:1024],
            "goal_report_detail": (result.goal_report_detail or "")[:1024],
        }
    )


def thread_response(thread: dict[str, Any]) -> AgentThreadResponse:
    """Keep response bytes bounded as well as the repository's row/body projections."""
    response = AgentThreadResponse(
        thread_id=thread["id"],
        repo=thread["repo"],
        title=thread["title"],
        messages=[],
        jobs=[],
        created_at=_iso(thread["created_at"]),
        updated_at=_iso(thread["updated_at"]),
        archived_at=_iso(thread.get("archived_at")),
        pending_messages=[_pending_response(row) for row in thread.get("pending_messages") or []],
        pending_messages_complete=thread.get("pending_messages_complete", True),
        queue_paused_reason=thread.get("queue_paused_reason"),
        current_job_id=thread.get("current_job_id"),
    )
    if len(response.model_dump_json().encode()) > THREAD_PAGE_BYTES // 4:
        response.pending_messages = []
        response.pending_messages_complete = False
    # Small legacy/fake stores keep their shape while the additive contract rolls out.
    if "_entries" not in thread:
        response.jobs = [_job_response(job) for job in thread["jobs"]]
        response.messages = [message_response(message) for message in thread["messages"]]
        return response
    return _fill_page(response, thread)


def _fill_page(response: AgentThreadResponse, thread: dict[str, Any]) -> AgentThreadResponse:
    jobs = {job["id"]: _preview_job(job) for job in thread["jobs"]}
    messages = {row["id"]: message_response(row) for row in thread["messages"]}
    costs = {key: len(value.model_dump_json().encode()) + 1 for key, value in jobs.items()}
    used = len(response.model_dump_json().encode()) + 4096  # cursors and array separators
    selected: dict[str, AgentJobResponse] = {}
    kept = []
    for entry in thread["_entries"]:
        job_id = entry["job_id"]
        message = messages.get(entry["id"]) if entry["phase"] >= 0 else None
        cost = costs[job_id] if job_id not in selected else 0
        cost += len(message.model_dump_json().encode()) + 1 if message is not None else 0
        if used + cost > THREAD_PAGE_BYTES:
            break
        used += cost
        selected[job_id] = jobs[job_id]
        if message is not None:
            response.messages.append(message)
        kept.append(entry)
    if not kept and thread["_entries"]:
        raise ValueError("Thread metadata exceeds the history page byte limit")
    response.jobs = sorted(selected.values(), key=lambda job: job.turn_no)
    order = {row["id"]: entry_key(row) for row in kept if row["phase"] >= 0}
    response.messages.sort(key=lambda message: order[message.id])
    if kept and (thread["_has_more"] or len(kept) < len(thread["_entries"])):
        response.next_cursor = history_cursor(thread["id"], entry_key(kept[-1]))
    response.sync_cursor = history_cursor(
        thread["id"],
        (max(1, thread["current_turn_no"] - 1), -2, 0),
    )
    return response
