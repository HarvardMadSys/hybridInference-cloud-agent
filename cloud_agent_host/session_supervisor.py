"""Host-singleton lifecycle owner for restartable chat sandboxes.

Runner replicas may claim turns independently, but only this supervisor is
allowed to decide whether a chat container is active or idle.  Its lease is a
local concurrency guard in addition to the control plane's attempt fence: one
thread cannot have two admitted turns, and an idle sweep cannot stop a
container while a turn or terminal is using it.

The durable authority remains Docker plus the control-plane sandbox row.  The
in-memory entries here are reconstructable from ownership labels; restarting
the supervisor never removes or eagerly stops a retained container.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from cloud_agent_host.chat_sandbox import (
    CHAT_CPU_LIMIT_LABEL,
    CHAT_GENERATION_LABEL,
    CHAT_IMAGE_DIGEST_LABEL,
    CHAT_MEMORY_LIMIT_LABEL,
    CHAT_RUNTIME_LABEL,
    CHAT_RUNTIME_VERSION_LABEL,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SHARED_LABEL,
    CHAT_STOP_TIMEOUT_LABEL,
    CHAT_THREAD_LABEL,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatBackend,
    RetainedChatLost,
)
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from collections.abc import Callable


DEFAULT_CHAT_IDLE_TIMEOUT_S = 30 * 60.0
DEFAULT_SWEEP_INTERVAL_S = 5.0
ContextStatus = Literal["new", "live", "restored", "lost"]


class SessionBusy(SandboxError):
    """Another fenced owner is already using this chat."""


class SessionLeaseLost(SandboxError):
    """A local operation carried a stale or unknown activation lease."""


@dataclass(frozen=True)
class SessionLease:
    """Receipt proving one caller currently owns a chat activation window."""

    thread_id: str
    sandbox_id: str
    container_id: str
    container_name: str
    token: str
    activation_generation: int
    context_status: ContextStatus


@dataclass(frozen=True)
class SessionDescriptor:
    """Owner-safe lifecycle state returned by status and settle operations."""

    thread_id: str
    sandbox_id: str
    generation: int
    container_id: str
    container_name: str
    docker_status: str
    running: bool
    busy: bool
    terminal_count: int
    operation_count: int
    workspace_suspended: bool
    activation_generation: int
    process_generation: int
    last_settled_job_id: str | None
    idle_deadline_monotonic: float | None
    context_status: ContextStatus


@dataclass
class _SessionEntry:
    spec: ChatSandboxSpec
    inspection: ChatSandboxInspection
    activation_generation: int = 0
    process_generation: int = 0
    lease_token: str | None = None
    lease_owner: str | None = None
    terminal_tokens: set[str] = field(default_factory=set)
    operation_tokens: set[str] = field(default_factory=set)
    workspace_suspended: bool = False
    last_settled_job_id: str | None = None
    idle_deadline: float | None = None
    context_status: ContextStatus = "new"


class ChatSessionSupervisor:
    """Serialize chat use and stop settled containers after the idle timeout."""

    def __init__(
        self,
        manager: ChatSandboxManager,
        *,
        idle_timeout_s: float = DEFAULT_CHAT_IDLE_TIMEOUT_S,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_timeout_s <= 0:
            raise ValueError("chat sandbox idle timeout must be positive")
        if sweep_interval_s <= 0:
            raise ValueError("chat sandbox sweep interval must be positive")
        self._manager = manager
        self._idle_timeout_s = idle_timeout_s
        self._sweep_interval_s = sweep_interval_s
        self._clock = clock
        self._entries: dict[str, _SessionEntry] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def idle_timeout_s(self) -> float:
        """Return the settled-idle delay before a clean Docker stop."""
        return self._idle_timeout_s

    def start(self) -> None:
        """Reconcile Docker state and start the local idle reaper."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.reconcile()
            # A graceful broker shutdown closes every PTY. After a process or
            # host crash, Docker may retain an attached exec that this new
            # broker cannot stream or account for. Reap only exact terminal
            # wrappers before accepting traffic; chat containers and Agent
            # processes are otherwise left untouched.
            for entry in self._entries.values():
                self._manager.reap_stale_terminal_processes(entry.spec)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._sweep_loop,
                daemon=True,
                name="chat-sandbox-idle-sweep",
            )
            self._thread.start()

    def close(self) -> None:
        """Stop supervision only; retained containers deliberately survive."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._sweep_interval_s + 1)
        self._thread = None

    def reconcile(self) -> list[SessionDescriptor]:
        """Discover owned Docker containers without changing their lifecycle."""
        found: list[SessionDescriptor] = []
        for inspection in self._manager.list():
            # Shared-live containers have their own group/member supervisor.
            # Adopting one here as a legacy 1:1 chat would create two local
            # lifecycle authorities for the same Docker object.
            if inspection.labels.get(CHAT_SHARED_LABEL) == "true":
                continue
            spec = self._spec_from_inspection(inspection)
            with self._lock:
                existing = self._entries.get(spec.thread_id)
                if existing is not None and existing.spec.sandbox_id != spec.sandbox_id:
                    raise SandboxError(
                        f"thread {spec.thread_id!r} has more than one retained sandbox"
                    )
                if existing is None:
                    existing = _SessionEntry(spec=spec, inspection=inspection)
                    # A supervisor restart cannot distinguish a settled-idle
                    # container from one whose hour-long turn survived the
                    # broker restart. Never invent an idle deadline here: the
                    # host heartbeat asks the durable control-plane ledger for
                    # an exact, fenced stop command when its recorded deadline
                    # is actually due.
                    existing.idle_deadline = None
                    self._entries[spec.thread_id] = existing
                else:
                    existing.inspection = inspection
                found.append(self._descriptor(existing))
        return found

    def acquire(
        self,
        spec: ChatSandboxSpec,
        *,
        owner: str,
        has_settled_context: bool,
        activation_generation: int | None = None,
        live_process_proven: bool = False,
        expected_container_id: str | None = None,
    ) -> SessionLease:
        """Create/start a chat and reserve it for exactly one attempt owner."""
        if not owner:
            raise ValueError("session lease owner must be non-empty")
        with self._lock:
            entry = self._entries.get(spec.thread_id)
            if entry is not None and entry.spec != spec:
                raise SandboxError("chat sandbox immutable identity/configuration changed")
            if entry is not None and entry.lease_token is not None:
                if entry.lease_owner == owner:
                    return self._lease(entry)
                raise SessionBusy(f"chat {spec.thread_id!r} already has an active turn")
            if entry is not None and entry.workspace_suspended:
                raise SessionBusy(f"chat {spec.thread_id!r} workspace is suspended")
            if entry is None:
                inspection = (
                    self._manager.require_existing(spec, container_id=expected_container_id)
                    if expected_container_id is not None
                    else self._manager.create(spec)
                )
                entry = _SessionEntry(spec=spec, inspection=inspection)
                self._entries[spec.thread_id] = entry

            current = self._manager.inspect(spec.container_name)
            if current is None:
                entry.context_status = "lost"
                if expected_container_id is not None:
                    raise RetainedChatLost("retained chat container is missing")
                raise SandboxError("chat sandbox is missing")
            if expected_container_id is not None and current.container_id != expected_container_id:
                entry.context_status = "lost"
                raise RetainedChatLost(
                    "retained chat container id does not match its durable sandbox record"
                )
            entry.inspection = current
            was_running = entry.inspection.running
            entry.inspection = self._manager.start(spec)
            if activation_generation is not None and activation_generation < 1:
                raise ValueError("activation generation must be positive")
            if (
                activation_generation is not None
                and entry.activation_generation > activation_generation
            ):
                raise SessionLeaseLost("chat activation generation is stale")
            if activation_generation is not None:
                # The control-plane sandbox row is the durable counter. This
                # process may have reconstructed from Docker labels after a
                # restart, so it adopts (but never moves backwards from) that
                # fenced value instead of inventing a parallel counter.
                entry.activation_generation = activation_generation
            elif not was_running:
                entry.activation_generation += 1
            elif entry.activation_generation == 0:
                # Reconciled RUNNING state predates this process.  Zero is not
                # a usable fence, so the first owner adopts it as generation 1.
                entry.activation_generation = 1
            entry.lease_token = secrets.token_urlsafe(32)
            entry.lease_owner = owner
            entry.idle_deadline = None
            # Context state is the product contract, not a PID diagnostic.
            # LIVE means the same already-running chat sandbox still owned its
            # native state; RESTORED means Docker had to start the stopped
            # sandbox before the CLI resumed that state. ``live_process_proven``
            # remains a source-compatible request field during rollout, but no
            # longer changes this correctness status.
            _ = live_process_proven
            entry.context_status = (
                "live"
                if has_settled_context and was_running
                else ("restored" if has_settled_context else "new")
            )
            return self._lease(entry)

    def note_process_started(self, thread_id: str, token: str) -> SessionDescriptor:
        """Advance the diagnostic CLI-process generation under the active lease."""
        with self._lock:
            entry = self._leased_entry(thread_id, token)
            entry.process_generation += 1
            return self._descriptor(entry)

    def settle(
        self,
        thread_id: str,
        token: str,
        *,
        job_id: str,
        context_status: ContextStatus,
    ) -> SessionDescriptor:
        """Release a successful turn and begin its settled-idle window."""
        if context_status == "lost":
            raise ValueError("a lost context cannot be recorded as a settled turn")
        with self._lock:
            entry = self._leased_entry(thread_id, token)
            entry.last_settled_job_id = job_id
            entry.context_status = context_status
            entry.lease_token = None
            entry.lease_owner = None
            entry.idle_deadline = self._clock() + self._idle_timeout_s
            return self._descriptor(entry)

    def abandon(self, thread_id: str, token: str, *, lost: bool) -> SessionDescriptor:
        """End a failed activation and stop its container immediately.

        A failed turn has no settled idle boundary to protect. Stopping now
        releases every process spawned under the activation and makes a safe
        pre-admission retry restart from a quiescent Docker state. An admitted
        failure remains explicitly LOST.
        """
        with self._lock:
            entry = self._leased_entry(thread_id, token)
            current = self._manager.inspect(entry.spec.container_name)
            if current is None:
                entry.context_status = "lost"
                raise SandboxError("chat sandbox is missing")
            entry.inspection = current
            if current.running:
                entry.inspection = self._manager.stop(entry.spec)
            entry.lease_token = None
            entry.lease_owner = None
            entry.terminal_tokens.clear()
            entry.operation_tokens.clear()
            entry.workspace_suspended = False
            entry.context_status = "lost" if lost else entry.context_status
            entry.idle_deadline = None
            return self._descriptor(entry)

    def terminal_opened(self, thread_id: str) -> tuple[str, SessionDescriptor]:
        """Prevent idle stop while an interactive terminal is attached."""
        with self._lock:
            entry = self._entry(thread_id)
            if entry.workspace_suspended:
                raise SessionBusy(f"chat {thread_id!r} workspace is suspended")
            current = self._manager.inspect(entry.spec.container_name)
            if current is None:
                entry.context_status = "lost"
                raise SandboxError("chat sandbox is missing")
            entry.inspection = current
            if not current.running:
                entry.inspection = self._manager.start(entry.spec)
            terminal_token = secrets.token_urlsafe(24)
            entry.terminal_tokens.add(terminal_token)
            entry.idle_deadline = None
            return terminal_token, self._descriptor(entry)

    def terminal_closed(
        self,
        thread_id: str,
        terminal_token: str,
        *,
        last_activity_monotonic: float | None = None,
    ) -> SessionDescriptor:
        """Release a terminal and continue the one chat-idle clock.

        An idle PTY may be reaped only after a full inactivity window. Starting
        a second full window at reap time would make one 30-minute product
        timeout behave like 60 minutes. The terminal manager therefore passes
        its last activity timestamp; explicit/manual closes omit it and count
        as fresh user activity.
        """
        with self._lock:
            entry = self._entry(thread_id)
            if terminal_token not in entry.terminal_tokens:
                raise SessionLeaseLost("terminal lifecycle token is stale")
            now = self._clock()
            activity_at = now
            if last_activity_monotonic is not None:
                activity_at = float(last_activity_monotonic)
                if activity_at < 0 or activity_at != activity_at:
                    raise ValueError("terminal last activity timestamp is invalid")
                # Both clocks are host monotonic values, but cap a tiny
                # scheduling skew rather than extending the product clock.
                activity_at = min(activity_at, now)

            # Validate the timestamp before consuming the lifecycle token so
            # a malformed internal callback cannot leave the session half
            # released.
            entry.terminal_tokens.remove(terminal_token)
            if (
                not entry.terminal_tokens
                and not entry.operation_tokens
                and entry.lease_token is None
                and not entry.workspace_suspended
            ):
                entry.idle_deadline = activity_at + self._idle_timeout_s
            return self._descriptor(entry)

    def operation_opened(self, thread_id: str) -> tuple[str, SessionDescriptor]:
        """Keep a container active for one bounded file/Git/command operation."""
        with self._lock:
            entry = self._entry(thread_id)
            if entry.workspace_suspended:
                raise SessionBusy(f"chat {thread_id!r} workspace is suspended")
            current = self._manager.inspect(entry.spec.container_name)
            if current is None:
                entry.context_status = "lost"
                raise SandboxError("chat sandbox is missing")
            entry.inspection = current
            if not current.running:
                entry.inspection = self._manager.start(entry.spec)
            token = secrets.token_urlsafe(24)
            entry.operation_tokens.add(token)
            entry.idle_deadline = None
            return token, self._descriptor(entry)

    def operation_closed(self, thread_id: str, token: str) -> SessionDescriptor:
        """Release one bounded operation and re-arm idle stop when fully idle."""
        with self._lock:
            entry = self._entry(thread_id)
            if token not in entry.operation_tokens:
                raise SessionLeaseLost("chat operation lifecycle token is stale")
            entry.operation_tokens.remove(token)
            self._condition.notify_all()
            if (
                not entry.operation_tokens
                and not entry.terminal_tokens
                and entry.lease_token is None
                and not entry.workspace_suspended
            ):
                entry.idle_deadline = self._clock() + self._idle_timeout_s
            return self._descriptor(entry)

    def suspend_workspace(self, thread_id: str) -> SessionDescriptor:
        """Prevent new file, Git, command, and terminal operations."""
        with self._lock:
            entry = self._entry(thread_id)
            entry.workspace_suspended = True
            entry.idle_deadline = None
            return self._descriptor(entry)

    def wait_workspace_operations(
        self,
        thread_id: str,
        *,
        timeout_s: float = 30.0,
    ) -> SessionDescriptor:
        """Wait until bounded operations admitted before suspension drain."""
        if timeout_s <= 0:
            raise ValueError("workspace suspension timeout must be positive")
        deadline = time.monotonic() + timeout_s
        with self._condition:
            entry = self._entry(thread_id)
            if not entry.workspace_suspended:
                raise SessionLeaseLost("chat workspace suspension is no longer active")
            while entry.operation_tokens:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionBusy(f"chat {thread_id!r} workspace operation did not drain")
                self._condition.wait(timeout=remaining)
            return self._descriptor(entry)

    def resume_workspace(self, thread_id: str) -> SessionDescriptor:
        """Allow workspace operations after a protected runner phase."""
        with self._lock:
            entry = self._entry(thread_id)
            entry.workspace_suspended = False
            if (
                entry.lease_token is None
                and not entry.terminal_tokens
                and not entry.operation_tokens
                and entry.inspection.running
            ):
                entry.idle_deadline = self._clock() + self._idle_timeout_s
            return self._descriptor(entry)

    def backend(self, thread_id: str) -> RetainedChatBackend:
        """Return a Docker-exec backend for a currently supervised chat."""
        with self._lock:
            entry = self._entry(thread_id)
            return RetainedChatBackend(self._manager, entry.spec)

    def status(self, thread_id: str) -> SessionDescriptor:
        """Return a fresh Docker-observed descriptor for one retained chat."""
        with self._lock:
            entry = self._entry(thread_id)
            current = self._manager.inspect(entry.spec.container_name)
            if current is None:
                entry.context_status = "lost"
                raise SandboxError("chat sandbox is missing")
            entry.inspection = current
            return self._descriptor(entry)

    def stop_if_idle(
        self,
        thread_id: str,
        *,
        generation: int,
        activation_generation: int,
        reason: Literal["idle", "orphaned_attempt", "lost"] = "idle",
    ) -> SessionDescriptor:
        """Apply a control-plane-authorized stop under exact durable fences."""
        with self._lock:
            entry = self._entry(thread_id)
            if entry.spec.generation != generation:
                raise SessionLeaseLost("chat container generation is stale")
            if entry.activation_generation not in {0, activation_generation}:
                raise SessionLeaseLost("chat activation generation is stale")
            if reason == "idle":
                if entry.workspace_suspended:
                    raise SessionBusy(f"chat {thread_id!r} workspace is suspended")
                if entry.lease_token is not None:
                    raise SessionBusy(f"chat {thread_id!r} has an active turn")
                if entry.terminal_tokens or entry.operation_tokens:
                    raise SessionBusy(f"chat {thread_id!r} has active workspace users")
            else:
                # The control plane has already superseded the exact attempt
                # generation. Any in-memory tokens reconstructed by this
                # broker process belong to that dead activation and must not
                # keep the container alive or block a pre-admission retry.
                entry.lease_token = None
                entry.lease_owner = None
                entry.terminal_tokens.clear()
                entry.operation_tokens.clear()
                entry.workspace_suspended = False
                if reason == "lost":
                    entry.context_status = "lost"
            if entry.activation_generation == 0:
                # Reconstructed from immutable Docker labels; the durable row
                # supplies the generation the local process could not know.
                entry.activation_generation = activation_generation
            current = self._manager.inspect(entry.spec.container_name)
            if current is None:
                entry.context_status = "lost"
                raise SandboxError("chat sandbox is missing")
            entry.inspection = current
            if current.running:
                entry.inspection = self._manager.stop(entry.spec)
            entry.idle_deadline = None
            return self._descriptor(entry)

    def sweep(self) -> list[SessionDescriptor]:
        """Stop only settled, unused containers whose idle deadline elapsed."""
        now = self._clock()
        stopped: list[SessionDescriptor] = []
        with self._lock:
            candidates = [
                entry
                for entry in self._entries.values()
                if entry.inspection.running
                and entry.lease_token is None
                and not entry.terminal_tokens
                and not entry.operation_tokens
                and not entry.workspace_suspended
                and entry.idle_deadline is not None
                and entry.idle_deadline <= now
            ]
            for entry in candidates:
                # The same lock guards acquire/terminal_opened, so nobody can
                # become active between the predicate and docker stop.
                entry.inspection = self._manager.stop(entry.spec)
                entry.idle_deadline = None
                stopped.append(self._descriptor(entry))
        return stopped

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self._sweep_interval_s):
            try:
                self.sweep()
            except Exception:
                # The caller-facing operation will surface Docker errors.  A
                # transient reaper failure must not kill the singleton and
                # silently disable every future timeout.
                continue

    def _entry(self, thread_id: str) -> _SessionEntry:
        entry = self._entries.get(thread_id)
        if entry is None:
            raise SandboxError(f"chat {thread_id!r} has no retained sandbox")
        return entry

    def _leased_entry(self, thread_id: str, token: str) -> _SessionEntry:
        entry = self._entry(thread_id)
        if not token or entry.lease_token != token:
            raise SessionLeaseLost("chat activation lease is stale")
        return entry

    @staticmethod
    def _lease(entry: _SessionEntry) -> SessionLease:
        assert entry.lease_token is not None
        return SessionLease(
            thread_id=entry.spec.thread_id,
            sandbox_id=entry.spec.sandbox_id,
            container_id=entry.inspection.container_id,
            container_name=entry.inspection.name,
            token=entry.lease_token,
            activation_generation=entry.activation_generation,
            context_status=entry.context_status,
        )

    @staticmethod
    def _descriptor(entry: _SessionEntry) -> SessionDescriptor:
        return SessionDescriptor(
            thread_id=entry.spec.thread_id,
            sandbox_id=entry.spec.sandbox_id,
            generation=entry.spec.generation,
            container_id=entry.inspection.container_id,
            container_name=entry.inspection.name,
            docker_status=entry.inspection.status.value,
            running=entry.inspection.running,
            busy=entry.lease_token is not None,
            terminal_count=len(entry.terminal_tokens),
            operation_count=len(entry.operation_tokens),
            workspace_suspended=entry.workspace_suspended,
            activation_generation=entry.activation_generation,
            process_generation=entry.process_generation,
            last_settled_job_id=entry.last_settled_job_id,
            idle_deadline_monotonic=entry.idle_deadline,
            context_status=entry.context_status,
        )

    @staticmethod
    def _spec_from_inspection(inspection: ChatSandboxInspection) -> ChatSandboxSpec:
        labels = inspection.labels
        try:
            return ChatSandboxSpec(
                thread_id=labels[CHAT_THREAD_LABEL],
                sandbox_id=labels[CHAT_SANDBOX_ID_LABEL],
                generation=int(labels[CHAT_GENERATION_LABEL]),
                runtime_id=labels[CHAT_RUNTIME_LABEL],
                runtime_version=labels.get(CHAT_RUNTIME_VERSION_LABEL) or None,
                sandbox_image_digest=labels.get(CHAT_IMAGE_DIGEST_LABEL) or None,
                memory_limit=labels[CHAT_MEMORY_LIMIT_LABEL],
                cpu_limit=labels[CHAT_CPU_LIMIT_LABEL],
                stop_timeout_s=int(labels[CHAT_STOP_TIMEOUT_LABEL]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxError(
                f"retained container {inspection.name!r} has incomplete identity labels"
            ) from exc


__all__ = [
    "DEFAULT_CHAT_IDLE_TIMEOUT_S",
    "DEFAULT_SWEEP_INTERVAL_S",
    "ChatSessionSupervisor",
    "ContextStatus",
    "SessionBusy",
    "SessionDescriptor",
    "SessionLease",
    "SessionLeaseLost",
]
