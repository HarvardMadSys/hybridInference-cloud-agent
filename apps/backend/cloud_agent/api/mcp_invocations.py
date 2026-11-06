"""The owner's decisions on MCP calls that wait for one: the inbox, history and verdicts.

Split from :mod:`cloud_agent.api.mcp_connections`, whose router serves these
routes as its own. Every read is scoped to the owner in the query, because a
held call's arguments are shown decrypted; a decision is conditional in SQL,
so one that arrives after the call expired does not revive it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.api.mcp_connections_shared import (
    _SNAPSHOT_SURFACES,
    _bad_request,
    _conflict,
    _McpConnectionsStorage,
    _not_found,
    _require_store,
)
from cloud_agent.mcp.models import Capability, CapabilityKind, InvocationStatus

router = APIRouter(prefix="/v1/agent/mcp", tags=["agent-mcp"])


class InvocationView(BaseModel):
    """One call that needed a decision."""

    id: int
    job_id: str
    #: The attempt that raised the call, even when the job has since been retried.
    attempt_id: int | None = None
    thread_id: str
    thread_title: str | None = None
    connection_id: int
    connection_label: str | None = None
    capability_name: str
    capability_kind: str = CapabilityKind.TOOL.value
    #: What the server says the capability does, from the catalogue the call
    #: was held under. ``None`` once a newer discovery has replaced it, so a
    #: card never explains a call with a description it was not made against.
    capability_description: str | None = None
    #: The server's hints (title, read-only, destructive, ...). Untrusted and
    #: for display only; they never decide authorization.
    capability_annotations: dict[str, Any] = Field(default_factory=dict)
    status: str
    always_allow_status: str | None = None
    arguments_digest: str
    arguments: dict[str, Any] | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    result_summary: str | None = None
    guidance: str = ""


class InvocationsResponse(BaseModel):
    """A page of the owner's waiting calls or scoped history, newest first."""

    invocations: list[InvocationView] = Field(default_factory=list)
    has_more: bool = False
    next_before_id: int | None = None


class DecisionRequest(BaseModel):
    """Approve or deny one pending call."""

    model_config = ConfigDict(extra="forbid")

    approve: bool
    always_allow: bool = False


@router.get("/invocations", response_model=InvocationsResponse)
async def list_invocations(
    job_id: str | None = Query(default=None),
    thread_id: str | None = Query(default=None),
    pending_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, gt=0, le=2**63 - 1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> InvocationsResponse:
    """Page one job's history, one conversation, or the owner's pending inbox.

    The owner is part of the query, not a check after it. These rows carry the
    tool call's arguments decrypted for display -- code, documents, whatever the
    model was about to send -- so a listing that trusts the caller's ``job_id``
    is a way to read another user's calls by guessing an id. The named job is
    checked first so a caller learns "not yours" the same way it learns "does
    not exist": with a 404 that says nothing about which it was.
    """
    job_store = _require_store(store)
    if job_id is None and thread_id is None and not pending_only:
        raise _bad_request("name a job or a conversation")
    if job_id is not None:
        job = await job_store.conversation_queries.get_job(job_id)
        if (
            job is None
            or str(job.get("user_id")) != user["user_id"]
            or job.get("deleted_at") is not None
        ):
            raise _not_found("no such job")
    statuses = [InvocationStatus.PENDING.value] if pending_only else None
    rows = await job_store.mcp.list_mcp_invocations(
        job_id=job_id,
        thread_id=thread_id,
        statuses=statuses,
        user_id=user["user_id"],
        limit=limit + 1,
        before_id=before_id,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    # One catalogue read per connection, not per row: a history page is mostly
    # calls to the same few servers, and a catalogue carries every schema.
    snapshots: dict[int, Mapping[str, Any] | None] = {}
    threads: dict[str, Mapping[str, Any] | None] = {}
    return InvocationsResponse(
        invocations=[
            await _invocation_view(job_store, row, user["user_id"], snapshots, threads)
            for row in rows
        ],
        has_more=has_more,
        next_before_id=int(rows[-1]["id"]) if has_more else None,
    )


@router.post("/invocations/{invocation_id}/decision", response_model=InvocationView)
async def decide_invocation(
    invocation_id: int,
    body: DecisionRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> InvocationView:
    """Approve or deny one pending call.

    Scoped by owner at the read, and decided conditionally in SQL: an approval
    that arrives after the call expired does not revive it, and a decision by
    somebody who does not own the job is refused before anything is written.
    """
    job_store = _require_store(store)
    existing = await job_store.mcp.get_mcp_invocation(invocation_id=invocation_id)
    if existing is None:
        raise _not_found("no such MCP call")
    job = await job_store.conversation_queries.get_job(str(existing["job_id"]))
    if (
        job is None
        or str(job.get("user_id")) != user["user_id"]
        or job.get("deleted_at") is not None
    ):
        raise _not_found("no such MCP call")
    rule: dict[str, Any] | None = None
    if body.always_allow:
        if not body.approve:
            raise HTTPException(status_code=422, detail="Always allow requires approval")
        snapshot = await job_store.mcp.get_mcp_capabilities(
            connection_id=int(existing["connection_id"])
        )
        kind = str(existing.get("capability_kind") or "tool")
        entry = _held_capability(snapshot, existing, kind)
        if entry is None:
            raise _conflict("the capability changed; review the current permissions")
        schema = entry.get("input_schema")
        capability = Capability(
            kind=CapabilityKind(kind),
            name=str(entry["name"]),
            description=str(entry.get("description") or ""),
            input_schema=schema if isinstance(schema, Mapping) else None,
        )
        rule = {"policy": "allow", "fingerprint": capability.fingerprint()}
    decision_args: dict[str, Any] = {
        "invocation_id": invocation_id,
        "approve": body.approve,
        "decided_by": user["user_id"],
    }
    if rule is not None:
        decision_args["always_allow_rule"] = rule
    decided = await job_store.mcp.decide_mcp_invocation(**decision_args)
    if decided is None:
        raise _conflict("this call was already decided, or it expired")
    return await _invocation_view(job_store, decided, user["user_id"])


async def _invocation_view(
    job_store: _McpConnectionsStorage,
    row: Mapping[str, Any],
    user_id: str,
    snapshots: dict[int, Mapping[str, Any] | None] | None = None,
    threads: dict[str, Mapping[str, Any] | None] | None = None,
) -> InvocationView:
    """Render one invocation, including the arguments the owner is approving.

    ``snapshots`` and ``threads`` cache metadata across one bounded page.
    """
    from cloud_agent.mcp.host import decode_arguments
    from cloud_agent.mcp.invocations import recovery_note
    from cloud_agent.mcp.models import Invocation, InvocationStatus as Status

    arguments: dict[str, Any] | None = None
    if row.get("arguments_ciphertext"):
        try:
            arguments = decode_arguments(
                mcp_invocation_cipher().decrypt(str(row["arguments_ciphertext"]))
            )
        except Exception:
            arguments = None
    connection_id = int(row["connection_id"])
    connection = await job_store.mcp.get_mcp_connection(connection_id=connection_id)
    if snapshots is None:
        snapshots = {}
    if connection_id not in snapshots:
        snapshots[connection_id] = await job_store.mcp.get_mcp_capabilities(
            connection_id=connection_id
        )
    kind = str(row.get("capability_kind") or CapabilityKind.TOOL.value)
    capability = _held_capability(snapshots[connection_id], row, kind)
    thread_id = str(row["thread_id"])
    if threads is None:
        threads = {}
    if thread_id not in threads:
        threads[thread_id] = await job_store.conversation_queries.get_thread(thread_id)
    thread = threads[thread_id]
    thread_title = (
        str(thread["title"])
        if thread is not None and str(thread.get("user_id")) == user_id and thread.get("title")
        else None
    )
    status = Status(str(row["status"]))
    expires_at = row.get("expires_at")
    # A worker persists expiry asynchronously. Display an elapsed deadline
    # immediately, without writing during a read or reviving any decision.
    if isinstance(expires_at, datetime) and status in {Status.PENDING, Status.APPROVED}:
        deadline = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
        if deadline <= datetime.now(UTC):
            status = Status.EXPIRED
    invocation = Invocation(
        invocation_id=str(row["id"]),
        job_id=str(row["job_id"]),
        attempt_id=str(row.get("attempt_id") or ""),
        lease_generation=int(row.get("lease_generation") or 0),
        thread_id=thread_id,
        connection_id=str(row["connection_id"]),
        capability_name=str(row["capability_name"]),
        arguments_digest=str(row["arguments_digest"]),
        status=status,
    )
    return InvocationView(
        id=int(row["id"]),
        job_id=str(row["job_id"]),
        attempt_id=int(row["attempt_id"]) if row.get("attempt_id") is not None else None,
        thread_id=thread_id,
        thread_title=thread_title,
        connection_id=int(row["connection_id"]),
        connection_label=str(connection["label"]) if connection else None,
        capability_name=str(row["capability_name"]),
        capability_kind=kind,
        capability_description=(
            str(capability.get("description") or "") if capability is not None else None
        ),
        capability_annotations=(
            dict(capability.get("annotations") or {}) if capability is not None else {}
        ),
        status=status.value,
        always_allow_status=row.get("always_allow_status"),
        arguments_digest=str(row["arguments_digest"]),
        arguments=arguments,
        created_at=row.get("created_at"),
        expires_at=row.get("expires_at"),
        decided_at=row.get("decided_at"),
        result_summary=row.get("result_summary"),
        guidance=recovery_note(invocation),
    )


def _held_capability(
    snapshot: Mapping[str, Any] | None, row: Mapping[str, Any], kind: str
) -> Mapping[str, Any] | None:
    """The catalogue entry a held call names, if it is still the one it was held under.

    A discovery that changed anything moves the capability revision, and a
    call held under an older one cannot be dispatched on this approval. Its
    entry in the newer catalogue may describe something else, so none is
    returned rather than a description the call was not made against.
    """
    if snapshot is None or snapshot.get("capability_revision") != row.get("capability_revision"):
        return None
    key = next((key for key, surface in _SNAPSHOT_SURFACES if surface.value == kind), None)
    if key is None:
        return None
    name = str(row["capability_name"])
    for entry in snapshot.get(key) or []:
        if isinstance(entry, Mapping) and str(entry.get("name") or "") == name:
            return entry
    return None


def mcp_invocation_cipher() -> Any:
    """The cipher a held call's arguments are stored under."""
    from cloud_agent.mcp.registry import invocation_cipher

    return invocation_cipher()


__all__ = [
    "DecisionRequest",
    "InvocationView",
    "InvocationsResponse",
    "mcp_invocation_cipher",
    "router",
]
