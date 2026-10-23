"""One interactive terminal in a retained chat container.

A PTY process with a bounded, resumable output ring, the limits on that ring
and its viewers, and two lifetimes, one idle and one absolute. The sandbox
manager's registry (``_TerminalManager``) holds these terminals: its reaper
thread closes one past either lifetime, and until then a request treats it as
already gone (:meth:`_TerminalSession.lapsed`).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.host_settings import SANDBOX_MANAGER_SETTING_KEYS, default_values
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cloud_agent_host.sandbox import TerminalProcess

#: Its records keep the sandbox manager's logger name, which they had before
#: this module was split from it: a log selection by name must not lose them.
logger = get_logger("cloud_agent_host.sandbox_manager")

_DEFAULT_SETTINGS = default_values(SANDBOX_MANAGER_SETTING_KEYS)
MAX_TERMINAL_BUFFER_BYTES = _DEFAULT_SETTINGS["terminal_buffer_max_mib"] * 1024 * 1024
MAX_TERMINAL_BUFFER_EVENTS = _DEFAULT_SETTINGS["terminal_buffer_max_events"]
MAX_TERMINAL_TOTAL_OUTPUT_BYTES = _DEFAULT_SETTINGS["terminal_output_max_mib"] * 1024 * 1024
MAX_TERMINAL_STREAMS_PER_SESSION = _DEFAULT_SETTINGS["terminal_max_streams_per_session"]


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
        secrets: dict[str, str] | None = None,
        on_result: Callable[[dict[str, Any]], None] | None = None,
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
        self.operation_uid: int | None = None
        self._summary_redactor = SecretRedactor(secrets or {})
        self._summary_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._safe_output = ""
        self._exit_code: int | None = None
        self._workdir = workdir
        self._on_closed = on_closed
        self._on_result = on_result
        self._activity_released = False
        self._events: deque[_TerminalEvent] = deque()
        self._buffer_bytes = 0
        self._last_seq = 0
        self._condition = threading.Condition(threading.RLock())
        # Orders input against suspension. Both wait on the process -- a PTY
        # write blocks while the shell reads nothing, and a signal is a Docker
        # exec -- so neither may hold the condition, which the output pump and
        # every viewer, on the event loop too, take.
        self._control_lock = threading.Lock()
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

    def safe_output(self) -> dict[str, Any]:
        """Read an already masked bounded summary, separate from owner PTY replay."""
        with self._condition:
            return {**self.descriptor(), "output": self._safe_output, "exit_code": self._exit_code}

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
            return not self._closed and (
                self.state == "cleanup_failed"
                or self._outlived_locked(now=now, idle_ttl_s=idle_ttl_s, hard_ttl_s=hard_ttl_s)
            )

    def lapsed(self, *, now: float, idle_ttl_s: float, hard_ttl_s: float) -> bool:
        """Whether the session outlived a lifetime and only the reaper's close is left.

        A request treats it as gone, as it did when it closed the session
        itself. One whose cleanup failed is not lapsed: it still holds its
        process, and stays listed so that a kill can retry it.
        """
        with self._condition:
            return (
                not self._closed
                and self.state != "cleanup_failed"
                and self._outlived_locked(now=now, idle_ttl_s=idle_ttl_s, hard_ttl_s=hard_ttl_s)
            )

    def _outlived_locked(self, *, now: float, idle_ttl_s: float, hard_ttl_s: float) -> bool:
        effective_idle_ttl_s = (
            min(idle_ttl_s, self._idle_ttl_s) if self._idle_ttl_s is not None else idle_ttl_s
        )
        return (
            now - self.last_active_at >= effective_idle_ttl_s or now - self.created_at >= hard_ttl_s
        )

    def write(self, data: bytes, *, now: float) -> dict[str, Any]:
        """Write terminal input, rejecting writes after exit/close."""
        with self._control_lock:
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
        if self._on_result is not None:
            try:
                self._on_result(self.safe_output())
            except Exception:
                # The durable launch tombstone still prevents a replay. A
                # failed result write must not retain the workspace activity
                # token after the process has already exited.
                logger.warning("workspace_action_result_persist_failed")
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
        with self._control_lock:
            with self._condition:
                if self.state != "running" or self._closed or self._suspended:
                    return
                self._suspended = True
            try:
                self._process.suspend()
            except (SandboxError, OSError):
                with self._condition:
                    self._suspended = False
                raise

    def resume(self) -> None:
        """Resume a process tree frozen for workspace preparation or capture."""
        with self._control_lock:
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
            with self._condition:
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
            self._safe_output = (
                self._safe_output + self._summary_redactor.feed(self._summary_decoder.decode(data))
            )[-65536:]
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
            self._exit_code = exit_code
            self._safe_output = (
                self._safe_output
                + self._summary_redactor.feed(
                    self._summary_decoder.decode(b"", final=True), final=True
                )
            )[-65536:]
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
        if self.operation_uid is not None:
            try:
                self._process.kill()
            except (SandboxError, OSError):
                exit_code = 1
        self._append_exit(exit_code)
        self._release_activity()
