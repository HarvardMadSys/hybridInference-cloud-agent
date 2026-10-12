"""Reviewed, bounded metadata projections for the admin database browser.

This policy is deliberately an allowlist. Adding a database column or table
does not expose it to the browser. Credentials, user content, configuration,
opaque JSON, error messages and transport handles are excluded before SQL is
built; none can be selected, sorted or used as a filter.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# These columns describe execution identity, status, timing and resources.
# Free-form content and configuration are intentionally absent even on tables
# whose other columns are useful to an administrator.
REVIEWED_TABLES: dict[str, tuple[str, ...]] = {
    "agent_threads": (
        "id",
        "user_id",
        "execution_profile",
        "archived_at",
        "pinned_at",
        "deleted_at",
        "created_at",
        "updated_at",
    ),
    "agent_jobs": (
        "id",
        "thread_id",
        "user_id",
        "parent_job_id",
        "turn_no",
        "native_turn_no",
        "fork_source_job_id",
        "runtime",
        "model",
        "reasoning_effort",
        "state",
        "cancel_requested",
        "terminal_resume_pending",
        "current_attempt_id",
        "created_at",
        "updated_at",
    ),
    "agent_attempts": (
        "id",
        "job_id",
        "thread_id",
        "attempt_no",
        "lease_generation",
        "lease_expires_at",
        "terminal_ready",
        "status",
        "started_at",
        "finished_at",
        "host_id",
        "sandbox_runtime",
        "runtime_version",
        "continuity_mode",
        "context_status",
    ),
    "agent_runner_hosts": (
        "host",
        "first_seen_at",
        "last_seen_at",
        "last_heartbeat_at",
        "status",
        "slots_total",
        "slots_used",
        "runner_version",
        "sandbox_runtime",
    ),
    "agent_sandboxes": (
        "id",
        "user_id",
        "origin_thread_id",
        "generation",
        "host",
        "runtime_id",
        "runtime_version",
        "execution_profile",
        "state",
        "last_activity_at",
        "idle_deadline",
        "idle_timeout_seconds",
        "active_operation_count",
        "resident_slot_capacity",
        "workspace_epoch",
        "snapshot_state",
        "created_at",
        "updated_at",
    ),
    "agent_job_events": (
        "id",
        "job_id",
        "attempt_id",
        "seq",
        "client_seq",
        "event_type",
        "created_at",
    ),
    "agent_thread_messages": ("id", "thread_id", "job_id", "role", "source_event_id", "created_at"),
    "agent_sandbox_snapshots": (
        "id",
        "sandbox_id",
        "workspace_epoch",
        "anchor_job_id",
        "state",
        "created_at",
        "updated_at",
        "captured_at",
        "published_at",
        "retry_at",
    ),
    "agent_snapshot_jobs": ("snapshot_id", "job_id", "created_at"),
    "agent_thread_sandbox_memberships": (
        "thread_id",
        "user_id",
        "sandbox_id",
        "native_state",
        "context_status",
        "activation_generation",
        "process_generation",
        "residency_generation",
        "resident_state",
        "resident_slot_id",
        "active_attempt_id",
        "admitted_job_id",
        "admitted_at",
        "last_settled_job_id",
        "forked_from_thread_id",
        "fork_anchor_job_id",
        "fork_operation_id",
        "created_at",
        "updated_at",
    ),
    "agent_attempt_resources": (
        "attempt_id",
        "job_id",
        "cpu_usec",
        "mem_peak_bytes",
        "mem_avg_bytes",
        "io_read_bytes",
        "io_write_bytes",
        "net_rx_bytes",
        "net_tx_bytes",
        "sample_count",
        "coverage",
        "sampler_version",
        "tool_calls_truncated",
        "tool_time_ms",
        "process_observation_complete",
        "process_events_lost",
        "processes_truncated",
        "created_at",
    ),
    "agent_tool_call_resources": (
        "attempt_id",
        "call_seq",
        "tool_name",
        "timing_source",
        "duration_ms",
        "sample_count",
        "overlap_n",
        "measured_span_ms",
        "cpu_usec_exclusive",
        "cpu_usec_shared",
        "mem_peak_exclusive_bytes",
        "mem_peak_shared_bytes",
        "process_count",
        "process_cpu_usec",
        "process_mem_peak_bytes",
        "process_io_read_bytes",
        "process_io_write_bytes",
        "process_resources_partial",
        "created_at",
    ),
    "agent_admin_audit_log": (
        "id",
        "action",
        "admin_ip",
        "actor_id",
        "target_user_id",
        "success",
        "created_at",
    ),
}

TEXT_TYPES = frozenset({"text", "character varying", "character"})
INTEGER_TYPES = frozenset({"smallint", "integer", "bigint"})
NUMBER_TYPES = INTEGER_TYPES | {"real", "double precision", "numeric"}
TIME_TYPES = frozenset({"date", "timestamp with time zone", "timestamp without time zone"})
SUPPORTED_TYPES = TEXT_TYPES | NUMBER_TYPES | TIME_TYPES | {"boolean", "uuid"}
MAX_RESULT_BYTES = 2_000_000
MAX_TEXT_CHARACTERS = 2048


class DatabaseColumn(BaseModel):
    """One reviewed scalar column that exists in the current schema."""

    name: str
    data_type: str
    nullable: bool


class DatabaseTable(BaseModel):
    """The safe schema of one table, including its deterministic row key."""

    name: str
    columns: list[DatabaseColumn]
    primary_key: list[str]


class DatabaseTables(BaseModel):
    """Available reviewed base tables in the Agent database."""

    tables: list[DatabaseTable]


class DatabaseFilter(BaseModel):
    """A structured comparison; a filter never contains SQL."""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=128)
    operator: Literal["eq", "ne", "contains", "gt", "gte", "lt", "lte", "is_null", "is_not_null"]
    value: str | None = Field(default=None, max_length=2048)


class DatabaseQuery(BaseModel):
    """A bounded read of one reviewed table with AND-combined filters."""

    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, max_length=128)
    filters: list[DatabaseFilter] = Field(default_factory=list, max_length=12)
    order_by: str | None = Field(default=None, min_length=1, max_length=128)
    order_direction: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=50, ge=1, le=200, strict=True)
    offset: int = Field(default=0, ge=0, le=100_000, strict=True)


class DatabaseQueryResult(BaseModel):
    """Column-aligned scalar values; large integers retain exact precision."""

    table: str
    columns: list[str]
    rows: list[list[Any]]
    limit: int
    offset: int
    has_more: bool
    elapsed_ms: float
    text_limit: int


class DatabaseQueryInvalid(ValueError):
    """A query cannot be applied to the reviewed scalar schema."""


class DatabaseQueryTimeout(Exception):
    """The database refused a read that exceeded its statement budget."""


class DatabaseQueryUnavailable(Exception):
    """A read could not complete without disclosing database diagnostics."""
