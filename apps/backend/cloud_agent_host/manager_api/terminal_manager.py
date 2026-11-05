"""The registry of interactive terminals inside retained chat containers."""

from __future__ import annotations

import contextlib
import re
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent_host.action_receipts import ActionReceipts
from cloud_agent_host.chat_sandbox import RetainedChatBackend
from cloud_agent_host.environment_process import tool_environment
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.sandbox import SandboxError, SandboxSpec, TerminalNotSupportedError
from cloud_agent_host.terminal_session import _TerminalLimits, _TerminalSession
from cloud_agent_shared.host_settings import SANDBOX_MANAGER_SETTING_KEYS, default_values

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cloud_agent_host.sandbox import SandboxBackend, TerminalProcess
    from cloud_agent_shared.environment import BoundEnvironment, EnvironmentAction

# The sandbox manager's own logger: its terminal events keep their name.
logger = get_logger("cloud_agent_host.sandbox_manager")

_DEFAULT_SETTINGS = default_values(SANDBOX_MANAGER_SETTING_KEYS)
MAX_TERMINAL_SESSIONS_PER_WORKSPACE = _DEFAULT_SETTINGS["terminal_max_sessions_per_workspace"]
DEFAULT_MAX_TERMINAL_SESSIONS = _DEFAULT_SETTINGS["terminal_max_sessions"]
DEFAULT_TERMINAL_IDLE_TTL_S = float(_DEFAULT_SETTINGS["terminal_idle_ttl_s"])
DEFAULT_TERMINAL_HARD_TTL_S = float(_DEFAULT_SETTINGS["terminal_hard_ttl_s"])


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
        action_receipts: ActionReceipts | None = None,
    ) -> None:
        self._action_receipts = action_receipts or ActionReceipts()
        self._idle_ttl_s = idle_ttl_s
        self._hard_ttl_s = hard_ttl_s
        self._max_sessions = max_sessions
        self._max_sessions_per_workspace = max_sessions_per_workspace
        self._limits = limits or _TerminalLimits()
        self._sessions: dict[str, dict[str, _TerminalSession]] = {}
        self._suspended_workspaces: set[str] = set()
        self._workspace_suspensions: dict[str, set[str]] = {}
        self._lock = threading.RLock()
        # One workspace's suspensions, resumptions and creations still run one
        # at a time, but under a lock of that workspace's own, kept only while
        # a caller holds or waits for it: all three wait on Docker, and
        # ``_lock`` is taken by every terminal request on the sandbox manager.
        self._workspace_locks: dict[str, tuple[threading.Lock, int]] = {}
        # Terminals admitted but not listed yet: each counts against the
        # global cap while its spawn runs without ``_lock``.
        self._spawning = 0
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
        for session in sessions:
            try:
                session.close(mark_activity=False)
            except (SandboxError, OSError):
                logger.exception("workspace_terminal_shutdown_cleanup_failed")
        if self._reaper is not None:
            self._reaper.join(timeout=5)

    @contextlib.contextmanager
    def _serialized(self, workspace_id: str) -> Iterator[None]:
        """Run one suspension, resumption or creation of a workspace at a time.

        Suspension and resumption signal every terminal through Docker, and
        creation spawns one, which can take seconds each, so they hold this
        workspace's own lock across those calls and ``_lock`` only for
        bookkeeping: no other workspace's terminal request waits behind them.
        A delete that finds no terminal waits here too, for a creation that
        may be spawning it.
        """
        with self._lock:
            lock, holders = self._workspace_locks.get(workspace_id) or (threading.Lock(), 0)
            self._workspace_locks[workspace_id] = (lock, holders + 1)
        try:
            with lock:
                yield
        finally:
            with self._lock:
                lock, holders = self._workspace_locks.pop(workspace_id)
                if holders > 1:
                    self._workspace_locks[workspace_id] = (lock, holders - 1)

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
        environment: BoundEnvironment | None = None,
        action_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one PTY while atomically enforcing the per-workspace cap.

        Resolving an action's directory and spawning the PTY are Docker calls
        that can take seconds, so they hold only this workspace's own lock,
        which keeps its creations one at a time. ``_lock``, which every
        terminal request on the sandbox manager takes, covers only the
        admission, which reserves the new terminal's place under the global
        cap, and the listing once it is spawned.
        """
        self.reap()
        terminal_id = "term_" + (run_id or secrets.token_urlsafe(18))
        now = time.monotonic()
        evicted: list[_TerminalSession] = []
        try:
            # After a suspension or resumption of this workspace in progress,
            # as when one lock covered both, rather than in its middle.
            with self._serialized(workspace_id):
                with self._lock:
                    if workspace_id in self._suspended_workspaces:
                        raise HTTPException(
                            status_code=409, detail="terminal workspace is not ready"
                        )
                    group = self._sessions.setdefault(workspace_id, {})
                    if terminal_id in group:
                        if on_closed is not None:
                            on_closed(now)
                        return group[terminal_id].descriptor()
                    self._refuse_over_limit_locked(workspace_id, group)
                    action = (
                        next((a for a in environment.actions if a.id == action_id), None)
                        if environment and action_id
                        else None
                    )
                    if action_id and action is None:
                        raise HTTPException(404, "Action not found in workspace binding")
                    operation_uids = {getattr(s, "operation_uid", None) for s in group.values()}
                    self._spawning += 1
                spawning = True
                try:
                    if action is not None:
                        workdir = self._action_workdir(backend, action)
                    spec = SandboxSpec(
                        argv=["/bin/sh", "-lc", action.command] if action else ["/bin/sh"],
                        workdir=str(workdir),
                        env={
                            **backend.base_env(),
                            "GIT_CONFIG_NOSYSTEM": "1",
                            "TERM": "xterm-256color",
                            **(tool_environment() if environment else {}),
                            **(environment.values("runtime") if environment else {}),
                            **(
                                {
                                    "AGENT_ACTION_UID": str(
                                        31000
                                        + next(
                                            i for i in range(64) if 31000 + i not in operation_uids
                                        )
                                    )
                                }
                                if action
                                else {}
                            ),
                        },
                    )
                    previous = self._reserve_action_run(
                        workspace_id,
                        terminal_id,
                        environment,
                        action,
                        on_closed=on_closed,
                        now=now,
                    )
                    if previous is not None:
                        return previous
                    process = self._spawn_terminal_process(backend, spec, rows=rows, cols=cols)
                    session = _TerminalSession(
                        terminal_id=terminal_id,
                        process=process,
                        workdir=workdir if isinstance(workdir, Path) else None,
                        rows=rows,
                        cols=cols,
                        now=now,
                        idle_ttl_s=idle_ttl_s,
                        on_closed=on_closed,
                        secrets=environment.secrets() if environment else {},
                        limits=self._limits,
                        on_result=(
                            lambda result: self._action_receipts.finish(
                                workspace_id, terminal_id, result
                            )
                        )
                        if action
                        else None,
                    )
                    session.operation_uid = int(spec.env["AGENT_ACTION_UID"]) if action else None
                    # Draining before it is listed: whoever finds it listed, a
                    # delete waiting on this workspace among them, may close
                    # it, and closing joins the pump.
                    session.start()
                    with self._lock:
                        self._spawning -= 1
                        spawning = False
                        # A reap or a delete may have unlisted this workspace
                        # while it had no terminal.
                        group = self._sessions.setdefault(workspace_id, {})
                        evicted.extend(self._unlist_exited_locked(workspace_id))
                        group[terminal_id] = session
                finally:
                    if spawning:
                        with self._lock:
                            self._spawning -= 1
        finally:
            for stale in evicted:
                try:
                    stale.close(mark_activity=False)
                except (SandboxError, OSError):
                    logger.exception("workspace_terminal_eviction_cleanup_failed")
        return session.descriptor()

    def _refuse_over_limit_locked(
        self, workspace_id: str, group: dict[str, _TerminalSession]
    ) -> None:
        """Refuse a terminal over the sandbox manager's or the workspace's limit.

        The caller holds ``_lock``.
        """
        global_active = self._spawning + sum(
            session.holds_process
            for sessions in self._sessions.values()
            for session in sessions.values()
        )
        if global_active >= self._max_sessions:
            if not group:
                self._sessions.pop(workspace_id, None)
            raise HTTPException(status_code=503, detail="sandbox manager terminal limit reached")
        active = sum(session.holds_process for session in group.values())
        if active >= self._max_sessions_per_workspace:
            raise HTTPException(status_code=409, detail="workspace terminal limit reached")

    @staticmethod
    def _action_workdir(backend: SandboxBackend, action: EnvironmentAction) -> str:
        """Resolve an action's directory, refusing one that escapes the workspace.

        The caller holds the workspace's own lock.
        """
        workdir = "/workspace/" + action.cwd
        if isinstance(backend, RetainedChatBackend):
            resolved = backend.manager.exec_capture(
                backend.spec,
                ["realpath", "-e", "--", str(workdir)],
                workdir="/workspace",
                timeout_s=10,
            )
            normalized = resolved.stdout.decode().strip()
            if resolved.returncode or not (
                normalized == "/workspace" or normalized.startswith("/workspace/")
            ):
                raise HTTPException(422, "Action directory escapes workspace")
            workdir = normalized
        return workdir

    def _reserve_action_run(
        self,
        workspace_id: str,
        terminal_id: str,
        environment: BoundEnvironment | None,
        action: EnvironmentAction | None,
        *,
        on_closed: Callable[[float], None] | None,
        now: float,
    ) -> dict[str, Any] | None:
        """Persist an action run's identity, or return the receipt of the run it repeats.

        The caller holds the workspace's own lock.
        """
        if action is not None and environment is not None:
            previous = self._action_receipts.reserve(
                workspace_id, terminal_id, environment.binding_id, action.id
            )
            if previous is not None:
                if on_closed is not None:
                    on_closed(now)
                return previous
        return None

    @staticmethod
    def _spawn_terminal_process(
        backend: SandboxBackend, spec: SandboxSpec, *, rows: int, cols: int
    ) -> TerminalProcess:
        """Spawn the PTY, reporting why the backend could not."""
        try:
            return backend.spawn_terminal(spec, rows=rows, cols=cols)
        except TerminalNotSupportedError as exc:
            raise HTTPException(
                status_code=409,
                detail=("Interactive terminals require the container or kata sandbox backend."),
            ) from exc
        except (OSError, SandboxError) as exc:
            raise HTTPException(status_code=502, detail="terminal could not be started") from exc

    def _unlist_exited_locked(self, workspace_id: str) -> list[_TerminalSession]:
        """Unlist the oldest exited shells so one more terminal fits both limits.

        An exited shell stays listed for its output and keeps up to a full
        output buffer, so it still takes room under both limits, but only a
        running shell can refuse a new terminal. The new terminal's workspace
        gives up its own oldest exited shells while its list is full; then,
        while the sandbox manager as a whole is full, the oldest anywhere go. The
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

    def suspend(self, workspace_id: str, *, job_id: str) -> dict[str, Any]:
        """Freeze PTYs and retain one independent token per protected turn."""
        with self._serialized(workspace_id):
            with self._lock:
                self._workspace_suspensions.setdefault(workspace_id, set()).add(job_id)
                # From here no terminal of this workspace takes input or opens.
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

    def resume(self, workspace_id: str, *, job_id: str) -> dict[str, Any]:
        """Release one turn token and resume PTYs only after the last release.

        A token the turn does not hold is released as a no-op, so a settled
        turn's release after its own resume, or a sibling's, thaws nothing.
        """
        with self._serialized(workspace_id):
            with self._lock:
                suspensions = self._workspace_suspensions.get(workspace_id)
                if not suspensions or job_id not in suspensions:
                    return {"ok": True, "resumed": 0}
                suspensions.remove(job_id)
                if suspensions:
                    return {"ok": True, "resumed": 0}
                sessions = list(self._sessions.get(workspace_id, {}).values())
            # The workspace stays fenced until every PTY has resumed: no terminal
            # of it takes input or opens before then.
            attempted: list[_TerminalSession] = []
            try:
                for session in sessions:
                    attempted.append(session)
                    session.resume()
            except (SandboxError, OSError) as exc:
                # Restore every PTY already resumed in this batch. A retry must
                # see both the logical workspace fence and the actual processes
                # suspended; otherwise one shell could mutate the shared tree
                # inside a quiescent capture after a sibling's resume failed.
                for session in attempted:
                    with contextlib.suppress(SandboxError, OSError):
                        session.suspend()
                with self._lock:
                    suspensions.add(job_id)
                raise HTTPException(
                    status_code=503,
                    detail="terminal resume could not be confirmed; retry resume",
                ) from exc
            with self._lock:
                self._workspace_suspensions.pop(workspace_id, None)
                self._suspended_workspaces.discard(workspace_id)
            return {"ok": True, "resumed": len(sessions)}

    def list(self, workspace_id: str) -> list[dict[str, Any]]:
        """List visible terminals for one workspace only."""
        now = time.monotonic()
        with self._lock:
            sessions = [
                session
                for session in self._sessions.get(workspace_id, {}).values()
                if not self._lapsed_locked(session, now)
            ]
        return [session.descriptor() for session in sessions if not session.is_closed]

    def get(self, workspace_id: str, terminal_id: str) -> _TerminalSession:
        """Resolve an opaque id inside its workspace namespace."""
        with self._lock:
            return self._find_locked(workspace_id, terminal_id)

    def _find_locked(self, workspace_id: str, terminal_id: str) -> _TerminalSession:
        """The open terminal a request names.

        One past either lifetime is already gone to a request, and closing it
        is the reaper's. So no keystroke waits on a teardown, its own
        terminal's or another workspace's: a hangup, up to three seconds of
        polling, then a kill.
        """
        session = self._sessions.get(workspace_id, {}).get(terminal_id)
        if session is None or session.is_closed or self._lapsed_locked(session, time.monotonic()):
            raise HTTPException(status_code=404, detail="terminal does not exist")
        return session

    def _lapsed_locked(self, session: _TerminalSession, now: float) -> bool:
        return session.lapsed(now=now, idle_ttl_s=self._idle_ttl_s, hard_ttl_s=self._hard_ttl_s)

    def output(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Read a live summary or the action's durable terminal result."""
        # Unlike a keystroke this still closes what has expired first: closing
        # an action's terminal is what records the result this read returns.
        self.reap()
        try:
            session = self.get(workspace_id, terminal_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            receipt = self._action_receipts.read(workspace_id, terminal_id)
            if receipt is None:
                raise
            return receipt
        output = session.safe_output()
        if session._suspended:
            output["state"] = "paused"
        return output

    def get_for_control(self, workspace_id: str, terminal_id: str) -> _TerminalSession:
        """Resolve a terminal only while its workspace-level fence is open."""
        with self._lock:
            session = self._find_locked(workspace_id, terminal_id)
            if workspace_id in self._suspended_workspaces:
                raise HTTPException(status_code=409, detail="terminal workspace is not ready")
            return session

    def delete(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Close a terminal, retaining it when cleanup needs to be retried."""
        with self._lock:
            session = self._sessions.get(workspace_id, {}).get(terminal_id)
        if session is None:
            # A creation in this workspace may have reserved this action's
            # receipt and still be spawning its terminal, holding the
            # workspace's own lock. The tombstone below leaves a reserved
            # receipt alone, so the stopped command would start once that
            # terminal is listed: wait for the creation, then look again.
            with self._serialized(workspace_id):
                with self._lock:
                    session = self._sessions.get(workspace_id, {}).get(terminal_id)
                if session is None:
                    # Action run ids are UUIDs; ephemeral interactive terminal ids are not.
                    if re.fullmatch(
                        r"term_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        terminal_id,
                    ):
                        self._action_receipts.stop(workspace_id, terminal_id)
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
