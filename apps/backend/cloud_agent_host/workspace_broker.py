"""Internal broker for live agent workspaces.

The public gateway authenticates workspace owners and forwards only a validated
job id. This service is deliberately separate from the gateway: it can reach
the host Docker daemon, while the public-facing backend cannot. Commands, Git
inspection, terminals, and Agent CLI transports all execute inside the exact
retained chat container; no host workspace is mounted into either service.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import functools
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from cloud_agent_host import broker_profiling_policy
from cloud_agent_host.chat_sandbox import (
    CHAT_WORKDIR,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatBackend,
    RetainedChatLost,
)
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.guest_observer import (
    GuestObservation,
    add_process_counts,
    add_process_resources,
    attribute_processes,
)
from cloud_agent_host.logging_utils import configure_process_logging, get_logger
from cloud_agent_host.operation_call import BrokerCall, closed, open_call
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.resources import shared_member_resource_rollup
from cloud_agent_host.retained_workspace import (
    RetainedWorkspaceError,
    read_retained_file,
    write_retained_file,
)
from cloud_agent_host.runtimes import RuntimeEffortUnsupportedError, RuntimeMCPUnavailableError
from cloud_agent_host.sandbox import (
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    SandboxSpec,
    TerminalNotSupportedError,
    TerminalProcess,
    build_backend_from_env,
)
from cloud_agent_host.sandbox_archive import (
    MAX_ARCHIVE_BYTES,
    ArchiveIdentity,
    SandboxArchiveService as SandboxDeletionArchiveService,
)
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveConfigError
from cloud_agent_host.sandbox_resources import SandboxResourceCollector
from cloud_agent_host.session_lifecycle import (
    DEFAULT_CHAT_IDLE_TIMEOUT_S,
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedTurnBoundaryError,
    SharedTurnTelemetry,
)
from cloud_agent_host.source_archive import DEFAULT_MAX_BASE_BUNDLE_BYTES
from cloud_agent_host.tool_windows import ToolWindowTracker, attribute, tool_time_ms
from cloud_agent_host.workspace_browser import (
    WorkspacePathError,
    normalize_workspace_path,
)
from cloud_agent_shared.host_settings import (
    BROKER_SETTING_KEYS,
    HOST_SETTINGS_BY_KEY,
    admin_values,
    default_values,
    resolve,
)
from cloud_agent_shared.operation_profiling import PROFILING_CONTEXT_HEADER
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicyError

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from cloud_agent_host.sandbox_archives import SandboxArchiveService

_DEFAULT_SETTINGS = default_values(BROKER_SETTING_KEYS)
# Protocol validation keeps these upper bounds even when the runtime admin
# value is lower. The shared registry is the single source for defaults.
MAX_FILE_WRITE_BYTES = _DEFAULT_SETTINGS["workspace_file_write_max_mib"] * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = _DEFAULT_SETTINGS["workspace_command_output_max_mib"] * 1024 * 1024
MAX_DIRECTORY_ENTRIES = _DEFAULT_SETTINGS["workspace_directory_max_entries"]
DEFAULT_COMMAND_TIMEOUT_S = float(_DEFAULT_SETTINGS["workspace_command_timeout_s"])
MAX_TERMINAL_SESSIONS_PER_WORKSPACE = _DEFAULT_SETTINGS["terminal_max_sessions_per_workspace"]
DEFAULT_MAX_TERMINAL_SESSIONS = _DEFAULT_SETTINGS["terminal_max_sessions"]
MAX_TERMINAL_BUFFER_BYTES = _DEFAULT_SETTINGS["terminal_buffer_max_mib"] * 1024 * 1024
MAX_TERMINAL_BUFFER_EVENTS = _DEFAULT_SETTINGS["terminal_buffer_max_events"]
MAX_TERMINAL_TOTAL_OUTPUT_BYTES = _DEFAULT_SETTINGS["terminal_output_max_mib"] * 1024 * 1024
MAX_TERMINAL_INPUT_BYTES = _DEFAULT_SETTINGS["terminal_input_max_kib"] * 1024
MAX_TERMINAL_STREAMS_PER_SESSION = _DEFAULT_SETTINGS["terminal_max_streams_per_session"]
DEFAULT_TERMINAL_IDLE_TTL_S = float(_DEFAULT_SETTINGS["terminal_idle_ttl_s"])
DEFAULT_TERMINAL_HARD_TTL_S = float(_DEFAULT_SETTINGS["terminal_hard_ttl_s"])
TERMINAL_STREAM_WAIT_S = 15.0
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


class _EndpointLifecycleCalls:
    """Track executor work through request cancellation and broker shutdown."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._accepting = True
        self._calls: set[asyncio.Future[Any]] = set()

    async def run(
        self,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Admit one executor call, retaining ownership until it really exits."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            if not self._accepting:
                raise HTTPException(status_code=503, detail="workspace broker is shutting down")
            future = loop.run_in_executor(None, functools.partial(function, *args, **kwargs))
            self._calls.add(future)
            future.add_done_callback(self._call_finished)
        # Cancellation of an ASGI request must not detach an already-running
        # lifecycle call from broker shutdown ownership.
        return await asyncio.shield(future)

    def _call_finished(self, future: asyncio.Future[Any]) -> None:
        loop = future.get_loop()
        if not loop.is_closed():
            loop.create_task(self._discard(future))

    async def _discard(self, future: asyncio.Future[Any]) -> None:
        async with self._lock:
            self._calls.discard(future)

    async def seal_and_wait(self) -> None:
        """Reject new executor calls and wait for every admitted call to exit."""
        async with self._lock:
            self._accepting = False
            calls = tuple(self._calls)
        if calls:
            await asyncio.gather(
                *(asyncio.shield(call) for call in calls),
                return_exceptions=True,
            )
        async with self._lock:
            self._calls.difference_update(calls)


def _chat_idle_timeout_from_env() -> float:
    """Read the one canonical chat-group inactivity clock."""
    raw = os.environ.get("AGENT_CHAT_IDLE_TIMEOUT_S")
    return float(raw) if raw else DEFAULT_CHAT_IDLE_TIMEOUT_S


class FileWriteRequest(BaseModel):
    """One bounded UTF-8 file replacement."""

    content: str = Field(..., max_length=MAX_FILE_WRITE_BYTES)


class TerminalRequest(BaseModel):
    """One interactive-shell command inside the retained workspace."""

    command: str = Field(..., min_length=1, max_length=8000)
    cwd: str = Field("/workspace", min_length=1, max_length=4096)
    timeout_seconds: float = Field(DEFAULT_COMMAND_TIMEOUT_S, gt=0, le=120)


class TerminalCreateRequest(BaseModel):
    """Dimensions for one persistent interactive terminal."""

    rows: int = Field(24, ge=2, le=200)
    cols: int = Field(80, ge=20, le=500)


class TerminalInputRequest(BaseModel):
    """One bounded base64-encoded terminal input chunk."""

    data: str = Field(..., min_length=1, max_length=88_000)


class TerminalResizeRequest(BaseModel):
    """A validated PTY window size."""

    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)


class TerminalCoordinationRequest(BaseModel):
    """Worker fence plus the protected turn owning one suspension token."""

    lease_generation: int = Field(..., ge=1)
    # Optional only for a previous-generation control plane during a rolling
    # deploy. Current callers always send it so concurrent fork siblings hold
    # independent suspension tokens in their shared workspace.
    job_id: str | None = Field(None, min_length=1, max_length=128)


class TerminalSettledRequest(BaseModel):
    """Release only the terminal suspension owned by one settled turn."""

    job_id: str | None = Field(None, min_length=1, max_length=128)


class SharedLeaseRequest(BaseModel):
    """Receipt for one shared member activation mutation."""

    token: str = Field(..., min_length=1, max_length=512)


class SharedInputRequest(BaseModel):
    """The control plane's message for one member's running turn.

    ``owner`` names the activation the control plane saw running
    (``attempt:<id>``); a member now leased to anyone else does not take it.
    ``message_id`` names the waiting message, so the turn's stream can say
    which one it took.
    """

    owner: str = Field(..., min_length=1, max_length=128)
    message_id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=100_000)


class SharedResidencyLostRequest(BaseModel):
    """Control-plane rejection of one exact member residency generation."""

    generation: int = Field(..., ge=1)


class SharedQuestionAnswerRequest(BaseModel):
    """A bounded native-question reply for one exact activation owner."""

    owner: str = Field(..., min_length=1, max_length=128)
    request_id: str = Field(..., min_length=1, max_length=128)
    answers: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[list[Annotated[str, Field(max_length=10_000)]], Field(max_length=20)],
    ] = Field(default_factory=dict, max_length=10)
    reject: bool = False


class SharedAbandonRequest(SharedLeaseRequest):
    """Release a failed local admission without claiming a settled context."""

    lost: bool = False


class SharedIdleStopRequest(BaseModel):
    """Durable control-plane fence authorizing one shared-container stop."""

    generation: int = Field(..., ge=1)
    # A broker reconstructed before the first agent activation may observe a
    # generation-zero container after a file/terminal wake. The durable stop
    # fence must still be able to name that exact container.
    activation_generation: int = Field(..., ge=0)
    reason: Literal["idle", "orphaned_attempt", "lost"] = "idle"
    member_activation_generations: (
        dict[Annotated[str, Field(min_length=1, max_length=256)], Annotated[int, Field(ge=0)]]
        | None
    ) = Field(None, max_length=128)


class SandboxArchivePrepareRequest(BaseModel):
    """Exact stopped identity and immutable S3 object for one admin deletion."""

    request_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)
    expected_container_id: str = Field(..., min_length=1, max_length=256)
    archive_key: str = Field(..., min_length=1, max_length=1024)


class SandboxArchiveCommitRequest(SandboxArchivePrepareRequest):
    """Prepared receipt independently verified by the trusted control plane."""

    sha256: str = Field(..., pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(..., gt=0, le=MAX_ARCHIVE_BYTES)


class SandboxArchiveUploadRequest(SandboxArchiveCommitRequest):
    """Checksum-bound single-object upload with no S3 credential on the host."""

    upload_url: str = Field(..., min_length=1, max_length=16384)
    upload_headers: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[str, Field(max_length=2048)],
    ] = Field(..., min_length=4, max_length=4)


class SharedSandboxMemberRequest(BaseModel):
    """Authoritative durable identity for one member alias."""

    thread_id: str = Field(..., min_length=1, max_length=256)
    native_session_id: str | None = Field(None, max_length=512)
    context_status: Literal["new", "live", "restored", "lost"] = "new"
    activation_generation: int = Field(0, ge=0)
    process_generation: int = Field(0, ge=0)
    residency_generation: int = Field(0, ge=0)
    resident_state: Literal["stopped", "starting", "live", "restarting", "lost"] = "stopped"
    resident_slot_id: int = Field(..., ge=0, le=127)
    last_settled_job_id: str | None = Field(None, max_length=256)


class SharedSandboxIdentityRequest(BaseModel):
    """Immutable group identity plus its current member manifest."""

    sandbox_id: str = Field(..., min_length=1, max_length=256)
    generation: int = Field(..., ge=1)
    origin_thread_id: str = Field(..., min_length=1, max_length=256)
    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)
    sandbox_image_digest: str | None = Field(None, max_length=512)
    expected_container_id: str | None = Field(None, min_length=1, max_length=256)
    resident_slot_capacity: int = Field(8, ge=2, le=128)
    memory_limit: str = Field("4g", min_length=1, max_length=32)
    cpu_limit: str = Field("2", min_length=1, max_length=32)
    disk_limit_mib: int = Field(0, ge=0, le=16777216, strict=True)
    stop_timeout_s: int = Field(10, ge=1, le=300)
    members: list[SharedSandboxMemberRequest] = Field(..., min_length=1, max_length=128)


class SharedSandboxAcquireRequest(SharedSandboxIdentityRequest):
    """Lease one member while preserving sibling concurrency."""

    owner: str = Field(..., min_length=1, max_length=256)
    activation_generation: int = Field(..., ge=1)
    has_settled_context: bool = False
    #: The measurement selection the caller is profiling this path under.
    #: Deliberately unvalidated: a broker sees one call at a time and cannot
    #: know which attempt it belongs to, so the caller states the selection and
    #: `operation_profiling.start_stage_recording` decides whether to honour
    #: it. Typing or bounding this field would let a malformed *measurement*
    #: setting reject a sandbox request with 422 before the supervisor ever saw
    #: it, and measurement configuration must never fail the business call.
    profiling: Any = None


class SharedResidentLaunchConfig(BaseModel):
    """Process-fixed configuration and renewable residency credential."""

    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)
    native_session_id: str | None = Field(None, max_length=512)
    model: str = Field(..., min_length=1, max_length=256)
    reasoning_effort: str | None = Field(None, max_length=64)
    # The broker replaces this with its host-local relay address when one is
    # configured.  The fallback keeps injected/unit-test brokers usable.
    gateway_base_url: str = Field("", max_length=2048)
    credential: str | None = Field(None, max_length=16384)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    # Replaced, like the gateway's, by this host's own MCP relay address.
    mcp_base_url: str = Field("", max_length=2048)
    mcp_credential: str = Field("", max_length=16384)
    slot_id: int = Field(..., ge=0, le=127)
    residency_generation: int = Field(0, ge=0)


class SharedResidentLaunchRequest(SharedLeaseRequest):
    """Lease fence plus resident launch configuration."""

    launch: SharedResidentLaunchConfig
    #: See ``SharedSandboxAcquireRequest.profiling``. Deliberately unvalidated
    #: here too, so a malformed selection cannot reject a resident request.
    profiling: Any = None


class SharedTurnRequest(SharedLeaseRequest):
    """One prompt delivered to an already-owned resident process."""

    prompt: str = Field(..., min_length=1, max_length=2_000_000)
    model: str = Field(..., min_length=1, max_length=256)
    reasoning_effort: str | None = Field(None, max_length=64)


class SharedSettleRequest(SharedLeaseRequest):
    """One member's successful native turn boundary."""

    job_id: str = Field(..., min_length=1, max_length=256)
    context_status: Literal["new", "live", "restored"]


class SharedForkRequest(SharedSandboxIdentityRequest):
    """Prompt-free fork-only request inside one shared sandbox."""

    operation_id: str = Field(..., min_length=1, max_length=256)
    source_native_session_id: str = Field(..., min_length=1, max_length=512)
    source_native_turn_no: int = Field(..., ge=1, le=100_000)
    target_thread_id: str = Field(..., min_length=1, max_length=256)
    target_slot_id: int = Field(..., ge=0, le=127)
    source_launch: SharedResidentLaunchConfig
    target_launch: SharedResidentLaunchConfig


class SharedSnapshotCaptureRequest(SharedSandboxIdentityRequest):
    """Exact ordered workspace epoch requested by the coordinator."""

    snapshot_id: int = Field(..., ge=1)
    workspace_epoch: int = Field(..., ge=1)
    base_sha: str = Field(..., min_length=7, max_length=64)


class SharedBaseImportRequest(SharedSandboxIdentityRequest):
    """Published commit objects to add without moving shared Git state."""

    snapshot_id: int = Field(..., ge=1)
    commit_sha: str = Field(..., min_length=7, max_length=64)
    bundle_b64: str = Field(..., min_length=1)


class SharedInitialArchiveRequest(SharedSandboxIdentityRequest):
    """Exact fresh activation whose prepared workspace becomes the baseline."""

    expected_container_id: str = Field(..., min_length=1, max_length=256)
    token: str = Field(..., min_length=1, max_length=512)
    preparation_id: str = Field(..., min_length=1, max_length=256)
    base_sha: str = Field(..., min_length=7, max_length=64)


class SharedInitialArchiveStatusRequest(SharedSandboxIdentityRequest):
    """Exact fresh activation checking for a crash-committed baseline."""

    expected_container_id: str = Field(..., min_length=1, max_length=256)
    token: str = Field(..., min_length=1, max_length=512)
    preparation_id: str = Field(..., min_length=1, max_length=256)


def build_turn_resources(
    value: Any,
    tool_windows: ToolWindowTracker,
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """The resource object one finished shared turn reports.

    Named and module-level so the frame a turn actually emits can be built by a
    test. The contract between this producer and the runner that reads it lived
    in two closures either side of an HTTP hop, and a hand-written frame in the
    runner's test hid a nesting change that made every real turn report
    nothing.
    """
    telemetry = (
        value
        if isinstance(value, SharedTurnTelemetry)
        else SharedTurnTelemetry(
            observation=(
                value
                if isinstance(value, GuestObservation)
                else GuestObservation.unavailable("shared turn returned no process telemetry")
            )
        )
    )
    marks = tool_windows.apply_marks(telemetry.tool_marks)
    # One line per turn: every call's pairing, lag and reason, so mark
    # adoption can be checked from the broker log without a database read.
    logger.info("tool marks thread=%s %s", thread_id, marks.describe())
    windows = tool_windows.windows()
    resources: dict[str, Any] = shared_member_resource_rollup()
    tool_calls = attribute(
        [],
        windows,
        windows_complete=not tool_windows.truncated,
    )
    processes, process_counts = attribute_processes(
        telemetry.observation,
        windows,
        windows_complete=not tool_windows.truncated,
    )
    resources.update(
        {
            "tool_calls": add_process_resources(
                add_process_counts(tool_calls, process_counts),
                processes,
                observation_complete=telemetry.observation.complete,
            ),
            "tool_calls_truncated": tool_windows.truncated,
            "tool_time_ms": (None if tool_windows.truncated else tool_time_ms(windows)),
            "process_observer": telemetry.observation.source,
            "process_observation_complete": telemetry.observation.complete,
            "process_events_lost": telemetry.observation.lost_events,
            "processes_truncated": telemetry.observation.truncated,
            "processes": processes,
        }
    )
    # The whole-turn member execution window travels beside the legacy rollup
    # rather than inside it: it is a different measurement with its own
    # endpoint, and folding it into this dict would make the shared-member
    # guard's terms ambiguous.
    if telemetry.turn_resources is not None:
        resources["turn_resources"] = telemetry.turn_resources
    return resources


@dataclass(frozen=True)
class _TerminalEvent:
    """One resumable event in a terminal's bounded output ring."""

    seq: int
    kind: str
    data: bytes = b""
    exit_code: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _TerminalLimits:
    """A consistent snapshot of terminal buffering and viewer limits."""

    buffer_bytes: int = MAX_TERMINAL_BUFFER_BYTES
    buffer_events: int = MAX_TERMINAL_BUFFER_EVENTS
    output_bytes: int = MAX_TERMINAL_TOTAL_OUTPUT_BYTES
    streams: int = MAX_TERMINAL_STREAMS_PER_SESSION

    @classmethod
    def from_settings(cls, settings: dict[str, float]) -> _TerminalLimits:
        """Translate the shared admin values into terminal implementation units."""
        return cls(
            buffer_bytes=int(settings["terminal_buffer_max_mib"]) * 1024 * 1024,
            buffer_events=int(settings["terminal_buffer_max_events"]),
            output_bytes=int(settings["terminal_output_max_mib"]) * 1024 * 1024,
            streams=int(settings["terminal_max_streams_per_session"]),
        )


class _TerminalSession:
    """Thread-safe state and output history for one terminal process."""

    def __init__(
        self,
        *,
        terminal_id: str,
        process: TerminalProcess,
        workdir: Path | None,
        rows: int,
        cols: int,
        now: float,
        idle_ttl_s: float | None = None,
        on_closed: Callable[[float], None] | None = None,
        limits: _TerminalLimits | None = None,
    ) -> None:
        self.id = terminal_id
        self.shell = getattr(process, "shell", "sh")
        self.cwd = "/workspace"
        self.rows = rows
        self.cols = cols
        self.created_at = now
        self.last_active_at = now
        self._idle_ttl_s = idle_ttl_s
        self._limits = limits or _TerminalLimits()
        self.state = "running"
        self._process = process
        self._workdir = workdir
        self._on_closed = on_closed
        self._activity_released = False
        self._events: deque[_TerminalEvent] = deque()
        self._buffer_bytes = 0
        self._last_seq = 0
        self._condition = threading.Condition(threading.RLock())
        self._close_lock = threading.Lock()
        self._closed = False
        self._suspended = False
        self._total_output_bytes = 0
        self._active_streams = 0
        self._async_waiters: set[asyncio.Future[None]] = set()
        self._pump_thread = threading.Thread(
            target=self._pump,
            daemon=True,
            name=f"workspace-terminal-{terminal_id[-8:]}",
        )

    def start(self) -> None:
        """Start draining output immediately so the PTY can never back up."""
        self._pump_thread.start()

    def descriptor(self) -> dict[str, Any]:
        """Return the public, non-secret terminal state."""
        with self._condition:
            return {
                "id": self.id,
                "shell": self.shell,
                "state": self.state,
                "cwd": self.cwd,
                "rows": self.rows,
                "cols": self.cols,
                "last_seq": self._last_seq,
            }

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def holds_process(self) -> bool:
        """Whether this session still owns a process, and with it a terminal slot.

        An exited shell keeps its output for replay until it is closed or
        reaped, but runs nothing, so it never refuses a new terminal: the
        oldest exited shells are unlisted to make room instead.
        """
        with self._condition:
            return not self._closed and self.state != "exited"

    @property
    def has_exited(self) -> bool:
        """Whether the shell ended on its own and the session is still listed."""
        with self._condition:
            return not self._closed and self.state == "exited"

    def expired(self, *, now: float, idle_ttl_s: float, hard_ttl_s: float) -> bool:
        """Whether idle or absolute lifetime requires this session to close."""
        with self._condition:
            effective_idle_ttl_s = (
                min(idle_ttl_s, self._idle_ttl_s) if self._idle_ttl_s is not None else idle_ttl_s
            )
            return not self._closed and (
                self.state == "cleanup_failed"
                or now - self.last_active_at >= effective_idle_ttl_s
                or now - self.created_at >= hard_ttl_s
            )

    def write(self, data: bytes, *, now: float) -> dict[str, Any]:
        """Write terminal input, rejecting writes after exit/close."""
        with self._condition:
            if self.state != "running" or self._closed or self._suspended:
                raise HTTPException(status_code=409, detail="terminal is not running")
            self.last_active_at = now
            try:
                self._process.write(data)
            except SandboxError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        self._touch_workdir()
        return self.descriptor()

    def _touch_workdir(self) -> None:
        if self._workdir is not None:
            with contextlib.suppress(OSError):
                os.utime(self._workdir, None)

    def _release_activity(self) -> None:
        callback: Callable[[float], None] | None = None
        last_activity_at = 0.0
        with self._condition:
            if not self._activity_released:
                self._activity_released = True
                callback = self._on_closed
                last_activity_at = self.last_active_at
        if callback is not None:
            try:
                callback(last_activity_at)
            except Exception:
                logger.exception("workspace_terminal_activity_release_failed")

    def suspend(self) -> None:
        """Freeze the process tree, serialized against accepted input."""
        with self._condition:
            if self.state != "running" or self._closed or self._suspended:
                return
            self._suspended = True
            try:
                self._process.suspend()
            except (SandboxError, OSError):
                self._suspended = False
                raise

    def resume(self) -> None:
        """Resume a process tree frozen for workspace preparation or capture."""
        with self._condition:
            if self.state != "running" or self._closed or not self._suspended:
                return
            try:
                self._process.resume()
            except (SandboxError, OSError):
                # A backend may report failure after a partially effective
                # CONT. Best-effort STOP it again before preserving the
                # logical suspension; callers must never treat an uncertain
                # resume as an open publication/preparation barrier.
                with contextlib.suppress(SandboxError, OSError):
                    self._process.suspend()
                raise
            self._suspended = False

    def resize(self, *, rows: int, cols: int, now: float) -> dict[str, Any]:
        """Resize a running terminal and persist the accepted dimensions."""
        with self._condition:
            if self.state != "running" or self._closed:
                raise HTTPException(status_code=409, detail="terminal is not running")
            self.last_active_at = now
        try:
            self._process.resize(rows, cols)
        except SandboxError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        with self._condition:
            self.rows = rows
            self.cols = cols
        return self.descriptor()

    def close(self, *, mark_activity: bool = True) -> dict[str, Any]:
        """Close the process tree, optionally treating the close as activity."""
        with self._close_lock:
            with self._condition:
                if self._closed:
                    return self.descriptor()
                if mark_activity:
                    self.last_active_at = time.monotonic()
                self.state = "closing"
                self._condition.notify_all()
                self._wake_async_waiters_locked()
            try:
                self._process.kill()
            except (SandboxError, OSError):
                with self._condition:
                    self.state = "cleanup_failed"
                    self._condition.notify_all()
                    self._wake_async_waiters_locked()
                raise
            with self._condition:
                self._closed = True
                self.state = "closed"
                self._condition.notify_all()
                self._wake_async_waiters_locked()
            self._release_activity()
            if threading.current_thread() is not self._pump_thread:
                self._pump_thread.join(timeout=5)
            return self.descriptor()

    def events_after(self, after: int, *, wait_s: float) -> list[_TerminalEvent]:
        """Return resumable events, waiting once when no newer event exists."""
        with self._condition:
            events = [event for event in self._events if event.seq > after]
            if not events and self.state == "running" and not self._closed:
                self._condition.wait(timeout=wait_s)
                events = [event for event in self._events if event.seq > after]
            # Reading buffered events (and, in particular, an SSE keepalive)
            # is not terminal activity. Input, resize, process output, and
            # process exit update the clock at their actual source. Treating a
            # passive viewer as activity would let an abandoned browser tab
            # keep both the PTY and its retained chat container alive forever.
            return events

    async def events_after_async(self, after: int, *, wait_s: float) -> list[_TerminalEvent]:
        """Wait for output without occupying Starlette's shared thread pool."""
        loop = asyncio.get_running_loop()
        with self._condition:
            events = [item for item in self._events if item.seq > after]
            if events or self.state != "running" or self._closed:
                return events
            waiter = loop.create_future()
            self._async_waiters.add(waiter)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter, timeout=wait_s)
        finally:
            with self._condition:
                self._async_waiters.discard(waiter)
        with self._condition:
            return [item for item in self._events if item.seq > after]

    def open_stream(self) -> None:
        """Bound duplicate viewers of one PTY before starting an SSE response."""
        with self._condition:
            if self._active_streams >= self._limits.streams:
                raise HTTPException(status_code=429, detail="terminal stream limit reached")
            self._active_streams += 1

    def configure_limits(self, limits: _TerminalLimits) -> None:
        """Apply new output limits and trim existing history immediately."""
        with self._condition:
            self._limits = limits
            self._trim_events_locked()
            self._condition.notify_all()
            self._wake_async_waiters_locked()

    def close_stream(self) -> None:
        """Release one SSE viewer slot."""
        with self._condition:
            self._active_streams = max(0, self._active_streams - 1)

    def _wake_async_waiters_locked(self) -> None:
        """Wake event-loop waiters from the PTY pump or cleanup threads."""
        waiters = tuple(self._async_waiters)
        self._async_waiters.clear()
        for waiter in waiters:
            loop = waiter.get_loop()

            def resolve(target: asyncio.Future[None] = waiter) -> None:
                if not target.done():
                    target.set_result(None)

            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(resolve)

    def _append_output(self, data: bytes) -> None:
        if not data:
            return
        with self._condition:
            chunk_truncated = len(data) > self._limits.buffer_bytes
            if chunk_truncated:
                data = data[-self._limits.buffer_bytes :]
            if chunk_truncated:
                # The retained tail is not a complete output event. Give the
                # reset its own cursor so reconnecting after it resumes at the
                # retained output instead of receiving the reset forever.
                self._events.clear()
                self._buffer_bytes = 0
                self._last_seq += 1
                self._events.append(
                    _TerminalEvent(
                        self._last_seq,
                        "reset",
                        reason="output_truncated",
                    )
                )
            self._last_seq += 1
            self._events.append(_TerminalEvent(self._last_seq, "output", data=data))
            self._buffer_bytes += len(data)
            self._trim_events_locked()
            self.last_active_at = time.monotonic()
            self._condition.notify_all()
            self._wake_async_waiters_locked()

    def _append_exit(self, exit_code: int) -> None:
        with self._condition:
            self._last_seq += 1
            self._events.append(_TerminalEvent(self._last_seq, "exit", exit_code=exit_code))
            self._trim_events_locked()
            if not self._closed and self.state == "running":
                self.state = "exited"
                self.last_active_at = time.monotonic()
            self._condition.notify_all()
            self._wake_async_waiters_locked()

    def _trim_events_locked(self) -> None:
        """Bound the ring while preserving an explicit resumability gap marker."""
        reset_seq: int | None = None
        if self._events and self._events[0].kind == "reset":
            reset_seq = self._events.popleft().seq
        while self._events and (
            self._buffer_bytes > self._limits.buffer_bytes
            or len(self._events) + (reset_seq is not None) > self._limits.buffer_events
        ):
            oldest = self._events.popleft()
            self._buffer_bytes -= len(oldest.data)
            reset_seq = oldest.seq
        if reset_seq is not None:
            self._events.appendleft(_TerminalEvent(reset_seq, "reset", reason="output_truncated"))

    def _pump(self) -> None:
        """Continuously drain the PTY and publish one terminal exit event."""
        try:
            for chunk in self._process.chunks():
                with self._condition:
                    self._total_output_bytes += len(chunk)
                    output_exceeded = self._total_output_bytes > self._limits.output_bytes
                if output_exceeded:
                    self._append_output(
                        b"\r\n\x1b[31m[terminal stopped: output limit exceeded]\x1b[0m\r\n"
                    )
                    try:
                        self._process.kill()
                    except (SandboxError, OSError):
                        with self._condition:
                            self.state = "cleanup_failed"
                            self._condition.notify_all()
                            self._wake_async_waiters_locked()
                    break
                self._append_output(chunk)
                self._touch_workdir()
        except Exception:
            logger.exception("workspace_terminal_output_failed")
            try:
                self._process.kill()
            except Exception:
                logger.exception("workspace_terminal_output_cleanup_failed")
                with self._condition:
                    self.state = "cleanup_failed"
                    self._condition.notify_all()
                    self._wake_async_waiters_locked()
        try:
            exit_code = self._process.wait()
        except Exception:
            logger.exception("workspace_terminal_wait_failed")
            exit_code = 1
        self._append_exit(exit_code)
        self._release_activity()


class _TerminalManager:
    """Ephemeral registry of terminals inside retained chat containers."""

    def __init__(
        self,
        *,
        idle_ttl_s: float,
        hard_ttl_s: float,
        max_sessions: int,
        max_sessions_per_workspace: int = MAX_TERMINAL_SESSIONS_PER_WORKSPACE,
        limits: _TerminalLimits | None = None,
    ) -> None:
        self._idle_ttl_s = idle_ttl_s
        self._hard_ttl_s = hard_ttl_s
        self._max_sessions = max_sessions
        self._max_sessions_per_workspace = max_sessions_per_workspace
        self._limits = limits or _TerminalLimits()
        self._sessions: dict[str, dict[str, _TerminalSession]] = {}
        self._suspended_workspaces: set[str] = set()
        self._workspace_suspensions: dict[str, set[str]] = {}
        self._workspace_generations: dict[str, int] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def configure(
        self,
        *,
        idle_ttl_s: float,
        hard_ttl_s: float,
        max_sessions: int,
        max_sessions_per_workspace: int | None = None,
        limits: _TerminalLimits | None = None,
    ) -> None:
        """Change the limits at runtime, as the admin page asks.

        Open terminals are judged by the new lifetimes from the next sweep, so
        a shortened one closes those already past it. A lower session limit
        refuses new terminals and closes none.
        """
        if idle_ttl_s <= 0 or hard_ttl_s <= 0:
            raise ValueError("terminal TTL values must be positive")
        if not 1 <= max_sessions <= 4096:
            raise ValueError("terminal global session limit must be between 1 and 4096")
        if max_sessions_per_workspace is not None and not (
            1 <= max_sessions_per_workspace <= MAX_TERMINAL_SESSIONS_PER_WORKSPACE
        ):
            raise ValueError("terminal workspace session limit must be between 1 and 4")
        with self._lock:
            self._idle_ttl_s = idle_ttl_s
            self._hard_ttl_s = hard_ttl_s
            self._max_sessions = max_sessions
            if max_sessions_per_workspace is not None:
                self._max_sessions_per_workspace = max_sessions_per_workspace
            if limits is not None:
                self._limits = limits
                for group in self._sessions.values():
                    for session in group.values():
                        session.configure_limits(limits)

    @property
    def max_sessions_per_workspace(self) -> int:
        """Advertise the current creation limit to workspace clients."""
        with self._lock:
            return self._max_sessions_per_workspace

    def start(self) -> None:
        """Start periodic TTL cleanup once the app lifespan begins."""
        with self._lock:
            if self._reaper is not None:
                return
            self._reaper = threading.Thread(
                target=self._reap_loop,
                daemon=True,
                name="workspace-terminal-reaper",
            )
            self._reaper.start()

    def close(self) -> None:
        """Stop cleanup and close every live terminal transport."""
        self._stop.set()
        with self._lock:
            sessions = [session for group in self._sessions.values() for session in group.values()]
            self._sessions.clear()
            self._suspended_workspaces.clear()
            self._workspace_suspensions.clear()
            self._workspace_generations.clear()
        for session in sessions:
            try:
                session.close(mark_activity=False)
            except (SandboxError, OSError):
                logger.exception("workspace_terminal_shutdown_cleanup_failed")
        if self._reaper is not None:
            self._reaper.join(timeout=5)

    def create(
        self,
        workspace_id: str,
        workdir: Path | str,
        *,
        rows: int,
        cols: int,
        backend: SandboxBackend,
        idle_ttl_s: float | None = None,
        on_closed: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """Create one PTY while atomically enforcing the per-workspace cap."""
        self.reap()
        terminal_id = "term_" + secrets.token_urlsafe(18)
        now = time.monotonic()
        evicted: list[_TerminalSession] = []
        try:
            with self._lock:
                if workspace_id in self._suspended_workspaces:
                    raise HTTPException(status_code=409, detail="terminal workspace is not ready")
                group = self._sessions.setdefault(workspace_id, {})
                global_active = sum(
                    session.holds_process
                    for sessions in self._sessions.values()
                    for session in sessions.values()
                )
                if global_active >= self._max_sessions:
                    if not group:
                        self._sessions.pop(workspace_id, None)
                    raise HTTPException(status_code=503, detail="broker terminal limit reached")
                active = sum(session.holds_process for session in group.values())
                if active >= self._max_sessions_per_workspace:
                    raise HTTPException(status_code=409, detail="workspace terminal limit reached")
                spec = SandboxSpec(
                    argv=["/bin/sh"],
                    workdir=str(workdir),
                    env={
                        **backend.base_env(),
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "TERM": "xterm-256color",
                    },
                )
                try:
                    process = backend.spawn_terminal(spec, rows=rows, cols=cols)
                except TerminalNotSupportedError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Interactive terminals require the container or kata sandbox backend."
                        ),
                    ) from exc
                except (OSError, SandboxError) as exc:
                    raise HTTPException(
                        status_code=502, detail="terminal could not be started"
                    ) from exc
                session = _TerminalSession(
                    terminal_id=terminal_id,
                    process=process,
                    workdir=workdir if isinstance(workdir, Path) else None,
                    rows=rows,
                    cols=cols,
                    now=now,
                    idle_ttl_s=idle_ttl_s,
                    on_closed=on_closed,
                    limits=self._limits,
                )
                evicted.extend(self._unlist_exited_locked(workspace_id))
                group[terminal_id] = session
        finally:
            for stale in evicted:
                try:
                    stale.close(mark_activity=False)
                except (SandboxError, OSError):
                    logger.exception("workspace_terminal_eviction_cleanup_failed")
        session.start()
        return session.descriptor()

    def _unlist_exited_locked(self, workspace_id: str) -> list[_TerminalSession]:
        """Unlist the oldest exited shells so one more terminal fits both limits.

        An exited shell stays listed for its output and keeps up to a full
        output buffer, so it still takes room under both limits, but only a
        running shell can refuse a new terminal. The new terminal's workspace
        gives up its own oldest exited shells while its list is full; then,
        while the broker as a whole is full, the oldest anywhere go. The
        caller closes the returned sessions once the lock is released.
        """
        group = self._sessions[workspace_id]
        exited = sorted(
            (
                (owner, session)
                for owner, sessions in self._sessions.items()
                for session in sessions.values()
                if session.has_exited
            ),
            key=lambda candidate: candidate[1].created_at,
        )
        unlisted: list[_TerminalSession] = []

        def unlist(candidate: tuple[str, _TerminalSession]) -> None:
            owner, session = candidate
            sessions = self._sessions[owner]
            unlisted.append(sessions.pop(session.id))
            # The new terminal's own group is about to receive it.
            if not sessions and owner != workspace_id:
                self._sessions.pop(owner)

        own = [candidate for candidate in exited if candidate[0] == workspace_id]
        while own and len(group) >= self._max_sessions_per_workspace:
            candidate = own.pop(0)
            exited.remove(candidate)
            unlist(candidate)
        retained = sum(
            not session.is_closed
            for sessions in self._sessions.values()
            for session in sessions.values()
        )
        for candidate in exited[: max(0, retained + 1 - self._max_sessions)]:
            unlist(candidate)
        return unlisted

    @staticmethod
    def _suspension_key(job_id: str | None) -> str:
        """Map the rolling-compatibility request onto one reserved token."""
        return job_id or "__legacy_control_plane__"

    def suspend(
        self,
        workspace_id: str,
        *,
        lease_generation: int,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Freeze PTYs and retain one independent token per protected turn."""
        with self._lock:
            if job_id is None:
                # Previous control planes did not identify the turn. Preserve
                # their monotonic check only for that compatibility token;
                # current sibling turns must not fence one another by their
                # unrelated, per-job lease generations.
                current_generation = self._workspace_generations.get(workspace_id, 0)
                if lease_generation < current_generation:
                    raise HTTPException(status_code=409, detail="stale terminal workspace fence")
                self._workspace_generations[workspace_id] = lease_generation
            key = self._suspension_key(job_id)
            self._workspace_suspensions.setdefault(workspace_id, set()).add(key)
            self._suspended_workspaces.add(workspace_id)
            sessions = list(self._sessions.get(workspace_id, {}).values())
            try:
                for session in sessions:
                    session.suspend()
            except (SandboxError, OSError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="terminal suspension could not be confirmed; retry suspension",
                ) from exc
        return {"ok": True, "suspended": len(sessions)}

    def resume(
        self,
        workspace_id: str,
        *,
        lease_generation: int,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Release one turn token and resume PTYs only after the last release."""
        with self._lock:
            if job_id is None:
                current_generation = self._workspace_generations.get(workspace_id, 0)
                if lease_generation < current_generation:
                    raise HTTPException(status_code=409, detail="stale terminal workspace fence")
                self._workspace_generations[workspace_id] = lease_generation
            key = self._suspension_key(job_id)
            suspensions = self._workspace_suspensions.get(workspace_id)
            if not suspensions or key not in suspensions:
                return {"ok": True, "resumed": 0}
            suspensions.remove(key)
            if suspensions:
                return {"ok": True, "resumed": 0}
            sessions = list(self._sessions.get(workspace_id, {}).values())
            attempted: list[_TerminalSession] = []
            try:
                for session in sessions:
                    attempted.append(session)
                    session.resume()
            except (SandboxError, OSError) as exc:
                # Restore every PTY already resumed in this batch. A retry
                # must see both the logical workspace fence and the actual
                # processes suspended; otherwise one shell could mutate the
                # shared tree inside a quiescent capture after a sibling's
                # resume failed.
                for session in attempted:
                    with contextlib.suppress(SandboxError, OSError):
                        session.suspend()
                suspensions.add(key)
                raise HTTPException(
                    status_code=503,
                    detail="terminal resume could not be confirmed; retry resume",
                ) from exc
            self._workspace_suspensions.pop(workspace_id, None)
            self._suspended_workspaces.discard(workspace_id)
        return {"ok": True, "resumed": len(sessions)}

    def resume_settled(
        self,
        workspace_id: str,
        *,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Release one settled turn without disturbing a concurrent sibling."""
        # Generation is irrelevant for a terminal job. The reserved legacy
        # token is the only state an old control plane may release; a new
        # request names its job and therefore cannot thaw another member.
        with self._lock:
            legacy_generation = max(1, self._workspace_generations.get(workspace_id, 0))
            result = self.resume(
                workspace_id,
                lease_generation=legacy_generation,
                job_id=job_id,
            )
            if job_id is None:
                # Lease generations restart at one for the next platform job.
                self._workspace_generations.pop(workspace_id, None)
            return result

    def list(self, workspace_id: str) -> list[dict[str, Any]]:
        """List visible terminals for one workspace only."""
        self.reap()
        with self._lock:
            sessions = list(self._sessions.get(workspace_id, {}).values())
        return [session.descriptor() for session in sessions if not session.is_closed]

    def get(self, workspace_id: str, terminal_id: str) -> _TerminalSession:
        """Resolve an opaque id inside its workspace namespace."""
        self.reap()
        with self._lock:
            session = self._sessions.get(workspace_id, {}).get(terminal_id)
        if session is None or session.is_closed:
            raise HTTPException(status_code=404, detail="terminal does not exist")
        return session

    def get_for_control(self, workspace_id: str, terminal_id: str) -> _TerminalSession:
        """Resolve a terminal only while its workspace-level fence is open."""
        self.reap()
        with self._lock:
            session = self._sessions.get(workspace_id, {}).get(terminal_id)
            if session is None or session.is_closed:
                raise HTTPException(status_code=404, detail="terminal does not exist")
            if workspace_id in self._suspended_workspaces:
                raise HTTPException(status_code=409, detail="terminal workspace is not ready")
            return session

    def delete(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Close a terminal, retaining it when cleanup needs to be retried."""
        with self._lock:
            group = self._sessions.get(workspace_id, {})
            session = group.get(terminal_id)
        if session is None:
            return {
                "id": terminal_id,
                "shell": "sh",
                "state": "closed",
                "cwd": "/workspace",
                "rows": 24,
                "cols": 80,
                "last_seq": 0,
            }
        try:
            descriptor = session.close()
        except (SandboxError, OSError) as exc:
            raise HTTPException(
                status_code=503,
                detail="terminal cleanup could not be confirmed; retry kill",
            ) from exc
        with self._lock:
            remaining = self._sessions.get(workspace_id)
            if remaining is not None and remaining.get(terminal_id) is session:
                remaining.pop(terminal_id, None)
                if not remaining:
                    self._sessions.pop(workspace_id, None)
        return descriptor

    def reap(self) -> None:
        """Close sessions that exceeded either configured lifetime."""
        now = time.monotonic()
        with self._lock:
            expired = [
                (workspace_id, terminal_id, session)
                for workspace_id, group in self._sessions.items()
                for terminal_id, session in group.items()
                if session.expired(
                    now=now,
                    idle_ttl_s=self._idle_ttl_s,
                    hard_ttl_s=self._hard_ttl_s,
                )
            ]
        for workspace_id, terminal_id, session in expired:
            try:
                session.close(mark_activity=False)
            except (SandboxError, OSError):
                logger.exception("workspace_terminal_reap_cleanup_failed")
                continue
            with self._lock:
                group = self._sessions.get(workspace_id)
                if group is not None and group.get(terminal_id) is session:
                    group.pop(terminal_id, None)
                    if not group:
                        self._sessions.pop(workspace_id, None)

    def _reap_loop(self) -> None:
        # Recomputed each pass: the lifetimes can change at runtime, and an
        # interval fixed at start would sweep a shortened one late.
        while not self._stop.wait(self._reap_interval()):
            self.reap()

    def _reap_interval(self) -> float:
        with self._lock:
            return max(0.1, min(30.0, self._idle_ttl_s / 2, self._hard_ttl_s / 2))


def _relative_path(raw: str | None) -> str:
    try:
        return normalize_workspace_path(raw)
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_sandbox(
    backend: SandboxBackend,
    *,
    workdir: Path | str,
    script: str,
    env: dict[str, str],
    timeout_s: float,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> tuple[int, str, str]:
    """Run a bounded command and return status, stdout and stderr."""
    process = backend.spawn(
        SandboxSpec(
            argv=["/bin/sh", "-lc", script],
            workdir=str(workdir),
            env={**backend.base_env(), "GIT_CONFIG_NOSYSTEM": "1", **env},
        )
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            for line in process.lines():
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True, name="workspace-command").start()
    deadline = time.monotonic() + timeout_s
    chunks: list[str] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            process.kill()
            raise HTTPException(status_code=408, detail="workspace command timed out")
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            break
        size += len(line.encode("utf-8", errors="replace"))
        if size > max_output_bytes:
            process.kill()
            raise HTTPException(status_code=413, detail="workspace command output is too large")
        chunks.append(line)
    exit_code = process.wait()
    stderr = process.stderr_text()
    if len(stderr.encode("utf-8", errors="replace")) > max_output_bytes:
        stderr = stderr.encode("utf-8", errors="replace")[-max_output_bytes:].decode(
            "utf-8", errors="replace"
        )
    if isinstance(workdir, Path):
        os.utime(workdir, None)
    return exit_code, "".join(chunks), stderr


def _run_terminal(
    backend: SandboxBackend,
    workdir: Path | str,
    request: TerminalRequest,
    *,
    default_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> dict[str, Any]:
    marker = secrets.token_hex(16)
    script = (
        "umask 0002; "
        'cd "$WORKSPACE_CWD" 2>/dev/null || { printf "invalid working directory\\n" >&2; '
        "exit 125; }; "
        'eval "$WORKSPACE_COMMAND"; status=$?; '
        f'printf "\\n__HYI_{marker}_CWD__%s\\n__HYI_{marker}_STATUS__%s\\n" "$PWD" "$status"'
    )
    process_status, stdout, stderr = _run_sandbox(
        backend,
        workdir=workdir,
        script=script,
        env={"WORKSPACE_COMMAND": request.command, "WORKSPACE_CWD": request.cwd},
        timeout_s=(
            request.timeout_seconds
            if "timeout_seconds" in request.model_fields_set
            else default_timeout_s
        ),
        max_output_bytes=max_output_bytes,
    )
    cwd_marker = f"\n__HYI_{marker}_CWD__"
    status_marker = f"\n__HYI_{marker}_STATUS__"
    cwd = request.cwd
    status = process_status
    marker_index = stdout.rfind(cwd_marker)
    if marker_index >= 0:
        trailer = stdout[marker_index + len(cwd_marker) :]
        stdout = stdout[:marker_index]
        cwd_line, separator, status_text = trailer.partition(status_marker)
        if separator:
            cwd = cwd_line.strip() or request.cwd
            try:
                status = int(status_text.strip().splitlines()[0])
            except (ValueError, IndexError):
                status = process_status
    return {"output": stdout, "stderr": stderr, "exit_code": status, "cwd": cwd}


def _run_git(
    backend: SandboxBackend,
    workdir: Path | str,
    base_sha: str | None = None,
    *,
    timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> dict[str, Any]:
    if base_sha and not _COMMIT_SHA.fullmatch(base_sha):
        raise HTTPException(status_code=400, detail="base SHA is not a commit hash")
    target = base_sha or "HEAD"
    marker = secrets.token_hex(16)
    status_tag = f"__HYI_{marker}_STATUS__"
    diff_tag = f"__HYI_{marker}_DIFF__"
    commits_tag = f"__HYI_{marker}_COMMITS__"
    script = (
        "git --no-pager status --porcelain=v1 --branch; status_status=$?; "
        f"printf '\n{diff_tag}\n'; "
        'index_path="${TMPDIR:-/tmp}/hyi-browser-index.$$"; '
        'rm -f "$index_path"; trap \'rm -f "$index_path"\' EXIT; '
        f'GIT_INDEX_FILE="$index_path" git read-tree {target} && '
        'GIT_INDEX_FILE="$index_path" git add -A -N >/dev/null 2>&1 && '
        f'GIT_INDEX_FILE="$index_path" git --no-pager diff '
        f"--no-ext-diff --no-textconv --binary {target}; diff_status=$?; "
        f"printf '\n{commits_tag}\n'; "
        "git --no-pager log -20 --pretty=format:'%H%x09%h%x09%s%x09%an%x09%aI'; "
        "log_status=$?; overall_status=$status_status; "
        '[ "$overall_status" -eq 0 ] && overall_status=$diff_status; '
        '[ "$overall_status" -eq 0 ] && overall_status=$log_status; '
        f"printf '\n{status_tag}%s\n' \"$overall_status\""
    )
    process_status, stdout, stderr = _run_sandbox(
        backend,
        workdir=workdir,
        script=script,
        env={},
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    status_text, separator, remainder = stdout.partition(f"\n{diff_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    patch, separator, commits_and_status = remainder.partition(f"\n{commits_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    commits_text, _, git_status_text = commits_and_status.rpartition(f"\n{status_tag}")
    try:
        git_status = int(git_status_text.strip().splitlines()[0])
    except (ValueError, IndexError):
        git_status = process_status
    if git_status != 0:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")

    status_lines = [line for line in status_text.splitlines() if line]
    branch = ""
    if status_lines and status_lines[0].startswith("## "):
        branch = status_lines.pop(0)[3:].split("...", 1)[0]
    changes = [{"code": line[:2], "path": line[3:]} for line in status_lines if len(line) >= 4]
    commits = []
    for line in commits_text.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            commits.append(
                {
                    "sha": parts[0],
                    "short_sha": parts[1],
                    "subject": parts[2],
                    "author": parts[3],
                    "authored_at": parts[4],
                }
            )
    return {
        "available": True,
        "branch": branch,
        "changes": changes,
        "patch": patch,
        "commits": commits,
    }


def _terminal_sse_frame(event: _TerminalEvent) -> str:
    """Serialize one terminal event using the gateway's stable SSE contract."""
    if event.kind == "output":
        payload = {
            "seq": event.seq,
            "data": base64.b64encode(event.data).decode("ascii"),
        }
    elif event.kind == "reset":
        payload = {"seq": event.seq, "reason": event.reason}
    else:
        payload = {"seq": event.seq, "exit_code": event.exit_code}
    data = json.dumps(payload, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n"


def _terminal_attached_frame(last_seq: int) -> str:
    """Tell a new viewer where the terminal's history ends.

    Events up to ``last_seq`` were produced before the viewer attached. A fresh
    screen replaying them must not answer the terminal queries among them --
    the program that asked is long past waiting, or gone -- or the answers
    reach the shell as typed input. It is not a resumable event, so it has no
    id; a client that does not know it ignores it.
    """
    data = json.dumps({"seq": last_seq}, separators=(",", ":"))
    return f"event: attached\ndata: {data}\n\n"


def create_app(
    *,
    backend: SandboxBackend | None = None,
    token: str | None = None,
    terminal_idle_ttl_s: float | None = None,
    terminal_hard_ttl_s: float | None = None,
    terminal_max_sessions: int | None = None,
    shared_session_supervisor: SharedChatSessionSupervisor | None = None,
    sandbox_archive_service: SandboxArchiveService | None = None,
    sandbox_archive_controller: SandboxArchiveController | None = None,
    session_idle_timeout_s: float | None = None,
) -> FastAPI:
    """Create the internal broker app with injectable dependencies for tests."""
    sandbox = backend or build_backend_from_env()
    expected_token = (
        token if token is not None else os.environ.get("AGENT_WORKSPACE_BROKER_TOKEN", "")
    )
    idle_ttl_s = (
        terminal_idle_ttl_s
        if terminal_idle_ttl_s is not None
        else float(os.environ.get("AGENT_TERMINAL_IDLE_TTL_SECONDS", DEFAULT_TERMINAL_IDLE_TTL_S))
    )
    hard_ttl_s = (
        terminal_hard_ttl_s
        if terminal_hard_ttl_s is not None
        else float(os.environ.get("AGENT_TERMINAL_HARD_TTL_SECONDS", DEFAULT_TERMINAL_HARD_TTL_S))
    )
    max_sessions = (
        terminal_max_sessions
        if terminal_max_sessions is not None
        else int(os.environ.get("AGENT_TERMINAL_MAX_SESSIONS", DEFAULT_MAX_TERMINAL_SESSIONS))
    )
    if idle_ttl_s <= 0 or hard_ttl_s <= 0:
        raise ValueError("terminal TTL values must be positive")
    if not 1 <= max_sessions <= 4096:
        raise ValueError("terminal global session limit must be between 1 and 4096")
    chat_idle_timeout_s = (
        session_idle_timeout_s
        if session_idle_timeout_s is not None
        else _chat_idle_timeout_from_env()
    )
    if chat_idle_timeout_s <= 0:
        raise ValueError("chat sandbox idle timeout must be positive")
    shared_supervisor = shared_session_supervisor
    retained_manager = (
        ChatSandboxManager(sandbox) if isinstance(sandbox, ContainerBackend) else None
    )
    if sandbox_archive_service is not None and sandbox_archive_controller is not None:
        raise ValueError("sandbox archive service and controller are mutually exclusive")
    archive_service = sandbox_archive_service
    archive_controller = sandbox_archive_controller
    if archive_service is None and archive_controller is None and retained_manager is not None:
        archive_controller = SandboxArchiveController(
            retained_manager,
            namespace=retained_manager.backend.sandbox_namespace,
        )
    archive_boundary = archive_service or archive_controller
    if shared_supervisor is None:
        if retained_manager is None:
            raise ValueError("shared-live chat sandboxes require the container or kata backend")
        shared_supervisor = SharedChatSessionSupervisor(
            retained_manager,
            idle_timeout_s=chat_idle_timeout_s,
            stopped_archive_prepare_hook=(
                archive_controller.prepare_final if archive_controller is not None else None
            ),
            stopped_archive_invalidate_hook=(
                archive_controller.invalidate_final if archive_controller is not None else None
            ),
            starting_archive_prepare_hook=(
                archive_controller.prepare_start if archive_controller is not None else None
            ),
            started_archive_hook=(
                archive_controller.complete_start if archive_controller is not None else None
            ),
            markerless_final_prepare_hook=(
                archive_controller.prepare_markerless_final_if_disabled
                if archive_controller is not None
                else None
            ),
            stopped_archive_hook=(
                archive_boundary.queue_final if archive_boundary is not None else None
            ),
        )
    if archive_boundary is not None:
        configure_archive_hook = getattr(
            shared_supervisor,
            "configure_stopped_archive_hook",
            None,
        )
        configure_archive_prepare_hook = getattr(
            shared_supervisor,
            "configure_stopped_archive_prepare_hook",
            None,
        )
        configure_archive_transition_hooks = getattr(
            shared_supervisor,
            "configure_archive_transition_hooks",
            None,
        )
        final_archive_prepare_hook = getattr(archive_boundary, "prepare_final", None)
        final_archive_hook = getattr(archive_boundary, "queue_final", None)
        if not callable(configure_archive_hook) or not callable(final_archive_hook):
            raise ValueError("configured sandbox archives require a shared supervisor archive hook")
        if callable(final_archive_prepare_hook):
            if not callable(configure_archive_prepare_hook):
                raise ValueError(
                    "dynamic sandbox archives require a shared supervisor prepare hook"
                )
            configure_archive_prepare_hook(final_archive_prepare_hook)
        if archive_controller is not None:
            if not callable(configure_archive_transition_hooks):
                raise ValueError(
                    "dynamic sandbox archives require shared supervisor transition hooks"
                )
            configure_archive_transition_hooks(
                invalidate_final=archive_controller.invalidate_final,
                prepare_start=archive_controller.prepare_start,
                complete_start=archive_controller.complete_start,
                prepare_markerless_final=(archive_controller.prepare_markerless_final_if_disabled),
            )
        configure_archive_hook(final_archive_hook)
    terminals = _TerminalManager(
        idle_ttl_s=idle_ttl_s,
        hard_ttl_s=hard_ttl_s,
        max_sessions=max_sessions,
    )
    endpoint_calls = _EndpointLifecycleCalls()
    supervisor_start_lock = threading.Lock()
    supervisor_ready = threading.Event()

    def start_shared_supervisor() -> None:
        """Start reconciliation once, after dynamic archive policy is known."""
        with supervisor_start_lock:
            if supervisor_ready.is_set():
                return
            shared_supervisor.start()
            supervisor_ready.set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Docker owns every chat workspace and provider state. The broker keeps
        # only an in-memory index reconstructed from container labels/PID files.
        sandbox.preflight()
        expected_docker_root = (os.environ.get("AGENT_RETAINED_DOCKER_DATA_ROOT") or "").strip()
        if expected_docker_root:
            if retained_manager is None:
                raise ValueError("retained DockerRootDir validation requires a container backend")
            _app.state.retained_docker_data_root = retained_manager.require_docker_data_root(
                expected_docker_root
            )
        archive_boundary_started = False
        terminals_started = False
        resource_collector: SandboxResourceCollector | None = None
        try:
            # Reconcile/reap retained Docker execs before this process starts
            # accepting new terminal sessions. Keep startup inside the cleanup
            # guard: a failed Docker reconciliation must not leak the terminal
            # manager's background thread from a failed ASGI lifespan.
            if archive_boundary is not None:
                archive_boundary.start()
                archive_boundary_started = True
            # A dynamic controller begins at UNKNOWN. Reconciliation can stop
            # and release retained containers, so wait for the runner to
            # deliver an explicit enabled or disabled database policy first.
            if archive_controller is None:
                start_shared_supervisor()
            terminals.start()
            terminals_started = True
            resource_collector = SandboxResourceCollector()
            resource_collector.start()
            _app.state.sandbox_resource_collector = resource_collector
            yield
        finally:
            cleanup_errors: list[Exception] = []

            def cleanup(name: str, function: Callable[[], None]) -> None:
                try:
                    function()
                except Exception as exc:
                    logger.exception(
                        "workspace broker shutdown cleanup failed",
                        extra={"event": "workspace_broker_cleanup_failed", "component": name},
                    )
                    cleanup_errors.append(exc)

            # A canceled request still owns its executor call. Drain every
            # admitted mutation before closing supervisors or archive clients.
            await endpoint_calls.seal_and_wait()
            _app.state.sandbox_resource_collector = None
            if resource_collector is not None:
                cleanup("resource_collector", resource_collector.close)
            if terminals_started:
                cleanup("terminals", terminals.close)
            if supervisor_ready.is_set():
                archive_boundary_configured = (
                    archive_service is not None or archive_controller is not None
                )
                if archive_boundary_configured or shared_supervisor.has_pending_archive_work():
                    cleanup("shared_supervisor_quiesce", shared_supervisor.quiesce)
                    cleanup(
                        "shared_sandbox_stop_all",
                        lambda: shared_supervisor.stop_all_for_shutdown(
                            include_running=archive_boundary_configured
                        ),
                    )
                cleanup("shared_supervisor", shared_supervisor.close)
            if archive_boundary_started:
                assert archive_boundary is not None
                cleanup("sandbox_archive_service", archive_boundary.close)
            if cleanup_errors:
                raise RuntimeError("workspace broker shutdown cleanup failed") from cleanup_errors[
                    0
                ]

    app = FastAPI(title="Agent workspace broker", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.terminal_manager = terminals
    app.state.shared_session_supervisor = shared_supervisor
    app.state.sandbox_archive_service = archive_boundary
    app.state.sandbox_archive_controller = archive_controller
    app.state.sandbox_deletion_archive_service = SandboxDeletionArchiveService(shared_supervisor)
    app.state.sandbox_resource_collector = None

    async def runtime_refused(_request: Request, exc: Exception) -> JSONResponse:
        """Answer a runtime's deliberate refusal with the runtime's own reason.

        A runtime refuses a launch it cannot run as configured: MCP servers
        with no relay to reach them, or an effort its CLI cannot express. The
        message is written to be read, but these are not driver or sandbox
        errors, so no route mapped them. Each left as a plain-text 500, and
        the runner could only say the supervisor "rejected the lifecycle
        transition".
        """
        return JSONResponse(status_code=422, content={"detail": str(exc)[:300]})

    for refusal in (RuntimeMCPUnavailableError, RuntimeEffortUnsupportedError):
        app.add_exception_handler(refusal, runtime_refused)

    async def unexpected_failure(request: Request, exc: Exception) -> JSONResponse:
        """Answer a failure no route anticipated in a body both clients can read.

        Starlette's plain-text ``Internal Server Error`` has no ``detail``, so
        the cause never left this process. The exception's type and the route
        are enough to find it in this log; Starlette still re-raises after
        answering, so the traceback is logged here. The message stays here
        too: a broker failure can reach the owner's browser through the
        control plane, which shows only what is safe to show.
        """
        route = getattr(request.scope.get("route"), "path", None) or "an unknown route"
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    "workspace broker failed unexpectedly "
                    f"({type(exc).__name__} in {request.method} {route})"
                )
            },
        )

    app.add_exception_handler(Exception, unexpected_failure)

    # Until a policy arrives, this broker holds none, which reads as "not
    # collecting". That is what stops a broker that started before its runner
    # from briefly measuring for an experiment nobody told it about.
    broker_profiling_policy.install_policy_provider()

    def authenticate(x_agent_workspace_token: str | None = Header(None)) -> None:
        if (
            not expected_token
            or not x_agent_workspace_token
            or not secrets.compare_digest(expected_token, x_agent_workspace_token)
        ):
            raise HTTPException(status_code=401, detail="invalid workspace broker credential")

    @app.post("/profiling-policy")
    def profiling_policy(
        body: dict[str, Any],
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Accept the profiling policy this host's runner pushes.

        Reached over the broker's own shared-token channel and nowhere else.
        The broker holds no host credential and must not be given one -- the
        design keeps that out of the sandbox side entirely -- so the runner,
        which does hold one, is what tells it what the deployment is collecting
        for. Applying the policy through the same store the collector reads is
        what makes a fragment possible at all: a broker with no policy refuses
        every selection, however well-formed the header that carried it.
        """
        return broker_profiling_policy.apply_received_policy(body)

    # Legacy environment values are bootstrap observations only. The first
    # authenticated settings push replaces all of them with admin defaults.
    local_settings: dict[str, float] = {
        **_DEFAULT_SETTINGS,
        "terminal_idle_ttl_s": idle_ttl_s,
        "terminal_hard_ttl_s": hard_ttl_s,
        "terminal_max_sessions": max_sessions,
        "chat_idle_timeout_s": chat_idle_timeout_s,
    }
    for key in BROKER_SETTING_KEYS - {
        "terminal_idle_ttl_s",
        "terminal_hard_ttl_s",
        "terminal_max_sessions",
        "chat_idle_timeout_s",
    }:
        definition = HOST_SETTINGS_BY_KEY[key]
        raw = os.environ.get(definition.env)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if definition.accepts(value):
            local_settings[key] = value
    applied_settings = dict(local_settings)
    terminals.configure(
        idle_ttl_s=idle_ttl_s,
        hard_ttl_s=hard_ttl_s,
        max_sessions=max_sessions,
        max_sessions_per_workspace=int(applied_settings["terminal_max_sessions_per_workspace"]),
        limits=_TerminalLimits.from_settings(applied_settings),
    )

    @app.post("/host-settings")
    def host_settings(
        body: dict[str, Any],
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Apply the complete admin configuration, with defaults for missing keys.

        The runner sends them after every heartbeat, over the same
        shared-token channel the profiling policy uses: the broker holds no
        host credential and must not be given one. The whole set every time,
        so a cleared or unsupported value uses the shared built-in default.
        Legacy environment values cannot override a reset from the admin page.

        Answers with what it now applies and what its own would be, which the
        runner reports to the control plane as this host's confirmation.
        """
        nonlocal applied_settings
        admin = {
            **_DEFAULT_SETTINGS,
            **admin_values(body.get("settings"), BROKER_SETTING_KEYS),
        }
        applied = resolve(admin, local_settings)
        terminals.configure(
            idle_ttl_s=float(applied["terminal_idle_ttl_s"]),
            hard_ttl_s=float(applied["terminal_hard_ttl_s"]),
            max_sessions=int(applied["terminal_max_sessions"]),
            max_sessions_per_workspace=int(applied["terminal_max_sessions_per_workspace"]),
            limits=_TerminalLimits.from_settings(applied),
        )
        shared_supervisor.set_idle_timeout_s(float(applied["chat_idle_timeout_s"]))
        applied_settings = applied
        return {"applied": applied, "local": local_settings}

    @app.post("/sandbox-archive-settings")
    async def sandbox_archive_settings(
        body: dict[str, Any],
        _: None = Depends(authenticate),
    ) -> dict[str, object]:
        """Apply one complete DB policy and confirm its exact revision.

        This authenticated runner-to-broker channel never transports AWS
        credentials. The fixed destination and broker workload identity remain
        the only S3 authority.
        """
        if archive_controller is None:
            raise HTTPException(
                status_code=409,
                detail="workspace broker does not use dynamic sandbox archive settings",
            )
        if set(body) != {"settings"} or not isinstance(body.get("settings"), dict):
            raise HTTPException(
                status_code=422,
                detail="sandbox archive settings must be one complete object",
            )

        def apply_and_start() -> dict[str, object]:
            status = archive_controller.apply(body["settings"])
            # Apply first: reconciliation may immediately stop and archive an
            # orphaned retained container through this controller.
            start_shared_supervisor()
            return status

        try:
            return await endpoint_calls.run(apply_and_start)
        except (SandboxArchivePolicyError, SandboxArchiveConfigError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/health")
    def health(_: None = Depends(authenticate)) -> dict[str, Any]:
        return {
            "ok": True,
            "sandbox_archives": (
                {
                    **archive_service.status(),
                    "shutdown_candidates": (shared_supervisor.shutdown_archive_candidate_count()),
                }
                if archive_service is not None
                else (
                    {
                        **archive_controller.status(),
                        "supervisor_ready": supervisor_ready.is_set(),
                        "shutdown_candidates": (
                            shared_supervisor.shutdown_archive_candidate_count()
                            if supervisor_ready.is_set()
                            else 0
                        ),
                    }
                    if archive_controller is not None
                    else {"enabled": False}
                )
            ),
            "retained_docker_data_root": getattr(
                app.state,
                "retained_docker_data_root",
                None,
            ),
        }

    def require_shared_supervisor() -> SharedChatSessionSupervisor:
        if archive_controller is not None and not supervisor_ready.is_set():
            raise HTTPException(
                status_code=503,
                detail="sandbox archive policy has not been confirmed",
            )
        return shared_supervisor

    def supervisor_error(exc: Exception) -> HTTPException:
        if isinstance(exc, RetainedChatLost):
            return HTTPException(status_code=410, detail=str(exc))
        if isinstance(exc, SessionBusy):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, SessionLeaseLost):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=503, detail=str(exc)[:300])

    def retained_owner(workspace_id: str) -> SharedChatSessionSupervisor:
        """Resolve the only supported workspace shape: a shared chat member."""
        owner = require_shared_supervisor()
        if not workspace_id.startswith("athr_") or not owner.owns(workspace_id):
            raise HTTPException(status_code=404, detail="shared chat workspace is not materialized")
        return owner

    def retained_operation(
        workspace_id: str,
        operation: Callable[[RetainedChatBackend, BrokerCall], dict[str, Any]],
        *,
        call: BrokerCall | None = None,
    ) -> dict[str, Any]:
        """Open a workspace, run one operation, and answer its measurement call.

        ``call`` is this request's measurement fragment, if the inherited
        selection named one. It gates nothing: the workspace is admitted, its
        network phase is verified, and the operation runs exactly as before.
        Only the timings are conditional, and the answer is merged into the
        body so a caller can tell a producer that collected nothing from one
        that never reported at all.
        """
        active = call if call is not None else closed()
        owner = retained_owner(workspace_id)
        token: str | None = None
        try:
            with active.stage("admission"):
                token, _ = owner.operation_opened(workspace_id)
            retained = owner.backend(workspace_id)
            with active.stage("network"):
                retained.manager.require_network(retained.spec)
            return active.answer(operation(retained, active))
        except RetainedWorkspaceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        finally:
            if token is not None:
                with contextlib.suppress(SandboxError):
                    owner.operation_closed(workspace_id, token)

    def ensure_workspace(workspace_id: str) -> SharedSessionDescriptor:
        """Validate a member alias and return its shared-group descriptor."""
        owner = retained_owner(workspace_id)
        try:
            return owner.status(workspace_id)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc

    def terminal_scope(workspace_id: str) -> str:
        """Map any member URL onto the one terminal namespace for its group.

        The public/control-plane route stays chat-scoped so ownership is still
        checked against the addressed member.  The shell processes themselves
        operate on the group's single ``/workspace`` as the group UID, though,
        so capacity and suspension must be group-scoped too.  Otherwise a fork
        sibling could keep writing through a PTY while another member had
        installed the documented workspace preparation/publication barrier.
        """
        return str(ensure_workspace(workspace_id).origin_thread_id)

    def shared_spec(body: SharedSandboxIdentityRequest) -> ChatSandboxSpec:
        return ChatSandboxSpec(
            thread_id=body.origin_thread_id,
            sandbox_id=body.sandbox_id,
            generation=body.generation,
            runtime_id=body.runtime_id,
            runtime_version=body.runtime_version,
            sandbox_image_digest=body.sandbox_image_digest,
            memory_limit=body.memory_limit,
            cpu_limit=body.cpu_limit,
            disk_limit_mib=body.disk_limit_mib,
            stop_timeout_s=body.stop_timeout_s,
            shared_live=True,
            resident_slot_capacity=body.resident_slot_capacity,
        )

    def shared_manifest(
        members: list[SharedSandboxMemberRequest],
    ) -> list[SharedMemberManifest]:
        return [SharedMemberManifest(**member.model_dump()) for member in members]

    def resident_launch(
        thread_id: str,
        body: SharedResidentLaunchConfig,
    ) -> ResidentLaunch:
        gateway_base_url = (os.environ.get("AGENT_GATEWAY_URL") or body.gateway_base_url).strip()
        if not gateway_base_url:
            raise SandboxError("resident launch has no sandbox-reachable gateway URL")
        return ResidentLaunch(
            thread_id=thread_id,
            runtime_id=body.runtime_id,
            runtime_version=body.runtime_version,
            native_session_id=body.native_session_id,
            model=body.model,
            reasoning_effort=body.reasoning_effort,
            gateway_base_url=gateway_base_url,
            credential=body.credential,
            mcp_server_ids=tuple(body.mcp_server_ids),
            # Like the gateway's, the relay is this host's own, so the address
            # a CLI is told is the one the host rules admit for its group. Only
            # a launch with tools has one to name.
            mcp_base_url=(
                (os.environ.get("AGENT_SANDBOX_MCP_URL") or body.mcp_base_url).strip()
                if body.mcp_server_ids
                else ""
            ),
            mcp_credential=body.mcp_credential,
            slot_id=body.slot_id,
            residency_generation=body.residency_generation,
        )

    @app.post("/shared-sandboxes/{thread_id}/acquire")
    async def acquire_shared_chat_sandbox(
        thread_id: str,
        body: SharedSandboxAcquireRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            lease = await endpoint_calls.run(
                owner.acquire,
                shared_spec(body),
                thread_id=thread_id,
                owner=body.owner,
                activation_generation=body.activation_generation,
                has_settled_context=body.has_settled_context,
                manifest=shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
                profiling=body.profiling,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(lease)

    @app.post("/shared-sandboxes/{thread_id}/register")
    async def register_shared_chat_sandbox(
        thread_id: str,
        body: SharedSandboxIdentityRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Restore DB-owned member aliases without waking the container."""
        owner = require_shared_supervisor()
        if thread_id != body.origin_thread_id:
            raise HTTPException(status_code=422, detail="shared registration must name the origin")
        try:
            descriptors = await endpoint_calls.run(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return {
            "ok": True,
            "sandbox_id": body.sandbox_id,
            "member_count": len(descriptors),
        }

    @app.post("/shared-sandboxes/{thread_id}/archive-initial/status")
    async def shared_initial_archive_status(
        thread_id: str,
        body: SharedInitialArchiveStatusRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Resolve a crash-committed pre-prompt baseline before reset/setup."""
        owner = require_shared_supervisor()
        if thread_id != body.origin_thread_id:
            raise HTTPException(status_code=422, detail="initial archive must name the origin")
        if archive_boundary is None:
            return {"enabled": False, "prepared": False, "stored": False}
        try:
            return await endpoint_calls.run(
                owner.capture_initial_archive,
                thread_id,
                body.token,
                expected_spec=shared_spec(body),
                expected_container_id=body.expected_container_id,
                capture=lambda backend, _spec, members: archive_boundary.probe_initial(
                    backend,
                    expected_container_id=body.expected_container_id,
                    preparation_id=body.preparation_id,
                    members=members,
                ),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @app.post("/shared-sandboxes/{thread_id}/archive-initial")
    async def capture_shared_initial_archive(
        thread_id: str,
        body: SharedInitialArchiveRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Persist the exact prepared workspace before any resident/prompt."""
        owner = require_shared_supervisor()
        if thread_id != body.origin_thread_id:
            raise HTTPException(status_code=422, detail="initial archive must name the origin")
        if not _COMMIT_SHA.fullmatch(body.base_sha):
            raise HTTPException(status_code=422, detail="base SHA is invalid")
        if archive_boundary is None:
            return {"enabled": False, "stored": False}
        try:
            return await endpoint_calls.run(
                owner.capture_initial_archive,
                thread_id,
                body.token,
                expected_spec=shared_spec(body),
                expected_container_id=body.expected_container_id,
                capture=lambda backend, _spec, members: archive_boundary.capture_initial(
                    backend,
                    expected_container_id=body.expected_container_id,
                    preparation_id=body.preparation_id,
                    base_sha=body.base_sha,
                    members=members,
                ),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @app.post("/shared-sandboxes/{thread_id}/resident")
    async def ensure_shared_resident(
        thread_id: str,
        body: SharedResidentLaunchRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await endpoint_calls.run(
                owner.ensure_resident,
                thread_id,
                body.token,
                resident_launch(thread_id, body.launch),
                profiling=body.profiling,
            )
        except (DriverError, SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/shared-sandboxes/{thread_id}/turn")
    async def run_shared_turn(
        thread_id: str,
        body: SharedTurnRequest,
        _: None = Depends(authenticate),
    ) -> StreamingResponse:
        owner = require_shared_supervisor()
        try:
            turn = await endpoint_calls.run(
                owner.start_turn,
                thread_id,
                body.token,
                prompt=body.prompt,
                model=body.model,
                reasoning_effort=body.reasoning_effort,
            )
        except (DriverError, SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

        def take_reported_checkpoint(
            supervisor: Any,
            member_thread_id: str,
        ) -> dict[str, Any] | None:
            """Collect a checkpoint the sampler thread has already produced.

            The sampler runs on its own clock, so a turn that emits nothing for
            a minute still gets its uploads; this only forwards what is waiting.
            Best effort by construction: a checkpoint exists so a crash keeps
            the work already measured, and a failure to forward one must not
            reach the turn's own event stream as an error.
            """
            take = getattr(supervisor, "take_reported_checkpoint", None)
            if take is None:
                return None
            try:
                return take(member_thread_id)
            except Exception:
                return None

        def frames() -> Iterator[str]:
            turn_boundary_closed = False
            tool_windows = ToolWindowTracker()
            resources: dict[str, Any] | None = None

            def close_resources(value: Any) -> dict[str, Any]:
                nonlocal resources
                if resources is not None:
                    return resources
                resources = build_turn_resources(value, tool_windows, thread_id=thread_id)
                return resources

            try:
                for event in turn.lines():
                    tool_windows.observe(
                        event.event_type,
                        event.payload,
                        mono=time.monotonic(),
                    )
                    yield (
                        json.dumps(
                            {
                                "kind": "event",
                                "event_type": event.event_type,
                                "payload": event.payload,
                                "raw_line": event.raw_line,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    # A long turn crosses the checkpoint interval several
                    # times. The sampler thread produces those checkpoints on
                    # its own clock; this only forwards one that is waiting,
                    # which costs a dictionary read per event.
                    checkpoint = take_reported_checkpoint(owner, thread_id)
                    if checkpoint is not None:
                        yield (
                            json.dumps(
                                {
                                    "kind": "resource_checkpoint",
                                    "turn_resources": checkpoint,
                                },
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                result = turn.wait()
                # The activation boundary is part of success, not cleanup
                # after success. It reaps escaped background processes and
                # normalizes shared writes; if either fails the runner must
                # receive an error frame instead of a result followed by an
                # opaque settle failure.
                telemetry: SharedTurnTelemetry | None = owner.complete_turn(
                    thread_id, body.token, turn
                )
                turn_boundary_closed = True
                resources = close_resources(telemetry)
                yield (
                    json.dumps(
                        {
                            "kind": "result",
                            "status": result.status,
                            "final_output": result.final_output,
                            "exit_code": result.exit_code,
                            "usage": result.usage,
                            "raw": result.raw,
                            "native_turn_settled": result.native_turn_settled,
                            "stderr": turn.stderr_text()[-4000:],
                            "resources": resources,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            except Exception as exc:
                telemetry = exc.telemetry if isinstance(exc, SharedTurnBoundaryError) else None
                if telemetry is not None:
                    turn_boundary_closed = True
                if not turn_boundary_closed:
                    try:
                        telemetry = owner.abort_turn(thread_id, body.token, turn)
                        turn_boundary_closed = True
                    except SharedTurnBoundaryError as boundary_exc:
                        telemetry = boundary_exc.telemetry
                        turn_boundary_closed = True
                    except Exception:
                        telemetry = None
                yield (
                    json.dumps(
                        {
                            "kind": "error",
                            "detail": str(exc)[:1000],
                            "resources": close_resources(telemetry),
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            finally:
                with contextlib.suppress(Exception):
                    if not turn_boundary_closed:
                        owner.abort_turn(thread_id, body.token, turn)

        return StreamingResponse(
            frames(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/shared-sandboxes/{thread_id}/interrupt")
    async def interrupt_shared_turn(
        thread_id: str,
        body: SharedLeaseRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            await endpoint_calls.run(owner.interrupt, thread_id, body.token)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return {"ok": True}

    @app.post("/shared-sandboxes/{thread_id}/input")
    async def inject_shared_input(
        thread_id: str,
        body: SharedInputRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Give the member's turn a message for the model's next step, or say why not.

        Accepted means the turn has it; the turn's own stream reports where the
        model got it. A refusal is an answer, not an error: the message stays
        waiting on the control plane and goes when the turn ends.
        """
        supervisor = require_shared_supervisor()
        try:
            outcome = await endpoint_calls.run(
                supervisor.inject_input,
                thread_id,
                owner=body.owner,
                message_id=body.message_id,
                text=body.text,
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return {"accepted": outcome.accepted, "reason": outcome.reason}

    @app.get("/shared-sandboxes/{thread_id}/questions")
    async def pending_shared_questions(
        thread_id: str,
        owner: str = Query(..., min_length=1, max_length=128),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """List native questions raised by the named running attempt."""
        supervisor = require_shared_supervisor()
        try:
            questions = await endpoint_calls.run(
                supervisor.pending_questions, thread_id, owner=owner
            )
        except (SandboxError, DriverError) as exc:
            raise supervisor_error(exc) from exc
        return {"questions": questions}

    @app.post("/shared-sandboxes/{thread_id}/questions")
    async def answer_shared_question(
        thread_id: str,
        body: SharedQuestionAnswerRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Deliver a response only to the native request that raised it."""
        supervisor = require_shared_supervisor()
        try:
            outcome = await endpoint_calls.run(
                supervisor.answer_question,
                thread_id,
                owner=body.owner,
                request_id=body.request_id,
                answers=body.answers,
                reject=body.reject,
            )
        except (SandboxError, DriverError) as exc:
            raise supervisor_error(exc) from exc
        return {"accepted": outcome.accepted, "reason": outcome.reason}

    @app.post("/shared-sandboxes/{thread_id}/residency-lost")
    async def shared_residency_lost(
        thread_id: str,
        body: SharedResidencyLostRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await endpoint_calls.run(
                owner.residency_lost,
                thread_id,
                generation=body.generation,
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/shared-sandboxes/{thread_id}/settle")
    async def settle_shared_chat_sandbox(
        thread_id: str,
        body: SharedSettleRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await endpoint_calls.run(
                owner.settle,
                thread_id,
                body.token,
                job_id=body.job_id,
                context_status=body.context_status,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/shared-sandboxes/{thread_id}/abandon")
    async def abandon_shared_chat_sandbox(
        thread_id: str,
        body: SharedAbandonRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await endpoint_calls.run(
                owner.abandon,
                thread_id,
                body.token,
                lost=body.lost,
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.get("/shared-sandboxes/{thread_id}")
    async def shared_chat_sandbox_status(
        thread_id: str,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await endpoint_calls.run(owner.status, thread_id)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/shared-sandboxes/{thread_id}/stop-idle")
    async def stop_idle_shared_chat_sandbox(
        thread_id: str,
        body: SharedIdleStopRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await endpoint_calls.run(
                owner.stop_if_idle,
                thread_id,
                generation=body.generation,
                activation_generation=body.activation_generation,
                reason=body.reason,
                member_activation_generations=body.member_activation_generations,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    def sandbox_archive_identity(
        thread_id: str, body: SandboxArchivePrepareRequest
    ) -> ArchiveIdentity:
        return ArchiveIdentity(
            thread_id=thread_id,
            request_id=body.request_id,
            generation=body.generation,
            expected_container_id=body.expected_container_id,
            archive_key=body.archive_key,
        )

    @app.post("/shared-sandboxes/{thread_id}/admin-delete/prepare")
    async def prepare_sandbox_archive(
        thread_id: str,
        body: SandboxArchivePrepareRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        require_shared_supervisor()
        try:
            return await endpoint_calls.run(
                app.state.sandbox_deletion_archive_service.prepare,
                sandbox_archive_identity(thread_id, body),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @app.post("/shared-sandboxes/{thread_id}/admin-delete/upload")
    async def upload_sandbox_archive(
        thread_id: str,
        body: SandboxArchiveUploadRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        require_shared_supervisor()
        try:
            return await endpoint_calls.run(
                app.state.sandbox_deletion_archive_service.upload,
                sandbox_archive_identity(thread_id, body),
                sha256=body.sha256,
                size_bytes=body.size_bytes,
                upload_url=body.upload_url,
                upload_headers=body.upload_headers,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @app.post("/shared-sandboxes/{thread_id}/admin-delete/commit")
    async def commit_sandbox_archive_deletion(
        thread_id: str,
        body: SandboxArchiveCommitRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        require_shared_supervisor()
        try:
            return await endpoint_calls.run(
                app.state.sandbox_deletion_archive_service.commit,
                sandbox_archive_identity(thread_id, body),
                sha256=body.sha256,
                size_bytes=body.size_bytes,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @app.post("/shared-sandboxes/{thread_id}/admin-delete/abort")
    async def abort_sandbox_archive(
        thread_id: str,
        body: SandboxArchivePrepareRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        require_shared_supervisor()
        try:
            return await endpoint_calls.run(
                app.state.sandbox_deletion_archive_service.abort,
                sandbox_archive_identity(thread_id, body),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @app.post("/shared-sandboxes/{thread_id}/fork")
    async def fork_shared_chat_session(
        thread_id: str,
        body: SharedForkRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            # The target already has a durable slot reservation, but Fork
            # itself installs its native identity. Reconciling that placeholder
            # first would occupy the identity with None, or overwrite a child
            # recovered after a lost receipt. Keep local live-child ownership
            # intact so fork_native can recover it under the existing fences.
            source_members = [
                member for member in body.members if member.thread_id != body.target_thread_id
            ]
            await endpoint_calls.run(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(source_members),
                expected_container_id=body.expected_container_id,
            )
            result = await endpoint_calls.run(
                owner.fork_native,
                thread_id,
                operation_id=body.operation_id,
                source_native_session_id=body.source_native_session_id,
                source_native_turn_no=body.source_native_turn_no,
                target_thread_id=body.target_thread_id,
                target_slot_id=body.target_slot_id,
                source_launch=resident_launch(thread_id, body.source_launch),
                target_launch=resident_launch(body.target_thread_id, body.target_launch),
            )
        except (DriverError, SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(result)

    @app.post("/shared-sandboxes/{thread_id}/snapshot")
    async def capture_shared_workspace_snapshot(
        thread_id: str,
        body: SharedSnapshotCaptureRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Capture one immutable group diff under the supervisor barrier."""
        owner = require_shared_supervisor()
        try:
            await endpoint_calls.run(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
            )
            result = await endpoint_calls.run(
                owner.capture_snapshot,
                thread_id,
                capture=lambda backend: _run_git(
                    backend,
                    CHAT_WORKDIR,
                    body.base_sha,
                    timeout_s=applied_settings["workspace_command_timeout_s"],
                    max_output_bytes=int(applied_settings["workspace_command_output_max_mib"])
                    * 1024
                    * 1024,
                ),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        patch = result.get("patch")
        if not isinstance(patch, str):
            raise HTTPException(status_code=502, detail="Git capture returned no patch")
        return {
            "snapshot_id": body.snapshot_id,
            "sandbox_id": body.sandbox_id,
            "sandbox_generation": body.generation,
            "container_id": body.expected_container_id,
            "workspace_epoch": body.workspace_epoch,
            "base_sha": body.base_sha,
            "patch": patch,
            "content_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        }

    @app.post("/shared-sandboxes/{thread_id}/import-base")
    async def import_shared_workspace_base(
        thread_id: str,
        body: SharedBaseImportRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Import a trusted publisher commit object without checkout/reset."""
        owner = require_shared_supervisor()
        try:
            bundle = base64.b64decode(body.bundle_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="base bundle is not valid base64") from exc
        if len(bundle) > DEFAULT_MAX_BASE_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail="base bundle exceeds the import limit")
        try:
            await endpoint_calls.run(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
            )
            token, _descriptor = await endpoint_calls.run(owner.operation_opened, thread_id)
            try:
                backend = owner.backend(thread_id)
                await endpoint_calls.run(
                    backend.manager.import_base_bundle,
                    backend.spec,
                    bundle,
                    commit=body.commit_sha,
                    env={
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "safe.directory",
                        "GIT_CONFIG_VALUE_0": CHAT_WORKDIR,
                    },
                )
            finally:
                await endpoint_calls.run(owner.operation_closed, thread_id, token)
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return {
            "ok": True,
            "snapshot_id": body.snapshot_id,
            "sandbox_id": body.sandbox_id,
            "commit_sha": body.commit_sha,
            "workspace_head_preserved": True,
        }

    @app.get("/sandboxes")
    async def list_chat_sandboxes(
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Return Docker-observed state for host-heartbeat reconciliation.

        This is a complete observation, not an ownership mutation.  The
        control plane keeps the durable thread/container mapping and accepts
        only exact identity matches; the broker supplies the fact it alone
        can see: whether each retained Docker container is currently running.
        Reconciliation also makes a broker restart self-healing because it
        rebuilds the supervisor's in-memory index from Docker labels first.
        """
        try:
            shared_descriptors = await endpoint_calls.run(require_shared_supervisor().list)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        rows: list[dict[str, Any]] = []
        shared_groups: dict[str, list[dict[str, Any]]] = {}
        for descriptor in shared_descriptors:
            shared_groups.setdefault(descriptor.sandbox_id, []).append(asdict(descriptor))
        for members in shared_groups.values():
            origin = next(
                (row for row in members if row["thread_id"] == row["origin_thread_id"]),
                members[0],
            )
            origin = dict(origin)
            # Descriptors count each member's PTYs; the heartbeat observes the
            # whole shared container, including terminals opened by a fork.
            origin["terminal_count"] = sum(row["terminal_count"] for row in members)
            origin["shared_live"] = True
            origin["members"] = [
                {
                    "thread_id": row["thread_id"],
                    "native_session_id": row["native_session_id"],
                    "context_status": row["context_status"],
                    "activation_generation": row["activation_generation"],
                    "process_generation": row["process_generation"],
                    "residency_generation": row["residency_generation"],
                    "resident_state": row["resident_state"],
                    "resident_slot_id": row["resident_slot_id"],
                    "last_settled_job_id": row["last_settled_job_id"],
                }
                for row in members
            ]
            rows.append(origin)
        resource_collector = app.state.sandbox_resource_collector
        if resource_collector is not None:
            # The listing already proved each group's labels and exact ID.
            # Synchronization and cache reads perform no Docker stats I/O.
            resource_collector.sync_targets(rows)
        for row in rows:
            row["resource_usage"] = (
                None
                if resource_collector is None or not row["running"]
                else resource_collector.latest(row["container_id"])
            )
        return {"sandboxes": rows}

    @app.get("/workspaces/{workspace_id}/files")
    def files(
        workspace_id: str,
        request: Request,
        path: str = Query("", max_length=4096),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        relative = _relative_path(path)
        # A path alone cannot say what this request is: the empty path is the
        # root listing, but a non-empty one may be a file preview or a
        # directory expansion, and only the read can tell them apart. So the
        # call is opened for the candidates the caller offered and filed by the
        # kind its own result reports.
        candidates = ("files.list", "files.open") if path else ("files.list",)
        call = open_call(request.headers.get(PROFILING_CONTEXT_HEADER), operations=candidates)

        def _read(retained: Any, active: BrokerCall) -> dict[str, Any]:
            body = read_retained_file(
                retained,
                relative,
                max_preview_bytes=int(applied_settings["workspace_file_preview_max_kib"]) * 1024,
                max_directory_entries=int(applied_settings["workspace_directory_max_entries"]),
                observer=active.recorder,
            )
            kind = str(body.get("kind") or "")
            if kind == "directory":
                active.set_operation("files.list")
            elif kind in {"file", "symlink"}:
                active.set_operation("files.open")
            return body

        return retained_operation(workspace_id, _read, call=call)

    @app.put("/workspaces/{workspace_id}/files")
    def write_file(
        workspace_id: str,
        body: FileWriteRequest,
        path: str = Query(..., min_length=1, max_length=4096),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        relative = _relative_path(path)
        return retained_operation(
            workspace_id,
            lambda retained, _call: write_retained_file(
                retained,
                relative,
                body.content,
                max_write_bytes=int(applied_settings["workspace_file_write_max_mib"]) * 1024 * 1024,
                max_preview_bytes=int(applied_settings["workspace_file_preview_max_kib"]) * 1024,
                max_directory_entries=int(applied_settings["workspace_directory_max_entries"]),
            ),
        )

    @app.post("/workspaces/{workspace_id}/terminal")
    async def terminal(
        workspace_id: str,
        body: TerminalRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        return await endpoint_calls.run(
            retained_operation,
            workspace_id,
            lambda retained, _call: _run_terminal(
                retained,
                "/workspace",
                body,
                default_timeout_s=applied_settings["workspace_command_timeout_s"],
                max_output_bytes=int(applied_settings["workspace_command_output_max_mib"])
                * 1024
                * 1024,
            ),
        )

    @app.post("/workspaces/{workspace_id}/terminals")
    async def create_terminal(
        workspace_id: str,
        request: Request,
        body: TerminalCreateRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        call = open_call(
            request.headers.get(PROFILING_CONTEXT_HEADER), operations=("terminal.open",)
        )
        owner = retained_owner(workspace_id)
        terminal_token: str | None = None
        try:
            with call.stage("admission"):
                token, descriptor = await endpoint_calls.run(owner.terminal_opened, workspace_id)
            terminal_token = token
            retained = owner.backend(workspace_id)
            with call.stage("network"):
                retained.manager.require_network(retained.spec)

            def on_terminal_closed(last_activity: float) -> None:
                owner.terminal_closed(
                    workspace_id,
                    token,
                    last_activity_monotonic=last_activity,
                )

            with call.stage("pty_spawn"):
                body_out = await endpoint_calls.run(
                    terminals.create,
                    descriptor.origin_thread_id,
                    "/workspace",
                    rows=body.rows,
                    cols=body.cols,
                    backend=retained,
                    idle_ttl_s=owner.idle_timeout_s,
                    on_closed=on_terminal_closed,
                )
            return call.answer(body_out)
        except (HTTPException, SandboxError, ValueError) as exc:
            if terminal_token is not None:
                with contextlib.suppress(SandboxError):
                    owner.terminal_closed(workspace_id, terminal_token)
            if isinstance(exc, HTTPException):
                raise
            raise supervisor_error(exc) from exc

    @app.post("/workspaces/{workspace_id}/terminals/suspend")
    async def suspend_terminals(
        workspace_id: str,
        body: TerminalCoordinationRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        scope = terminal_scope(workspace_id)
        return await endpoint_calls.run(
            terminals.suspend,
            scope,
            lease_generation=body.lease_generation,
            job_id=body.job_id,
        )

    @app.post("/workspaces/{workspace_id}/terminals/resume")
    async def resume_terminals(
        workspace_id: str,
        body: TerminalCoordinationRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        scope = terminal_scope(workspace_id)
        return await endpoint_calls.run(
            terminals.resume,
            scope,
            lease_generation=body.lease_generation,
            job_id=body.job_id,
        )

    @app.post("/workspaces/{workspace_id}/terminals/resume-settled")
    async def resume_settled_terminals(
        workspace_id: str,
        body: TerminalSettledRequest | None = None,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        scope = terminal_scope(workspace_id)
        return await endpoint_calls.run(
            terminals.resume_settled,
            scope,
            job_id=body.job_id if body is not None else None,
        )

    @app.get("/workspaces/{workspace_id}/terminals")
    async def list_terminals(
        workspace_id: str,
        request: Request,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        call = open_call(
            request.headers.get(PROFILING_CONTEXT_HEADER), operations=("terminal.open",)
        )
        scope = terminal_scope(workspace_id)
        with call.stage("list"):
            body = {
                "terminals": await endpoint_calls.run(terminals.list, scope),
                "max_sessions": terminals.max_sessions_per_workspace,
                "max_input_bytes": int(applied_settings["terminal_input_max_kib"]) * 1024,
            }
        return call.answer(body)

    @app.get("/workspaces/{workspace_id}/terminals/{terminal_id}/stream")
    async def stream_terminal(
        workspace_id: str,
        terminal_id: str,
        request: Request,
        after: int = Query(0, ge=0),
        _: None = Depends(authenticate),
    ) -> StreamingResponse:
        call = open_call(
            request.headers.get(PROFILING_CONTEXT_HEADER), operations=("terminal.open",)
        )
        scope = terminal_scope(workspace_id)
        with call.stage("stream_attach"):
            session = terminals.get(scope, terminal_id)
            session.open_stream()

        async def event_stream() -> AsyncIterator[str]:
            cursor = after
            try:
                yield _terminal_attached_frame(session.descriptor()["last_seq"])
                while True:
                    events = await session.events_after_async(
                        cursor,
                        wait_s=TERMINAL_STREAM_WAIT_S,
                    )
                    for event in events:
                        cursor = event.seq
                        yield _terminal_sse_frame(event)
                    descriptor = session.descriptor()
                    if descriptor["state"] != "running" and cursor >= descriptor["last_seq"]:
                        return
                    if not events:
                        # Keep intermediaries from timing out a quiet interactive
                        # shell without inventing a sequenced client event.
                        yield ": keepalive\n\n"
            finally:
                session.close_stream()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            # A stream's body is the terminal's own bytes, so this call's
            # measurement answer travels in headers rather than in the body.
            headers=call.headers(extra={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}),
        )

    @app.post("/workspaces/{workspace_id}/terminals/{terminal_id}/input")
    async def terminal_input(
        workspace_id: str,
        terminal_id: str,
        body: TerminalInputRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        scope = terminal_scope(workspace_id)
        try:
            data = base64.b64decode(body.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="terminal input is not valid base64"
            ) from exc
        if len(data) > int(applied_settings["terminal_input_max_kib"]) * 1024:
            raise HTTPException(status_code=413, detail="terminal input is too large")
        session = terminals.get_for_control(scope, terminal_id)
        return await endpoint_calls.run(session.write, data, now=time.monotonic())

    @app.post("/workspaces/{workspace_id}/terminals/{terminal_id}/resize")
    async def resize_terminal(
        workspace_id: str,
        terminal_id: str,
        body: TerminalResizeRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        scope = terminal_scope(workspace_id)
        session = terminals.get_for_control(scope, terminal_id)
        return await endpoint_calls.run(
            session.resize,
            rows=body.rows,
            cols=body.cols,
            now=time.monotonic(),
        )

    @app.delete("/workspaces/{workspace_id}/terminals/{terminal_id}")
    async def delete_terminal(
        workspace_id: str,
        terminal_id: str,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        scope = terminal_scope(workspace_id)
        return await endpoint_calls.run(terminals.delete, scope, terminal_id)

    @app.get("/workspaces/{workspace_id}/git")
    async def git(
        workspace_id: str,
        base_sha: str | None = Query(None, max_length=64),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        return await endpoint_calls.run(
            retained_operation,
            workspace_id,
            lambda retained, _call: _run_git(
                retained,
                "/workspace",
                base_sha,
                timeout_s=applied_settings["workspace_command_timeout_s"],
                max_output_bytes=int(applied_settings["workspace_command_output_max_mib"])
                * 1024
                * 1024,
            ),
        )

    return app


def main(argv: list[str] | None = None) -> int:
    """Run the broker on the private Compose network."""
    # This process had no logging configuration at all: uvicorn sets up its own
    # loggers and declares no `root`, so every `logger.info(...)` here — the
    # terminal and workspace events among them — reached no handler, and
    # anything at WARNING or above came out through `logging.lastResort` as a
    # bare message. The runner's `main()` has always done this; the broker was
    # simply missed.
    configure_process_logging()
    parser = argparse.ArgumentParser(description="Serve durable agent workspaces.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args(argv)
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FileWriteRequest",
    "TerminalCreateRequest",
    "TerminalInputRequest",
    "TerminalRequest",
    "TerminalResizeRequest",
    "create_app",
]
