"""Content-free admin read models and scoped pagination for stored observations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from cloud_agent.ledger import JobUsageResponse  # noqa: TC001 — Pydantic resolves this at runtime.
from cloud_agent.schemas import (
    RunResourceSummary,  # noqa: TC001 — Pydantic resolves this at runtime.
)
from cloud_agent.thread_stats import (
    SessionStatistics,  # noqa: TC001 — Pydantic resolves this at runtime.
)


class AdminRun(BaseModel):
    """One user turn, without its prompt, result, raw error or caller metadata."""

    id: str
    thread_id: str | None = None
    title: str | None = None
    turn_no: int
    user_id: str
    #: Null for a temporary workspace.
    repo: str | None
    runtime: str
    model: str
    reasoning_effort: str | None = None
    execution_profile: str
    state: str
    created_at: datetime
    updated_at: datetime
    hosts: list[str] = Field(default_factory=list)
    physical_hosts: list[str] = Field(
        default_factory=list,
        description="Distinct physical host labels, falling back to runner IDs when unreported.",
    )
    tool_calls: int = 0
    resource_summary: RunResourceSummary | None = Field(
        default=None,
        description=(
            "Compact whole-turn member execution window for this run, or null when no "
            "attempt has one. Cheap scalars only, so a page of runs stays one query."
        ),
    )


class AdminRunList(BaseModel):
    """A stable creation-time window and a keyset page within it."""

    runs: list[AdminRun]
    since: datetime
    until: datetime
    next_cursor: str | None = None


class AdminAttempt(BaseModel):
    """The execution identity needed to scope events and resource measurements."""

    attempt_id: int
    attempt_no: int
    host: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None


class AdminRunDetail(BaseModel):
    """Stored activities and usage, with no inferred failure or queue analytics."""

    run: AdminRun
    attempts: list[AdminAttempt]
    usage: JobUsageResponse
    session_statistics: SessionStatistics | None = None


class RunFilters(BaseModel):
    """Exact-match selectors; none of them searches event or prompt content."""

    user_id: str | None = Field(None, max_length=256)
    repo: str | None = Field(None, max_length=256)
    state: str | None = Field(None, max_length=64)
    runtime: str | None = Field(None, max_length=128)
    model: str | None = Field(None, max_length=256)
    host: str | None = Field(None, max_length=256)


class RunCursor(BaseModel):
    """Creation-time tie breaker, bound to the filters and original window."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    since: AwareDatetime
    until: AwareDatetime
    created_at: AwareDatetime
    job_id: str = Field(min_length=1, max_length=256)


_Cursor = TypeVar("_Cursor", bound=BaseModel)


def cursor_scope(values: Any) -> str:
    """Bind an opaque cursor to the same query; this is not authentication."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def encode_cursor(cursor: BaseModel) -> str:
    """Encode a small, versioned cursor without exposing arbitrary SQL values."""
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, schema: type[_Cursor], scope: str) -> _Cursor | None:
    """Reject malformed, oversized or filter-mismatched cursors before SQL."""
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        parsed = schema.model_validate_json(raw)
        if getattr(parsed, "scope", None) != scope:
            raise ValueError
        return parsed
    except (ValueError, binascii.Error, ValidationError):
        raise ValueError("Invalid pagination cursor for these filters.") from None


class CreationWindowCursor(Protocol):
    """The timestamp fields shared by run and conversation list cursors."""

    since: datetime
    until: datetime
    created_at: datetime


def resolve_run_window(
    *,
    since: datetime | None,
    until: datetime | None,
    cursor: CreationWindowCursor | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Keep a seven-day default stable on later pages; bound each query to a year."""
    if cursor is not None:
        if (since is not None and since != cursor.since) or (
            until is not None and until != cursor.until
        ):
            raise ValueError("The pagination cursor belongs to a different time window.")
        since, until = cursor.since, cursor.until
    until = until or now or datetime.now(UTC)
    since = since or until - timedelta(days=7)
    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("Time window boundaries must include a time zone.")
    if not since < until or until - since > timedelta(days=366):
        raise ValueError("Use a positive time window of at most 366 days.")
    if cursor is not None and not since <= cursor.created_at < until:
        raise ValueError("The pagination cursor is outside its time window.")
    return since, until
