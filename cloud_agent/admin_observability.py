"""Content-free admin read models and scoped pagination for stored observations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from cloud_agent.ledger import JobUsageResponse  # noqa: TC001 — Pydantic resolves this at runtime.
from cloud_agent_host.event_payloads import MAX_EVENT_PAYLOAD_BYTES


class AdminRun(BaseModel):
    """One user turn, without its prompt, result, raw error or caller metadata."""

    id: str
    thread_id: str | None = None
    title: str | None = None
    turn_no: int
    user_id: str
    repo: str
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


class AdminCapabilities(BaseModel):
    """Explicitly distinguish runtime activities from an HTTP request ledger."""

    llm_requests: Literal[False] = False


class AdminRunDetail(BaseModel):
    """Stored activities and usage, with no inferred failure or queue analytics."""

    run: AdminRun
    attempts: list[AdminAttempt]
    usage: JobUsageResponse
    capabilities: AdminCapabilities = Field(default_factory=AdminCapabilities)


class AdminActivity(BaseModel):
    """An allowlisted event envelope; created_at is the receipt timestamp."""

    id: int
    attempt_id: int
    attempt_no: int
    seq: int
    event_type: str
    created_at: datetime
    tool_use_id: str | None = None
    tool_name: str | None = None
    is_error: bool | None = None
    has_content: bool = False


class AdminActivityPage(BaseModel):
    """Events ordered within attempts, from a fixed upper event ID."""

    events: list[AdminActivity]
    next_cursor: str | None = None


class AdminEventContent(BaseModel):
    """One explicitly requested, audited and byte-bounded event payload."""

    id: int
    attempt_id: int
    seq: int
    event_type: str
    created_at: datetime
    payload: dict[str, Any]
    truncated: bool = False


def bounded_event_content(row: dict[str, Any]) -> AdminEventContent:
    """Cap the complete response, including JSON escapes, envelope and notice."""
    content = AdminEventContent.model_validate(row)
    if len(content.model_dump_json().encode("utf-8")) <= MAX_EVENT_PAYLOAD_BYTES:
        return content
    original = json.dumps(content.payload, ensure_ascii=False)
    original_bytes = len(original.encode("utf-8"))
    content.truncated = True
    content.payload = {
        "preview": "",
        "truncated": True,
        "original_payload_bytes": original_bytes,
        "truncation_notice": "Event exceeds the 256 KiB response limit; showing a preview.",
    }
    low, high = 0, min(len(original), MAX_EVENT_PAYLOAD_BYTES)
    while low < high:
        middle = (low + high + 1) // 2
        content.payload["preview"] = original[:middle]
        if len(content.model_dump_json().encode("utf-8")) <= MAX_EVENT_PAYLOAD_BYTES:
            low = middle
        else:
            high = middle - 1
    content.payload["preview"] = original[:low]
    return content


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


class ActivityCursor(BaseModel):
    """Do not reuse an attempt-local sequence as a job-wide cursor."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    snapshot_id: int = Field(ge=0, le=2**63 - 1)
    attempt_no: int = Field(ge=1, le=2**31 - 1)
    attempt_id: int = Field(ge=1, le=2**63 - 1)
    seq: int = Field(ge=0, le=2**31 - 1)


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
        if parsed.scope != scope:
            raise ValueError
        return parsed
    except (ValueError, binascii.Error, ValidationError):
        raise ValueError("Invalid pagination cursor for these filters.") from None


def resolve_run_window(
    *,
    since: datetime | None,
    until: datetime | None,
    cursor: RunCursor | None,
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
