"""Metadata-only inventory and reviewed controls for retained sandbox groups."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.admin_observability import cursor_scope
from cloud_agent_shared.sandbox_resources import parse_sandbox_resource_sample

SANDBOX_CURSOR_SCOPE = cursor_scope(["sandboxes", "running"])
SANDBOX_TRAJECTORY_MEMBER_LIMIT = 128
RESOURCE_STALE_SECONDS = 90.0
SandboxState = Literal[
    "provisioning", "starting", "running", "stopping", "stopped", "stopped_dirty", "lost"
]
SandboxStateFilter = Literal[
    "all", "provisioning", "starting", "running", "stopping", "stopped", "stopped_dirty", "lost"
]


def sandbox_cursor_scope(state: SandboxStateFilter, include_archived: bool = False) -> str:
    """Bind continuation tokens to the lifecycle and inventory visibility."""
    parts = ["sandboxes", state]
    if include_archived:
        parts.append("include_archived")
    return cursor_scope(parts)


def sandbox_trajectory_cursor_scope(sandbox_id: str) -> str:
    """Bind continuation tokens to one immutable sandbox identity."""
    return cursor_scope(["sandbox-trajectory", sandbox_id])


class AdminSandboxResourceUsage(BaseModel):
    """Docker container totals across every member, with explicit sample freshness."""

    source: Literal["docker-stats"] = "docker-stats"
    scope: Literal["container"] = "container"
    sample_id: str
    sampled_at: datetime
    received_at: datetime
    age_seconds: float = Field(ge=0)
    stale: bool
    cpu_cores: float | None = None
    cpu_window_seconds: float | None = None
    memory_bytes: int | None = None
    memory_limit_bytes: int | None = None
    pids: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None
    io_read_bytes_per_second: float | None = None
    io_write_bytes_per_second: float | None = None
    net_rx_bytes_per_second: float | None = None
    net_tx_bytes_per_second: float | None = None


def resource_usage_view(
    snapshot: Any,
    *,
    sampled_at: datetime | None,
    received_at: datetime | None,
    now: datetime,
) -> AdminSandboxResourceUsage | None:
    """Revalidate optional persisted counters and retain the original sampling age."""
    sample = parse_sandbox_resource_sample(snapshot)
    if sample is None or sampled_at is None or received_at is None:
        return None
    if sampled_at.tzinfo is None:
        sampled_at = sampled_at.replace(tzinfo=UTC)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    age = max(0.0, (now - sampled_at).total_seconds())
    metrics = sample.model_dump(exclude={"schema_version", "sample_age_ms"})
    return AdminSandboxResourceUsage(
        **metrics,
        sampled_at=sampled_at,
        received_at=received_at,
        age_seconds=age,
        stale=age > RESOURCE_STALE_SECONDS,
    )


class AdminSandbox(BaseModel):
    """One container shared by its current chat members, including idle groups."""

    id: str
    generation: int = Field(1, ge=1)
    origin_thread_id: str
    title: str | None = None
    #: Null for a temporary workspace.
    repo: str | None
    user_id: str
    host: str
    container_id: str | None = None
    container_name: str
    runtime_id: str
    runtime_version: str | None = None
    execution_profile: str
    state: SandboxState = "running"
    stop_requested_at: datetime | None = None
    archived_at: datetime | None = None
    deletion_state: Literal["none", "pending", "archived", "deleted", "failed"] = "none"
    deletion_discard: bool = False
    deletion_request_id: str | None = None
    deletion_requested_at: datetime | None = None
    deletion_archive_key: str | None = None
    deletion_archive_version_id: str | None = None
    deletion_sha256: str | None = None
    deletion_size_bytes: int | None = Field(None, gt=0)
    deletion_error: str | None = None
    deleted_at: datetime | None = None
    memory_limit: str
    cpu_limit: str
    disk_limit_mib: int = Field(0, ge=0, le=16777216, strict=True)
    member_count: int = Field(ge=0)
    active_attempt_count: int = Field(ge=0)
    resident_slot_capacity: int = Field(8, ge=1)
    active_operation_count: int = Field(0, ge=0)
    snapshot_state: str = "none"
    terminal_count: int | None = Field(None, ge=0)
    terminal_count_observed_at: datetime | None = None
    resource_usage: AdminSandboxResourceUsage | None = None
    created_at: datetime
    last_activity_at: datetime
    idle_deadline: datetime | None = None
    updated_at: datetime
    host_last_heartbeat_at: datetime | None = None


class AdminSandboxList(BaseModel):
    """A bounded page of the current inventory, ordered by sandbox identity."""

    sandboxes: list[AdminSandbox]
    total_count: int = Field(ge=0)
    observed_at: datetime
    next_cursor: str | None = None


class SandboxCursor(BaseModel):
    """Continue a running-container inventory after one sandbox identity."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    after_id: str = Field(min_length=1, max_length=256)


class AdminSandboxMember(BaseModel):
    """Bounded member identity and occupancy, without native session content."""

    thread_id: str
    title: str | None = None
    user_id: str
    resident_slot_id: int | None = None
    resident_state: str
    context_status: str
    native_state: str
    active_attempt_id: int | None = None
    last_settled_job_id: str | None = None
    created_at: datetime
    updated_at: datetime


class AdminSandboxCheckpoint(BaseModel):
    """The latest restorable checkpoint a host reported for this sandbox."""

    id: str
    generation: int
    host: str
    size_bytes: int
    new_layer_bytes: int
    state: Literal["reported", "verified", "superseded", "deleted"]
    captured_at: datetime
    recorded_at: datetime
    durations_ms: dict[str, int] = Field(default_factory=dict)


class AdminSandboxDetail(BaseModel):
    """A consistent sandbox inspection with at most 128 current members."""

    sandbox: AdminSandbox
    members: list[AdminSandboxMember] = Field(max_length=128)
    observed_at: datetime
    terminal_activity_error: str | None = None
    latest_checkpoint: AdminSandboxCheckpoint | None = None


class AdminSandboxStopRequest(BaseModel):
    """The exact container and activity the administrator reviewed."""

    model_config = ConfigDict(extra="forbid")

    generation: int = Field(ge=1)
    container_id: str = Field(min_length=1, max_length=256)
    last_activity_at: datetime


class AdminSandboxArchiveRequest(BaseModel):
    """The exact sandbox reviewed for reversible inventory removal or restore."""

    model_config = ConfigDict(extra="forbid")

    generation: int = Field(ge=1)
    container_id: str | None = Field(min_length=1, max_length=256)
    last_activity_at: datetime


class SandboxTrajectoryCursor(BaseModel):
    """Continue newest-first through one fixed event snapshot."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    snapshot_id: int = Field(ge=0, le=2**63 - 1)
    before_id: int = Field(ge=1, le=2**63 - 1)


class AdminSandboxTrajectoryEvent(BaseModel):
    """One content-free retained-sandbox lifecycle observation."""

    id: int
    generation: int = Field(ge=1)
    event_type: str
    from_state: str | None = None
    to_state: str | None = None
    source: Literal["system", "admin", "host", "migration"]
    actor_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AdminSandboxTrajectoryPage(BaseModel):
    """Current tombstone and bounded membership context beside a stable event page."""

    sandbox: AdminSandbox
    members: list[AdminSandboxMember] = Field(max_length=SANDBOX_TRAJECTORY_MEMBER_LIMIT)
    member_history_count: int = Field(ge=0)
    member_history_truncated: bool
    events: list[AdminSandboxTrajectoryEvent]
    observed_at: datetime
    next_cursor: str | None = None
    latest_checkpoint: AdminSandboxCheckpoint | None = None
