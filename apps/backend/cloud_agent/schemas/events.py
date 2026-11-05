"""Owner-visible event streams and bounded worker event ingestion."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from cloud_agent_shared.event_payloads import MAX_EVENT_BATCH_EVENTS, bound_event_payload

# Normalized event kinds (issue #1041) plus the control events the platform
# appends. The pattern is the security-relevant part: an event type is
# interpolated into the SSE ``event:`` field, so anything containing a newline
# would let a worker inject arbitrary frames into the owner's stream. Keeping
# the charset to lowercase/digits/underscore makes that structurally impossible
# while still letting runtime adapters introduce new kinds.
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class AgentJobEvent(BaseModel):
    """One normalized event from an agent job's append-only stream."""

    id: int
    attempt_id: int
    seq: int
    event_type: str
    payload: dict[str, Any] | None = None
    created_at: str | None = None


class AgentThreadEvent(AgentJobEvent):
    """One event, with the turn it belongs to.

    The per-job stream needs no such label — every row is the turn being
    watched. Across a thread it is what lets a reader see where one turn
    ended and the next began.
    """

    job_id: str
    turn_no: int = 1


class AgentThreadEventsResponse(BaseModel):
    """A page of a whole thread's events, plus the cursor to resume from."""

    events: list[AgentThreadEvent]
    next_cursor: int


class AgentJobEventsResponse(BaseModel):
    """A page of events plus the cursor to resume from."""

    events: list[AgentJobEvent]
    next_cursor: int


class WorkerEventRequest(BaseModel):
    """One normalized event reported by a worker."""

    event_type: str = Field(
        ...,
        pattern=EVENT_TYPE_PATTERN,
        description="thinking|message|tool_use|...|lifecycle",
    )
    payload: dict[str, Any] | None = None
    event_seq: int | None = Field(
        None,
        ge=1,
        description=(
            "The worker's own monotonically increasing number for this upload "
            "within its attempt. A replayed number is answered with the "
            "already-stored event's id instead of a duplicate row, which is "
            "what makes retrying a dropped response safe. Omitted by workers "
            "that predate it; those keep today's at-least-once behaviour."
        ),
    )

    @field_validator("payload")
    @classmethod
    def bound_payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep stored events bounded even when an older worker uploads them."""
        return bound_event_payload(value) if value is not None else None


class WorkerEventResponse(BaseModel):
    """Accepted event id (the SSE cursor value)."""

    event_id: int


class WorkerBatchEvent(WorkerEventRequest):
    """One event of a batch, which always carries the worker's number for it."""

    event_seq: int = Field(
        ...,
        ge=1,
        description=(
            "The worker's own number for this event within its attempt. A batch "
            "whose answer was lost is sent again whole, and each number already "
            "stored is answered with that event's id instead of a duplicate row."
        ),
    )


class WorkerEventBatchRequest(BaseModel):
    """Normalized events a worker reports together, oldest first."""

    events: list[WorkerBatchEvent] = Field(
        ...,
        min_length=1,
        max_length=MAX_EVENT_BATCH_EVENTS,
        description="Stored in this order, so their numbers must increase.",
    )

    @model_validator(mode="after")
    def _numbered_in_order(self) -> WorkerEventBatchRequest:
        """Refuse a batch whose order and numbering disagree."""
        if any(later.event_seq <= earlier.event_seq for earlier, later in pairwise(self.events)):
            raise ValueError("event_seq must increase within a batch")
        return self


class WorkerEventBatchResponse(BaseModel):
    """The id of each event in the batch, in the order it was sent."""

    event_ids: list[int]
