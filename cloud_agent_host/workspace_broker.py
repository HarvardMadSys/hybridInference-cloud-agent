"""Internal broker for live agent workspaces.

The public gateway authenticates workspace owners and forwards only a validated
job id.  This service is deliberately separate from the gateway: it mounts
the runner's worktree volume and Docker socket, while the public-facing backend
does neither.  Commands and Git inspection therefore execute in the same
disposable sandbox boundary as the agent, never in the trusted broker process.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
import queue
import re
import secrets
import stat
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.resources import ResourceSampler
from cloud_agent_host.retained_workspace import (
    RetainedWorkspaceError,
    read_retained_file,
    write_retained_file,
)
from cloud_agent_host.runtimes import registered_runtimes
from cloud_agent_host.sandbox import (
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    SandboxSpec,
    TerminalNotSupportedError,
    TerminalProcess,
    build_backend_from_env,
)
from cloud_agent_host.session_supervisor import (
    DEFAULT_CHAT_IDLE_TIMEOUT_S,
    ChatSessionSupervisor,
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
    SharedTurnBoundaryError,
    SharedTurnTelemetry,
)
from cloud_agent_host.source_archive import DEFAULT_MAX_BASE_BUNDLE_BYTES
from cloud_agent_host.tool_windows import ToolWindowTracker, attribute, tool_time_ms
from cloud_agent_host.workspace_browser import (
    MAX_FILE_PREVIEW_BYTES,
    WorkspacePathError,
    normalize_workspace_path,
)
from cloud_agent_host.workspace_paths import WorkspaceIdError, workspace_path

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_FILE_WRITE_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 5000
DEFAULT_COMMAND_TIMEOUT_S = 60.0
MAX_TERMINAL_SESSIONS_PER_WORKSPACE = 4
DEFAULT_MAX_TERMINAL_SESSIONS = 64
MAX_TERMINAL_BUFFER_BYTES = 2 * 1024 * 1024
MAX_TERMINAL_BUFFER_EVENTS = 4096
MAX_TERMINAL_TOTAL_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_TERMINAL_INPUT_BYTES = 64 * 1024
MAX_TERMINAL_STREAMS_PER_SESSION = 4
DEFAULT_TERMINAL_IDLE_TTL_S = 30 * 60.0
DEFAULT_TERMINAL_HARD_TTL_S = 8 * 60 * 60.0
TERMINAL_STREAM_WAIT_S = 15.0
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _chat_idle_timeout_from_env() -> float:
    """Read the canonical chat-idle clock, accepting the preview-era alias."""
    raw = os.environ.get("AGENT_CHAT_IDLE_TIMEOUT_S")
    if not raw:
        raw = os.environ.get("AGENT_SESSION_IDLE_TIMEOUT_S")
    return float(raw) if raw else DEFAULT_CHAT_IDLE_TIMEOUT_S


def _restartable_mode_covers_every_runtime(enabled: bool) -> bool:
    """Whether no legacy broker request needs the host worktree bind."""
    if not enabled:
        return False
    raw = os.environ.get("AGENT_RESTARTABLE_RUNTIMES")
    allowed = (
        {item.strip() for item in raw.split(",") if item.strip()}
        if raw
        else set(registered_runtimes())
    )
    return set(registered_runtimes()) <= allowed


class FileWriteRequest(BaseModel):
    """One bounded UTF-8 file replacement."""

    content: str = Field(..., max_length=MAX_FILE_WRITE_BYTES)


class TerminalRequest(BaseModel):
    """One interactive-shell command against the mounted workspace."""

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
    """Monotonic worker fence for terminal suspension and resumption."""

    lease_generation: int = Field(..., ge=1)


class ChatSandboxAcquireRequest(BaseModel):
    """Immutable chat identity plus the fenced attempt asking to use it."""

    sandbox_id: str = Field(..., min_length=1, max_length=256)
    generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)
    sandbox_image_digest: str | None = Field(None, max_length=512)
    owner: str = Field(..., min_length=1, max_length=256)
    has_settled_context: bool = False
    live_process_proven: bool = False
    expected_container_id: str | None = Field(None, min_length=1, max_length=256)
    memory_limit: str = Field("4g", min_length=1, max_length=32)
    cpu_limit: str = Field("2", min_length=1, max_length=32)
    stop_timeout_s: int = Field(10, ge=1, le=300)


class ChatSandboxLeaseRequest(BaseModel):
    """Receipt for one local activation mutation."""

    token: str = Field(..., min_length=1, max_length=512)


class SharedResidencyLostRequest(BaseModel):
    """Control-plane rejection of one exact member residency generation."""

    generation: int = Field(..., ge=1)


class ChatSandboxSettleRequest(ChatSandboxLeaseRequest):
    """A durable turn boundary that starts the settled idle timer."""

    job_id: str = Field(..., min_length=1, max_length=256)
    context_status: Literal["new", "live", "restored"]


class ChatSandboxAbandonRequest(ChatSandboxLeaseRequest):
    """Release a failed local admission without claiming a settled context."""

    lost: bool = False


class ChatSandboxIdleStopRequest(BaseModel):
    """Durable control-plane fence authorizing one retained-container stop."""

    generation: int = Field(..., ge=1)
    # A broker reconstructed before the first agent activation may observe a
    # generation-zero container after a file/terminal wake. The durable stop
    # fence must still be able to name that exact container.
    activation_generation: int = Field(..., ge=0)
    reason: Literal["idle", "orphaned_attempt", "lost"] = "idle"


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
    stop_timeout_s: int = Field(10, ge=1, le=300)
    members: list[SharedSandboxMemberRequest] = Field(..., min_length=1, max_length=128)


class SharedSandboxAcquireRequest(SharedSandboxIdentityRequest):
    """Lease one member while preserving sibling concurrency."""

    owner: str = Field(..., min_length=1, max_length=256)
    activation_generation: int = Field(..., ge=1)
    has_settled_context: bool = False


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
    mcp_base_url: str = Field("", max_length=2048)
    mcp_credential: str = Field("", max_length=16384)
    slot_id: int = Field(..., ge=0, le=127)
    residency_generation: int = Field(0, ge=0)


class SharedResidentLaunchRequest(ChatSandboxLeaseRequest):
    """Lease fence plus resident launch configuration."""

    launch: SharedResidentLaunchConfig


class SharedTurnRequest(ChatSandboxLeaseRequest):
    """One prompt delivered to an already-owned resident process."""

    prompt: str = Field(..., min_length=1, max_length=2_000_000)
    model: str = Field(..., min_length=1, max_length=256)
    reasoning_effort: str | None = Field(None, max_length=64)


class SharedSettleRequest(ChatSandboxLeaseRequest):
    """One member's successful native turn boundary."""

    job_id: str = Field(..., min_length=1, max_length=256)
    context_status: Literal["new", "live", "restored"]


class SharedForkRequest(SharedSandboxIdentityRequest):
    """Prompt-free fork-only request inside one shared sandbox."""

    operation_id: str = Field(..., min_length=1, max_length=256)
    source_native_session_id: str = Field(..., min_length=1, max_length=512)
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


@dataclass(frozen=True)
class _TerminalEvent:
    """One resumable event in a terminal's bounded output ring."""

    seq: int
    kind: str
    data: bytes = b""
    exit_code: int | None = None
    reason: str | None = None


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
    ) -> None:
        self.id = terminal_id
        self.shell = "sh"
        self.cwd = "/workspace"
        self.rows = rows
        self.cols = cols
        self.created_at = now
        self.last_active_at = now
        self._idle_ttl_s = idle_ttl_s
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
            self._process.resume()
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
            if self._active_streams >= MAX_TERMINAL_STREAMS_PER_SESSION:
                raise HTTPException(status_code=429, detail="terminal stream limit reached")
            self._active_streams += 1

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
        chunk_truncated = len(data) > MAX_TERMINAL_BUFFER_BYTES
        if chunk_truncated:
            data = data[-MAX_TERMINAL_BUFFER_BYTES:]
        with self._condition:
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
            self._buffer_bytes > MAX_TERMINAL_BUFFER_BYTES
            or len(self._events) + (reset_seq is not None) > MAX_TERMINAL_BUFFER_EVENTS
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
                self._total_output_bytes += len(chunk)
                if self._total_output_bytes > MAX_TERMINAL_TOTAL_OUTPUT_BYTES:
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
    """Ephemeral, bounded registry of terminal sessions by workspace."""

    def __init__(
        self,
        backend: SandboxBackend,
        *,
        idle_ttl_s: float,
        hard_ttl_s: float,
        max_sessions: int,
    ) -> None:
        self._backend = backend
        self._idle_ttl_s = idle_ttl_s
        self._hard_ttl_s = hard_ttl_s
        self._max_sessions = max_sessions
        self._sessions: dict[str, dict[str, _TerminalSession]] = {}
        self._suspended_workspaces: set[str] = set()
        self._workspace_generations: dict[str, int] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

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
        """Stop cleanup and kill every live sandbox during broker shutdown."""
        self._stop.set()
        with self._lock:
            sessions = [session for group in self._sessions.values() for session in group.values()]
            self._sessions.clear()
            self._suspended_workspaces.clear()
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
        backend: SandboxBackend | None = None,
        idle_ttl_s: float | None = None,
        on_closed: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """Create one PTY while atomically enforcing the per-workspace cap."""
        self.reap()
        terminal_id = "term_" + secrets.token_urlsafe(18)
        now = time.monotonic()
        with self._lock:
            if workspace_id in self._suspended_workspaces:
                raise HTTPException(status_code=409, detail="terminal workspace is not ready")
            group = self._sessions.setdefault(workspace_id, {})
            global_active = sum(
                not session.is_closed
                for sessions in self._sessions.values()
                for session in sessions.values()
            )
            if global_active >= self._max_sessions:
                if not group:
                    self._sessions.pop(workspace_id, None)
                raise HTTPException(status_code=503, detail="broker terminal limit reached")
            active = sum(not session.is_closed for session in group.values())
            if active >= MAX_TERMINAL_SESSIONS_PER_WORKSPACE:
                raise HTTPException(status_code=409, detail="workspace terminal limit reached")
            terminal_backend = backend or self._backend
            spec = SandboxSpec(
                argv=["/bin/sh"],
                workdir=str(workdir),
                env={
                    **terminal_backend.base_env(),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "TERM": "xterm-256color",
                },
                phase="agent",
            )
            try:
                process = terminal_backend.spawn_terminal(spec, rows=rows, cols=cols)
            except TerminalNotSupportedError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=("Interactive terminals require the container or kata sandbox backend."),
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
            )
            group[terminal_id] = session
        session.start()
        return session.descriptor()

    def suspend(self, workspace_id: str, *, lease_generation: int) -> dict[str, Any]:
        """Fence new PTYs and freeze every existing terminal process tree."""
        with self._lock:
            current_generation = self._workspace_generations.get(workspace_id, 0)
            if lease_generation < current_generation:
                raise HTTPException(status_code=409, detail="stale terminal workspace fence")
            self._workspace_generations[workspace_id] = lease_generation
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

    def resume(self, workspace_id: str, *, lease_generation: int) -> dict[str, Any]:
        """Resume retained PTYs, opening the workspace only after all succeed."""
        with self._lock:
            current_generation = self._workspace_generations.get(workspace_id, 0)
            if lease_generation < current_generation:
                raise HTTPException(status_code=409, detail="stale terminal workspace fence")
            self._workspace_generations[workspace_id] = lease_generation
            sessions = list(self._sessions.get(workspace_id, {}).values())
            try:
                for session in sessions:
                    session.resume()
            except (SandboxError, OSError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="terminal resume could not be confirmed; retry resume",
                ) from exc
            self._suspended_workspaces.discard(workspace_id)
        return {"ok": True, "resumed": len(sessions)}

    def resume_settled(self, workspace_id: str) -> dict[str, Any]:
        """Authoritatively reopen terminals after a job is terminally settled."""
        with self._lock:
            self._workspace_generations[workspace_id] = (
                self._workspace_generations.get(workspace_id, 0) + 1
            )
            sessions = list(self._sessions.get(workspace_id, {}).values())
            try:
                for session in sessions:
                    session.resume()
            except (SandboxError, OSError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="terminal resume could not be confirmed; retry resume",
                ) from exc
            self._suspended_workspaces.discard(workspace_id)
        return {"ok": True, "resumed": len(sessions)}

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
            group = self._sessions.get(workspace_id)
            if group is not None and group.get(terminal_id) is session:
                group.pop(terminal_id, None)
                if not group:
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
        interval = max(0.1, min(30.0, self._idle_ttl_s / 2, self._hard_ttl_s / 2))
        while not self._stop.wait(interval):
            self.reap()


def _workspace(root: str, workspace_id: str) -> Path:
    try:
        path = workspace_path(root, workspace_id)
    except WorkspaceIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_dir() or not (path / ".git").is_dir():
        raise HTTPException(status_code=404, detail="workspace is not materialized")
    os.utime(path, None)
    return path


def _relative_path(raw: str | None) -> str:
    try:
        return normalize_workspace_path(raw)
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _open_parent_fd(root: Path, relative: str) -> tuple[int, str | None]:
    """Open a path's real parent beneath ``root`` without following symlinks."""
    parts = relative.split("/") if relative else []
    current_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise HTTPException(
                    status_code=400, detail="workspace path crosses an invalid directory"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1] if parts else None
    except BaseException:
        os.close(current_fd)
        raise


def _directory_response(relative: str, directory_fd: int) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise HTTPException(status_code=409, detail="workspace directory cannot be read") from exc
    children: list[tuple[str, os.stat_result]] = []
    for name in names:
        if name == ".git":
            continue
        try:
            child_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            # The agent may be changing the tree while it is open in the UI.
            continue
        children.append((name, child_info))
    children.sort(key=lambda child: (not stat.S_ISDIR(child[1].st_mode), child[0].casefold()))
    for name, child_info in children[:MAX_DIRECTORY_ENTRIES]:
        child_relative = f"{relative}/{name}" if relative else name
        kind = (
            "symlink"
            if stat.S_ISLNK(child_info.st_mode)
            else "directory"
            if stat.S_ISDIR(child_info.st_mode)
            else "file"
        )
        entries.append(
            {
                "name": name,
                "path": child_relative,
                "kind": kind,
                "size": child_info.st_size if kind != "directory" else None,
                "binary": False,
                "truncated": False,
                "status": None,
                "omitted_reason": "symlink" if kind == "symlink" else None,
            }
        )
    return {
        "path": relative,
        "kind": "directory",
        "entries": entries,
        "writable": True,
        "source": "workspace",
    }


def _file_response(root: Path, relative: str) -> dict[str, Any]:
    parent_fd, name = _open_parent_fd(root, relative)
    try:
        info = (
            os.fstat(parent_fd)
            if name is None
            else os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        )
    except FileNotFoundError as exc:
        os.close(parent_fd)
        raise HTTPException(status_code=404, detail="workspace path does not exist") from exc
    except OSError as exc:
        os.close(parent_fd)
        raise HTTPException(status_code=409, detail="workspace path cannot be read") from exc

    if stat.S_ISLNK(info.st_mode):
        os.close(parent_fd)
        return {
            "path": relative,
            "kind": "symlink",
            "size": info.st_size,
            "writable": False,
            "source": "workspace",
            "omitted_reason": "symlink",
        }
    if stat.S_ISDIR(info.st_mode):
        try:
            directory_fd = (
                parent_fd
                if name is None
                else os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            )
        except OSError as exc:
            os.close(parent_fd)
            raise HTTPException(
                status_code=409, detail="workspace directory cannot be read"
            ) from exc
        if name is not None:
            os.close(parent_fd)
        try:
            return _directory_response(relative, directory_fd)
        finally:
            os.close(directory_fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(parent_fd)
        raise HTTPException(status_code=409, detail="workspace path is not a regular file")

    if info.st_size > MAX_FILE_PREVIEW_BYTES:
        os.close(parent_fd)
        return {
            "path": relative,
            "kind": "file",
            "content": None,
            "size": info.st_size,
            "binary": False,
            "truncated": True,
            "writable": False,
            "source": "workspace",
            "omitted_reason": "too_large",
        }
    try:
        file_fd = os.open(name or ".", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        raise HTTPException(status_code=409, detail="workspace file cannot be read") from exc
    os.close(parent_fd)
    try:
        opened_info = os.fstat(file_fd)
        if not stat.S_ISREG(opened_info.st_mode):
            raise HTTPException(status_code=409, detail="workspace path is not a regular file")
        raw = b""
        while len(raw) <= MAX_FILE_PREVIEW_BYTES:
            chunk = os.read(file_fd, min(64 * 1024, MAX_FILE_PREVIEW_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(file_fd)
    if len(raw) > MAX_FILE_PREVIEW_BYTES:
        return {
            "path": relative,
            "kind": "file",
            "content": None,
            "size": opened_info.st_size,
            "binary": False,
            "truncated": True,
            "writable": False,
            "source": "workspace",
            "omitted_reason": "too_large",
        }
    binary = b"\x00" in raw
    try:
        content = None if binary else raw.decode("utf-8")
    except UnicodeDecodeError:
        binary = True
        content = None
    return {
        "path": relative,
        "kind": "file",
        "content": content,
        "size": len(raw),
        "binary": binary,
        "truncated": False,
        "writable": not binary,
        "source": "workspace",
        "omitted_reason": "binary" if binary else None,
    }


def _write_file(root: Path, relative: str, content: str) -> dict[str, Any]:
    if not relative:
        raise HTTPException(status_code=400, detail="a file path is required")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_WRITE_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds the workspace write limit")

    parts = relative.split("/")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = root_fd
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except (FileNotFoundError, NotADirectoryError, OSError) as exc:
                raise HTTPException(
                    status_code=400, detail="file parent must be an existing real directory"
                ) from exc
            opened.append(next_fd)
            parent_fd = next_fd
        try:
            file_fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o644,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise HTTPException(status_code=409, detail="workspace file cannot be written") from exc
        try:
            with os.fdopen(file_fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(file_fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)
    os.utime(root, None)
    return _file_response(root, relative)


def _run_sandbox(
    backend: SandboxBackend,
    *,
    workdir: Path | str,
    script: str,
    env: dict[str, str],
    timeout_s: float,
) -> tuple[int, str, str]:
    """Run a bounded command and return status, stdout and stderr."""
    process = backend.spawn(
        SandboxSpec(
            argv=["/bin/sh", "-lc", script],
            workdir=str(workdir),
            env={**backend.base_env(), "GIT_CONFIG_NOSYSTEM": "1", **env},
            phase="agent",
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
        if size > MAX_COMMAND_OUTPUT_BYTES:
            process.kill()
            raise HTTPException(status_code=413, detail="workspace command output is too large")
        chunks.append(line)
    exit_code = process.wait()
    stderr = process.stderr_text()
    if len(stderr.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES:
        stderr = stderr[-MAX_COMMAND_OUTPUT_BYTES:]
    if isinstance(workdir, Path):
        os.utime(workdir, None)
    return exit_code, "".join(chunks), stderr


def _run_terminal(
    backend: SandboxBackend, workdir: Path | str, request: TerminalRequest
) -> dict[str, Any]:
    marker = secrets.token_hex(16)
    shared_umask = (
        "umask 0002; "
        if isinstance(backend, RetainedChatBackend) and backend.spec.shared_live
        else ""
    )
    script = (
        shared_umask
        + 'cd "$WORKSPACE_CWD" 2>/dev/null || { printf "invalid working directory\\n" >&2; '
        "exit 125; }; "
        'eval "$WORKSPACE_COMMAND"; status=$?; '
        f'printf "\\n__HYI_{marker}_CWD__%s\\n__HYI_{marker}_STATUS__%s\\n" "$PWD" "$status"'
    )
    process_status, stdout, stderr = _run_sandbox(
        backend,
        workdir=workdir,
        script=script,
        env={"WORKSPACE_COMMAND": request.command, "WORKSPACE_CWD": request.cwd},
        timeout_s=request.timeout_seconds,
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
    backend: SandboxBackend, workdir: Path | str, base_sha: str | None = None
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
        backend, workdir=workdir, script=script, env={}, timeout_s=60
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


def create_app(
    *,
    backend: SandboxBackend | None = None,
    workdir_root: str | None = None,
    token: str | None = None,
    terminal_idle_ttl_s: float | None = None,
    terminal_hard_ttl_s: float | None = None,
    terminal_max_sessions: int | None = None,
    session_supervisor: ChatSessionSupervisor | None = None,
    shared_session_supervisor: SharedChatSessionSupervisor | None = None,
    restartable_enabled: bool | None = None,
    shared_live_enabled: bool | None = None,
    session_idle_timeout_s: float | None = None,
) -> FastAPI:
    """Create the internal broker app with injectable dependencies for tests."""
    sandbox = backend or build_backend_from_env()
    root = workdir_root or os.environ.get(
        "AGENT_WORKDIR_ROOT", "/var/lib/hybridinference/agent-jobs"
    )
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
    restartable = (
        restartable_enabled
        if restartable_enabled is not None
        else os.environ.get("AGENT_RESTARTABLE_SANDBOX", "") not in ("", "0")
    )
    chat_idle_timeout_s = (
        session_idle_timeout_s
        if session_idle_timeout_s is not None
        else _chat_idle_timeout_from_env()
    )
    if chat_idle_timeout_s <= 0:
        raise ValueError("chat sandbox idle timeout must be positive")
    supervisor = session_supervisor
    shared_enabled = (
        shared_live_enabled
        if shared_live_enabled is not None
        else os.environ.get("AGENT_SHARED_LIVE_SANDBOX", "") not in ("", "0")
    )
    shared_supervisor = shared_session_supervisor
    retained_manager: ChatSandboxManager | None = None
    if (supervisor is None or (shared_supervisor is None and shared_enabled)) and restartable:
        if not isinstance(sandbox, ContainerBackend):
            raise ValueError("restartable chat sandboxes require the container or kata backend")
        retained_manager = ChatSandboxManager(sandbox)
        if supervisor is None:
            supervisor = ChatSessionSupervisor(
                retained_manager,
                idle_timeout_s=chat_idle_timeout_s,
            )
        if shared_supervisor is None and shared_enabled:
            shared_supervisor = SharedChatSessionSupervisor(
                retained_manager,
                idle_timeout_s=chat_idle_timeout_s,
            )
    terminals = _TerminalManager(
        sandbox,
        idle_ttl_s=idle_ttl_s,
        hard_ttl_s=hard_ttl_s,
        max_sessions=max_sessions,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        Path(root).mkdir(parents=True, exist_ok=True)
        # In restartable mode every supported chat owns its repository in the
        # retained container's writable layer. The broker may itself run in a
        # container, so ``root`` is not a path the host Docker daemon can
        # bind-mount. No retained operation needs that mount; keep the legacy
        # probe for deployments that still create host worktrees.
        retained_only = _restartable_mode_covers_every_runtime(restartable)
        sandbox.preflight(workdir_root=None if retained_only else root)
        if retained_manager is not None:
            expected_docker_root = (os.environ.get("AGENT_RETAINED_DOCKER_DATA_ROOT") or "").strip()
            if expected_docker_root:
                _app.state.retained_docker_data_root = retained_manager.require_docker_data_root(
                    expected_docker_root
                )
        # A workdir root has one active broker. Container backends use that
        # stable scope to remove only crash leftovers from a previous instance.
        sandbox.prepare_terminal_broker(root)
        supervisor_started = False
        shared_supervisor_started = False
        terminals_started = False
        try:
            # Reconcile/reap retained Docker execs before this process starts
            # accepting new terminal sessions. Keep startup inside the cleanup
            # guard: a failed Docker reconciliation must not leak the terminal
            # manager's background thread from a failed ASGI lifespan.
            if supervisor is not None:
                supervisor.start()
                supervisor_started = True
            if shared_supervisor is not None:
                shared_supervisor.start()
                shared_supervisor_started = True
            terminals.start()
            terminals_started = True
            yield
        finally:
            if terminals_started:
                terminals.close()
            if shared_supervisor_started and shared_supervisor is not None:
                shared_supervisor.close()
            if supervisor_started and supervisor is not None:
                supervisor.close()

    app = FastAPI(title="Agent workspace broker", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.terminal_manager = terminals
    app.state.session_supervisor = supervisor
    app.state.shared_session_supervisor = shared_supervisor

    def authenticate(x_agent_workspace_token: str | None = Header(None)) -> None:
        if (
            not expected_token
            or not x_agent_workspace_token
            or not secrets.compare_digest(expected_token, x_agent_workspace_token)
        ):
            raise HTTPException(status_code=401, detail="invalid workspace broker credential")

    @app.get("/health")
    def health(_: None = Depends(authenticate)) -> dict[str, Any]:
        return {
            "ok": True,
            "retained_docker_data_root": getattr(
                app.state,
                "retained_docker_data_root",
                None,
            ),
        }

    def require_supervisor() -> ChatSessionSupervisor:
        if supervisor is None:
            raise HTTPException(
                status_code=409,
                detail="restartable chat sandboxes are not enabled on this host",
            )
        return supervisor

    def require_shared_supervisor() -> SharedChatSessionSupervisor:
        if shared_supervisor is None:
            raise HTTPException(
                status_code=409,
                detail="shared live chat sandboxes are not enabled on this host",
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

    def retained_owner(
        workspace_id: str,
    ) -> ChatSessionSupervisor | SharedChatSessionSupervisor | None:
        """Thread ids address Docker-owned workspaces; job ids stay legacy."""
        if not workspace_id.startswith("athr_"):
            return None
        if shared_supervisor is not None and shared_supervisor.owns(workspace_id):
            return shared_supervisor
        return require_supervisor()

    def retained_operation(
        workspace_id: str,
        operation: Callable[[SandboxBackend], dict[str, Any]],
    ) -> dict[str, Any]:
        owner = retained_owner(workspace_id)
        if owner is None:
            raise AssertionError("retained_operation called for a legacy workspace")
        token: str | None = None
        try:
            token, _ = owner.operation_opened(workspace_id)
            retained = owner.backend(workspace_id)
            retained.manager.require_network_phase(retained.spec, "agent")
            return operation(retained)
        except RetainedWorkspaceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        finally:
            if token is not None:
                with contextlib.suppress(SandboxError):
                    owner.operation_closed(workspace_id, token)

    def ensure_workspace(workspace_id: str) -> Path | None:
        owner = retained_owner(workspace_id)
        if owner is None:
            return _workspace(root, workspace_id)
        try:
            owner.status(workspace_id)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return None

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
            mcp_base_url=body.mcp_base_url,
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
            lease = await asyncio.to_thread(
                owner.acquire,
                shared_spec(body),
                thread_id=thread_id,
                owner=body.owner,
                activation_generation=body.activation_generation,
                has_settled_context=body.has_settled_context,
                manifest=shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
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
            descriptors = await asyncio.to_thread(
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

    @app.post("/shared-sandboxes/{thread_id}/resident")
    async def ensure_shared_resident(
        thread_id: str,
        body: SharedResidentLaunchRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await asyncio.to_thread(
                owner.ensure_resident,
                thread_id,
                body.token,
                resident_launch(thread_id, body.launch),
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
            turn = await asyncio.to_thread(
                owner.start_turn,
                thread_id,
                body.token,
                prompt=body.prompt,
                model=body.model,
                reasoning_effort=body.reasoning_effort,
            )
        except (DriverError, SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

        def frames():
            turn_boundary_closed = False
            tool_windows = ToolWindowTracker()
            sampler = ResourceSampler(None)
            resources: dict[str, Any] | None = None

            def close_resources(value: Any) -> dict[str, Any]:
                nonlocal resources
                if resources is not None:
                    return resources
                telemetry = (
                    value
                    if isinstance(value, SharedTurnTelemetry)
                    else SharedTurnTelemetry(
                        observation=(
                            value
                            if isinstance(value, GuestObservation)
                            else GuestObservation.unavailable(
                                "shared turn returned no process telemetry"
                            )
                        )
                    )
                )
                tool_windows.apply_marks(list(telemetry.tool_marks))
                windows = tool_windows.windows()
                resources = sampler.stop()
                tool_calls = attribute(
                    sampler.samples(),
                    windows,
                    windows_complete=not tool_windows.truncated,
                )
                processes, process_counts = attribute_processes(
                    telemetry.observation,
                    windows,
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
                result = turn.wait()
                # The activation boundary is part of success, not cleanup
                # after success. It reaps escaped background processes and
                # normalizes shared writes; if either fails the runner must
                # receive an error frame instead of a result followed by an
                # opaque settle failure.
                telemetry = owner.complete_turn(thread_id, body.token, turn)
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
        body: ChatSandboxLeaseRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            await asyncio.to_thread(owner.interrupt, thread_id, body.token)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return {"ok": True}

    @app.post("/shared-sandboxes/{thread_id}/residency-lost")
    async def shared_residency_lost(
        thread_id: str,
        body: SharedResidencyLostRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await asyncio.to_thread(
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
            descriptor = await asyncio.to_thread(
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
        body: ChatSandboxAbandonRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await asyncio.to_thread(
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
            descriptor = await asyncio.to_thread(owner.status, thread_id)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/shared-sandboxes/{thread_id}/stop-idle")
    async def stop_idle_shared_chat_sandbox(
        thread_id: str,
        body: ChatSandboxIdleStopRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            descriptor = await asyncio.to_thread(
                owner.stop_if_idle,
                thread_id,
                generation=body.generation,
                reason=body.reason,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/shared-sandboxes/{thread_id}/fork")
    async def fork_shared_chat_session(
        thread_id: str,
        body: SharedForkRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_shared_supervisor()
        try:
            await asyncio.to_thread(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
            )
            result = await asyncio.to_thread(
                owner.fork_native,
                thread_id,
                operation_id=body.operation_id,
                source_native_session_id=body.source_native_session_id,
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
            await asyncio.to_thread(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
            )
            result = await asyncio.to_thread(
                owner.capture_snapshot,
                thread_id,
                capture=lambda backend: _run_git(backend, CHAT_WORKDIR, body.base_sha),
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
            await asyncio.to_thread(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
            )
            token, _descriptor = await asyncio.to_thread(owner.operation_opened, thread_id)
            try:
                backend = owner.backend(thread_id)
                await asyncio.to_thread(
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
                await asyncio.to_thread(owner.operation_closed, thread_id, token)
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return {
            "ok": True,
            "snapshot_id": body.snapshot_id,
            "sandbox_id": body.sandbox_id,
            "commit_sha": body.commit_sha,
            "workspace_head_preserved": True,
        }

    @app.post("/sandboxes/{thread_id}/acquire")
    async def acquire_chat_sandbox(
        thread_id: str,
        body: ChatSandboxAcquireRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_supervisor()
        spec = ChatSandboxSpec(
            thread_id=thread_id,
            sandbox_id=body.sandbox_id,
            generation=body.generation,
            runtime_id=body.runtime_id,
            runtime_version=body.runtime_version,
            sandbox_image_digest=body.sandbox_image_digest,
            memory_limit=body.memory_limit,
            cpu_limit=body.cpu_limit,
            stop_timeout_s=body.stop_timeout_s,
        )
        try:
            lease = await asyncio.to_thread(
                owner.acquire,
                spec,
                owner=body.owner,
                has_settled_context=body.has_settled_context,
                activation_generation=body.activation_generation,
                live_process_proven=body.live_process_proven,
                expected_container_id=body.expected_container_id,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(lease)

    @app.post("/sandboxes/{thread_id}/process-started")
    async def chat_process_started(
        thread_id: str,
        body: ChatSandboxLeaseRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_supervisor()
        try:
            descriptor = await asyncio.to_thread(owner.note_process_started, thread_id, body.token)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/sandboxes/{thread_id}/settle")
    async def settle_chat_sandbox(
        thread_id: str,
        body: ChatSandboxSettleRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_supervisor()
        try:
            descriptor = await asyncio.to_thread(
                owner.settle,
                thread_id,
                body.token,
                job_id=body.job_id,
                context_status=body.context_status,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.post("/sandboxes/{thread_id}/abandon")
    async def abandon_chat_sandbox(
        thread_id: str,
        body: ChatSandboxAbandonRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_supervisor()
        try:
            descriptor = await asyncio.to_thread(
                owner.abandon,
                thread_id,
                body.token,
                lost=body.lost,
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

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
        owner = require_supervisor()
        try:
            descriptors = await asyncio.to_thread(owner.reconcile)
            shared_descriptors = (
                await asyncio.to_thread(shared_supervisor.list)
                if shared_supervisor is not None
                else []
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        rows = [asdict(descriptor) for descriptor in descriptors]
        shared_groups: dict[str, list[dict[str, Any]]] = {}
        for descriptor in shared_descriptors:
            shared_groups.setdefault(descriptor.sandbox_id, []).append(asdict(descriptor))
        for members in shared_groups.values():
            origin = next(
                (row for row in members if row["thread_id"] == row["origin_thread_id"]),
                members[0],
            )
            origin = dict(origin)
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
        return {"sandboxes": rows}

    @app.post("/sandboxes/{thread_id}/stop-idle")
    async def stop_idle_chat_sandbox(
        thread_id: str,
        body: ChatSandboxIdleStopRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        """Apply the durable ledger's exact idle or failed-attempt stop."""
        owner = require_supervisor()
        try:
            descriptor = await asyncio.to_thread(
                owner.stop_if_idle,
                thread_id,
                generation=body.generation,
                activation_generation=body.activation_generation,
                reason=body.reason,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.get("/sandboxes/{thread_id}")
    async def chat_sandbox_status(
        thread_id: str,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = require_supervisor()
        try:
            descriptor = await asyncio.to_thread(owner.status, thread_id)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @app.get("/workspaces/{workspace_id}/files")
    def files(
        workspace_id: str,
        path: str = Query("", max_length=4096),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        relative = _relative_path(path)
        if retained_owner(workspace_id) is not None:
            return retained_operation(
                workspace_id,
                lambda retained: read_retained_file(
                    retained,
                    relative,
                    max_preview_bytes=MAX_FILE_PREVIEW_BYTES,
                    max_directory_entries=MAX_DIRECTORY_ENTRIES,
                ),
            )
        workdir = _workspace(root, workspace_id)
        return _file_response(workdir, relative)

    @app.put("/workspaces/{workspace_id}/files")
    def write_file(
        workspace_id: str,
        body: FileWriteRequest,
        path: str = Query(..., min_length=1, max_length=4096),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        relative = _relative_path(path)
        if retained_owner(workspace_id) is not None:
            return retained_operation(
                workspace_id,
                lambda retained: write_retained_file(
                    retained,
                    relative,
                    body.content,
                    max_write_bytes=MAX_FILE_WRITE_BYTES,
                    max_preview_bytes=MAX_FILE_PREVIEW_BYTES,
                    max_directory_entries=MAX_DIRECTORY_ENTRIES,
                ),
            )
        workdir = _workspace(root, workspace_id)
        return _write_file(workdir, relative, body.content)

    @app.post("/workspaces/{workspace_id}/terminal")
    async def terminal(
        workspace_id: str,
        body: TerminalRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        if retained_owner(workspace_id) is not None:
            return await asyncio.to_thread(
                retained_operation,
                workspace_id,
                lambda retained: _run_terminal(retained, "/workspace", body),
            )
        workdir = _workspace(root, workspace_id)
        return await asyncio.to_thread(_run_terminal, sandbox, workdir, body)

    @app.post("/workspaces/{workspace_id}/terminals")
    async def create_terminal(
        workspace_id: str,
        body: TerminalCreateRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = retained_owner(workspace_id)
        if owner is not None:
            terminal_token: str | None = None
            try:
                terminal_token, _ = await asyncio.to_thread(owner.terminal_opened, workspace_id)
                retained = owner.backend(workspace_id)
                retained.manager.require_network_phase(retained.spec, "agent")
                token = terminal_token
                return await asyncio.to_thread(
                    terminals.create,
                    workspace_id,
                    "/workspace",
                    rows=body.rows,
                    cols=body.cols,
                    backend=retained,
                    # A retained PTY cannot add a second inactivity window.
                    # Reap it by the earlier resource/chat limit, then let the
                    # supervisor continue from its final activity timestamp.
                    idle_ttl_s=owner.idle_timeout_s,
                    on_closed=lambda last_activity: owner.terminal_closed(
                        workspace_id,
                        token,
                        last_activity_monotonic=last_activity,
                    ),
                )
            except (SandboxError, ValueError) as exc:
                if terminal_token is not None:
                    with contextlib.suppress(SandboxError):
                        owner.terminal_closed(workspace_id, terminal_token)
                raise supervisor_error(exc) from exc
        workdir = _workspace(root, workspace_id)
        return await asyncio.to_thread(
            terminals.create,
            workspace_id,
            workdir,
            rows=body.rows,
            cols=body.cols,
        )

    @app.post("/workspaces/{workspace_id}/terminals/suspend")
    async def suspend_terminals(
        workspace_id: str,
        body: TerminalCoordinationRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = retained_owner(workspace_id)
        if owner is None:
            return await asyncio.to_thread(
                terminals.suspend,
                workspace_id,
                lease_generation=body.lease_generation,
            )
        if isinstance(owner, SharedChatSessionSupervisor):
            # Shared members never take the legacy group-wide workspace lock.
            # This endpoint coordinates only terminals owned by this chat;
            # sibling agents and bounded workspace operations remain live.
            return await asyncio.to_thread(
                terminals.suspend,
                workspace_id,
                lease_generation=body.lease_generation,
            )
        try:
            await asyncio.to_thread(owner.suspend_workspace, workspace_id)
            result = await asyncio.to_thread(
                terminals.suspend,
                workspace_id,
                lease_generation=body.lease_generation,
            )
            await asyncio.to_thread(owner.wait_workspace_operations, workspace_id)
            return result
        except (SandboxError, ValueError) as exc:
            with contextlib.suppress(SandboxError):
                await asyncio.to_thread(owner.resume_workspace, workspace_id)
            raise supervisor_error(exc) from exc

    @app.post("/workspaces/{workspace_id}/terminals/resume")
    async def resume_terminals(
        workspace_id: str,
        body: TerminalCoordinationRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = retained_owner(workspace_id)
        result = await asyncio.to_thread(
            terminals.resume,
            workspace_id,
            lease_generation=body.lease_generation,
        )
        if owner is not None and not isinstance(owner, SharedChatSessionSupervisor):
            try:
                await asyncio.to_thread(owner.resume_workspace, workspace_id)
            except SandboxError as exc:
                raise supervisor_error(exc) from exc
        return result

    @app.post("/workspaces/{workspace_id}/terminals/resume-settled")
    async def resume_settled_terminals(
        workspace_id: str,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        owner = retained_owner(workspace_id)
        result = await asyncio.to_thread(terminals.resume_settled, workspace_id)
        if owner is not None and not isinstance(owner, SharedChatSessionSupervisor):
            try:
                await asyncio.to_thread(owner.resume_workspace, workspace_id)
            except SandboxError as exc:
                raise supervisor_error(exc) from exc
        return result

    @app.get("/workspaces/{workspace_id}/terminals")
    async def list_terminals(
        workspace_id: str,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        ensure_workspace(workspace_id)
        return {"terminals": await asyncio.to_thread(terminals.list, workspace_id)}

    @app.get("/workspaces/{workspace_id}/terminals/{terminal_id}/stream")
    async def stream_terminal(
        workspace_id: str,
        terminal_id: str,
        after: int = Query(0, ge=0),
        _: None = Depends(authenticate),
    ) -> StreamingResponse:
        ensure_workspace(workspace_id)
        session = terminals.get(workspace_id, terminal_id)
        session.open_stream()

        async def event_stream():
            cursor = after
            try:
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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/workspaces/{workspace_id}/terminals/{terminal_id}/input")
    async def terminal_input(
        workspace_id: str,
        terminal_id: str,
        body: TerminalInputRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        ensure_workspace(workspace_id)
        try:
            data = base64.b64decode(body.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="terminal input is not valid base64"
            ) from exc
        if len(data) > MAX_TERMINAL_INPUT_BYTES:
            raise HTTPException(status_code=413, detail="terminal input is too large")
        session = terminals.get_for_control(workspace_id, terminal_id)
        return await asyncio.to_thread(session.write, data, now=time.monotonic())

    @app.post("/workspaces/{workspace_id}/terminals/{terminal_id}/resize")
    async def resize_terminal(
        workspace_id: str,
        terminal_id: str,
        body: TerminalResizeRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        ensure_workspace(workspace_id)
        session = terminals.get_for_control(workspace_id, terminal_id)
        return await asyncio.to_thread(
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
        ensure_workspace(workspace_id)
        return await asyncio.to_thread(terminals.delete, workspace_id, terminal_id)

    @app.get("/workspaces/{workspace_id}/git")
    async def git(
        workspace_id: str,
        base_sha: str | None = Query(None, max_length=64),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        if retained_owner(workspace_id) is not None:
            return await asyncio.to_thread(
                retained_operation,
                workspace_id,
                lambda retained: _run_git(retained, "/workspace", base_sha),
            )
        workdir = _workspace(root, workspace_id)
        return await asyncio.to_thread(_run_git, sandbox, workdir, base_sha)

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    """Run the broker on the private Compose network."""
    parser = argparse.ArgumentParser(description="Serve durable agent workspaces.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args(argv)
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
    "app",
    "create_app",
]
