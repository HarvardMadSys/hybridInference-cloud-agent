"""The runner's event uploads: a batch of events, or a single one.

Part of the worker surface (:mod:`cloud_agent.api.worker`), fenced the same
way: the worker token names the attempt and its lease generation, and an
attempt that lost its lease is answered 409. A runner sends its events in
batches, one transaction each. The single upload stays for runners from before
batches, which keep posting through a rolling deploy, and for a runner whose
control plane answers the batch route with 404 because it predates it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Header

from cloud_agent.api._body_limits import HostBodyLimitRoute
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.api.worker import _lease_lost, _match_job, _worker_claims
from cloud_agent.schemas import (
    WorkerEventBatchRequest,
    WorkerEventBatchResponse,
    WorkerEventRequest,
    WorkerEventResponse,
)

if TYPE_CHECKING:
    from cloud_agent.ports import AttemptCommands
    from cloud_agent.ports.environments import EnvironmentWorkspaceRepository

router = APIRouter(prefix="/v1/agent", route_class=HostBodyLimitRoute)


class _EventStorage(Protocol):
    """The capabilities an event upload reaches the store for."""

    attempt_commands: AttemptCommands

    @property
    def environments(self) -> EnvironmentWorkspaceRepository:
        """Where an environment's preparation receipt is recorded."""
        ...


@router.post("/worker/jobs/{job_id}/events", response_model=WorkerEventResponse, status_code=201)
async def worker_append_event(
    job_id: str,
    body: WorkerEventRequest,
    authorization: str | None = Header(None),
    store: _EventStorage | None = Depends(get_storage),
) -> WorkerEventResponse:
    """Append one normalized event to the job's append-only log."""
    job_store = require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    event_id = await job_store.attempt_commands.append_event(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        event_type=body.event_type,
        payload=body.payload,
        event_seq=body.event_seq,
    )
    if event_id is None:
        raise _lease_lost()
    await _record_environment_preparation(job_store, job_id, claims, body)
    return WorkerEventResponse(event_id=event_id)


@router.post(
    "/worker/jobs/{job_id}/events:batch",
    response_model=WorkerEventBatchResponse,
    status_code=201,
)
async def worker_append_events(
    job_id: str,
    body: WorkerEventBatchRequest,
    authorization: str | None = Header(None),
    store: _EventStorage | None = Depends(get_storage),
) -> WorkerEventBatchResponse:
    """Append several normalized events, oldest first, in one fenced transaction.

    What a runner sends instead of one request per event: one token check, one
    lease check and one commit for the batch. Every event is stored or none
    is, and a replayed ``event_seq`` answers with the stored event's id, so a
    batch whose answer was lost is sent again whole. A control plane without
    this route answers 404, and the runner then posts each event on its own.
    """
    job_store = require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    event_ids = await job_store.attempt_commands.append_events(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        events=[(event.event_type, event.payload, event.event_seq) for event in body.events],
    )
    if event_ids is None:
        raise _lease_lost()
    # After the commit and in the batch's order, as the single append runs it:
    # the receipt has its own fenced transaction, which locks the job.
    for event in body.events:
        await _record_environment_preparation(job_store, job_id, claims, event)
    return WorkerEventBatchResponse(event_ids=event_ids)


async def _record_environment_preparation(
    job_store: _EventStorage, job_id: str, claims: dict[str, Any], event: WorkerEventRequest
) -> None:
    """Keep an environment's preparation receipt once its event is stored."""
    if (
        event.event_type == "lifecycle"
        and event.payload
        and event.payload.get("phase") == "environment_prepared"
    ):
        await job_store.environments.record_worker_environment_preparation(
            job_id, claims["attempt_id"], claims["lease_generation"], event.payload
        )
