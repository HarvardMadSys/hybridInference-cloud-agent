"""Driver → Session → Turn: the platform's handle on a harness (design §5).

This is the object model the session-resident-threads design commits to
(``docs/specs/2026-08-17-session-resident-harness-threads-design.md``), layered
over the dialect adapters in :mod:`cloud_agent_host.runtimes`. The split of
labour is deliberate and mirrors the design's D7:

- **runtimes.py** stays the *dialect* layer — how one CLI is invoked and how
  one line of its output maps onto normalized events. It knows nothing about
  sessions or the platform.
- **drivers.py** is the *contract* layer the runner drives: ``launch`` a
  session, ``start_turn`` on it, iterate its events, ``wait`` for a
  :class:`TurnResult` whose guarantees the platform can lean on (D9).

One process may still carry only one turn without losing continuity. Codex,
pi, OpenCode and Kilo persist their own native session data and accept an
explicit session locator on the next process; Claude additionally implements
checkpoint export/restore and a live-input transport. PID lifetime is an
implementation detail — the contract reports whether the next turn receives
the native context, not whether Linux happened to keep the same child alive.

Synchronous by design: the runner is threaded, and the only transport v1
drivers own is container stdio. The design doc's async signatures become
meaningful with the live-input transport and arrive with it.
"""

from __future__ import annotations

import io
import json
import pathlib
import tarfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.runtimes import (
    DIFF,
    EMPTY_RUNTIME_MCP_CONFIG,
    ERROR,
    LIFECYCLE,
    MESSAGE,
    RAW,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    AgentRuntime,
    RuntimeMCPConfig,
    get_runtime,
)
from cloud_agent_host.sandbox import STATE_MOUNT_TARGET

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cloud_agent_host.sandbox import SandboxProcess

# Floor events (design D9): the minimum any driver must yield around a turn,
# synthesized here so a dialect adapter cannot forget them. They are the
# runner's control-flow signal, not stream rows — the platform's stored
# vocabulary stays the runtimes.py kinds.
TURN_STARTED = "turn_started"
TURN_COMPLETED = "turn_completed"
TURN_FAILED = "turn_failed"
FLOOR_EVENT_TYPES = frozenset({TURN_STARTED, TURN_COMPLETED, TURN_FAILED})

#: Yielded for a stdout line the dialect adapter had no event for. Carries the
#: raw line so the caller can keep its tail; never uploaded.
LINE = "line"


class DriverError(Exception):
    """Raised when a driver cannot honour a request."""


class StateRestoreUnsupported(DriverError):
    """``launch(state=...)`` before the driver implements restore (P2)."""


class StateExportUnsupported(DriverError):
    """``export_state()`` before the driver implements it (P2)."""


class ForkUnsupported(DriverError):
    """``fork()`` before the driver implements it (D8 v1: none do)."""


@dataclass(frozen=True)
class HarnessCapabilities:
    """What this harness can do, in the design's semantic vocabulary.

    Values state what is *implemented and wired*, never what the upstream CLI
    could theoretically do. Native resume and portable checkpoint export are
    separate facts: several pinned CLIs now do the former in the retained
    sandbox, while persistent-storage work for their state is intentionally a
    later milestone.
    """

    continuity: Literal["hot", "resume", "replay"] = "replay"
    #: Whether ``export_state`` produces a self-contained blob suitable for
    #: storage outside the current sandbox. Native on-disk resume does not
    #: imply this: the retained-container milestone deliberately lands first.
    checkpoint_export: bool = False
    steering: Literal["mid_turn", "queue_only", "none"] = "none"
    fork: Literal["latest_immediate", "at_latest", "at_anchor", "none"] = "none"
    #: Whether the host supervisor, rather than an individual runner turn,
    #: can own one bidirectional CLI transport across several messages.
    resident_transport: Literal["supervisor_live", "none"] = "none"
    #: A conformance claim, not an upstream marketing claim: two separately
    #: credentialed resident processes may safely operate in one workspace.
    concurrent_shared_workspace: bool = False
    # Replay rebuilds the conversation as text, so any turn may pick any
    # model — per-turn switching is the one thing the replay world does
    # *better* than sessions. Session-capable drivers will downgrade this to
    # "restart" (claude-code) as the design records.
    model_switch: Literal["per_turn", "restart", "none"] = "per_turn"


@dataclass(frozen=True)
class TurnInput:
    """One user turn: the prompt, the model, and how hard to think about it."""

    prompt: str
    model: str
    #: ``None`` is Auto — no flag, whatever the CLI and model do by default,
    #: which is what every turn did before this existed. A value has been
    #: checked against both halves of the pair by the control plane; the
    #: runtime refuses anything it cannot express rather than dropping it.
    #: Fixed at spawn like the model is, so a resident session changing it
    #: restarts (see :class:`ClaudeCodeLiveSession`).
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class TurnResult:
    """The platform-facing outcome of one turn (design D9).

    The hard guarantee: ``status == "completed"`` implies ``final_output`` is
    non-empty. A harness that produced only tool activity still yields a
    displayable line — synthesized by the driver — because an empty thread
    after a successful run is the pi-blank-thread bug, and this dataclass is
    where that class of bug is made unrepresentable.
    """

    status: Literal["completed", "failed", "cancelled"]
    final_output: str
    exit_code: int
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status == "completed" and not self.final_output.strip():
            raise ValueError("a completed turn must carry a non-empty final_output")


@dataclass(frozen=True)
class HarnessEvent:
    """One item of a turn's stream.

    Dialect events carry their ``raw_line``; floor events carry none. A line
    the adapter had no event for is yielded as :data:`LINE` so the caller can
    keep its tail without this layer deciding what to drop.
    """

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw_line: str | None = None

    @property
    def is_floor(self) -> bool:
        """Whether this is driver control flow rather than a stream row."""
        return self.event_type in FLOOR_EVENT_TYPES


@dataclass(frozen=True)
class HarnessState:
    """One exported harness session: the driver's opaque blob plus its name.

    The platform stores and transports this (encrypted, size-capped) inside a
    ThreadCheckpoint; only the driver that exported it can interpret ``blob``.
    """

    session_id: str
    blob: bytes


@dataclass(frozen=True)
class LaunchContext:
    """Everything a session needs that is not the turn itself.

    ``spawn`` is the sandbox boundary made explicit: the *platform* places the
    process inside the sandbox (D7 — no driver may create a harness process
    anywhere else), and the driver owns everything that flows over the
    returned handle's transport.
    """

    workdir: str
    gateway_base_url: str
    credential: str
    provides_isolation: bool
    spawn: Callable[[list[str], dict[str, str]], SandboxProcess]
    base_env: dict[str, str] = field(default_factory=dict)
    mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG
    #: Host directory the platform bind-mounts at the sandbox's state target.
    #: ``None`` disables session persistence (and with it export/restore).
    state_dir: str | None = None
    #: In-sandbox provider state root. Legacy mounts use /agent-state; a
    #: retained multi-member runtime may select a provider-compatible child
    #: directory without changing the host-side persistence signal above.
    session_state_dir: str = STATE_MOUNT_TARGET
    #: Spawns with a live stdin (SandboxSpec.live_input). ``None`` means this
    #: caller does not offer residency; ``launch_live`` refuses without it.
    spawn_live: Callable[[list[str], dict[str, str]], SandboxProcess] | None = None
    #: Private per-membership tmpfs root for generated provider configuration.
    #: It is never the durable native session store.
    activation_dir: str | None = None


class HarnessTurn:
    """One executing turn: its event stream, cancellation, and result."""

    def __init__(self, session: HarnessSession, process: SandboxProcess) -> None:
        self._session = session
        self._process = process
        self._runtime = session.runtime
        self._cancelled = False
        self._final_messages: list[str] = []
        self._result: TurnResult | None = None

    def iter_events(self) -> Iterator[HarnessEvent]:
        """Yield floor and dialect events as the harness produces them.

        Blocking, line-driven iteration. The caller that must interleave
        deadline/lease checks reads the process on its own thread exactly as
        before — this iterator is the single place a line becomes an event.
        """
        yield HarnessEvent(TURN_STARTED)
        for line in self._process.lines():
            yield self.event_for_line(line)
        yield self.finish_event()

    def event_for_line(self, line: str) -> HarnessEvent:
        """Map one stdout line to its event; :data:`LINE` when there is none."""
        event = self._runtime.parse_event(line)
        if event is None:
            return HarnessEvent(LINE, raw_line=line)
        if event.event_type == MESSAGE:
            text = str((event.payload or {}).get("text", ""))
            if text.strip():
                self._final_messages.append(text)
        return HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)

    def raw_lines(self) -> Iterator[str]:
        """The transport's line stream, for callers that pump on their own thread.

        The runner reads stdout on a dedicated thread so deadline, lease and
        cancel checks keep running while the agent is silent; it feeds each
        line back through :meth:`event_for_line`. Iterating :meth:`iter_events`
        directly is the single-threaded equivalent.
        """
        return self._process.lines()

    def stderr_text(self) -> str:
        """Whatever the harness wrote to stderr, once it has exited."""
        return self._process.stderr_text()

    def cancel(self) -> None:
        """Stop this turn now. The kill is the platform's backstop, kept here."""
        self._cancelled = True
        self._process.kill()

    def wait(self) -> TurnResult:
        """Wait for exit and return the contract-checked result."""
        if self._result is None:
            exit_code = self._process.wait()
            self._result = self._build_result(exit_code)
            self._session.turn_finished()
        return self._result

    def finish_event(self) -> HarnessEvent:
        """The floor event describing how this turn ended."""
        result = self.wait()
        if result.status == "completed":
            return HarnessEvent(TURN_COMPLETED, {"final_output": result.final_output})
        return HarnessEvent(TURN_FAILED, {"status": result.status, "exit_code": result.exit_code})

    def _build_result(self, exit_code: int) -> TurnResult:
        if self._cancelled:
            return TurnResult(status="cancelled", final_output="", exit_code=exit_code)
        if exit_code != 0:
            return TurnResult(status="failed", final_output="", exit_code=exit_code)
        final = "\n\n".join(self._final_messages).strip()
        if not final:
            # D9: tool-only runs still owe the owner a displayable line. This
            # text is deliberately explicit about being synthesized — a driver
            # must never put words in the agent's mouth.
            final = "(the agent finished without a closing message)"
        return TurnResult(status="completed", final_output=final, exit_code=exit_code)


class HarnessSession:
    """A launched harness. Today: one turn, then done (the replay world)."""

    def __init__(self, driver: HarnessDriver, context: LaunchContext) -> None:
        self._driver = driver
        self._context = context
        self._turn: HarnessTurn | None = None
        self._turn_done = False
        self._closed = False

    @property
    def runtime(self) -> AgentRuntime:
        """The dialect adapter this session speaks through."""
        return self._driver.runtime

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has ended this session."""
        return self._closed

    @property
    def container(self) -> str | None:
        """The sandbox container this session's process runs in, if any.

        The host-side resource sampler is the only consumer: a resident turn
        reuses a process spawned by an earlier turn, so without this the one
        rung that needs no spawn would be the one rung nobody could measure.
        Sessions without a live process of their own answer None.
        """
        return None

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn:
        """Spawn the harness for this turn inside the platform's sandbox."""
        if self._closed:
            raise DriverError("session is closed")
        if self._turn is not None and not self._turn_done:
            raise DriverError("a turn is already running on this session")
        if self._turn is not None:
            # One-shot CLIs cannot take a second turn on one process. The
            # session that can (HOT, issue #57) overrides this, not callers.
            raise DriverError(
                f"the {self._driver.id!r} driver runs one turn per session; "
                "launch a new session for the next turn"
            )
        argv, extra_env = self.runtime.prepare(
            workdir=self._context.workdir,
            task_prompt=turn_input.prompt,
            model=turn_input.model,
            gateway_base_url=self._context.gateway_base_url,
            credential=self._context.credential,
            provides_isolation=self._context.provides_isolation,
            mcp_config=self._context.mcp_config,
            reasoning_effort=turn_input.reasoning_effort,
            **self._extra_prepare_kwargs(),
        )
        env = dict(self._context.base_env)
        env.update(extra_env)
        process = self._context.spawn(argv, env)
        self._turn = HarnessTurn(self, process)
        return self._turn

    def _extra_prepare_kwargs(self) -> dict[str, Any]:
        """Dialect-specific prepare arguments; the generic session has none."""
        return {}

    def turn_finished(self) -> None:
        """Recorded by the turn on completion; not a caller API."""
        self._turn_done = True

    def export_state(self) -> HarnessState:
        """Refused until a driver implements checkpointing (issue #56)."""
        raise StateExportUnsupported(
            f"the {self._driver.id!r} driver does not export session state yet"
        )

    def fork(self, at: object | None = None) -> HarnessSession:
        """Refused in v1 (design D8); the signature is the reserved contract."""
        raise ForkUnsupported(f"the {self._driver.id!r} driver does not fork sessions")

    def close(self) -> None:
        """Release the session; kills a still-running turn."""
        self._closed = True
        if self._turn is not None and not self._turn_done:
            self._turn.cancel()

    def stderr_text(self) -> str:
        """Return bounded process diagnostics when this session owns one."""
        if self._turn is None:
            return ""
        return self._turn.stderr_text()

    def process_exit_code(self) -> int | None:
        """Return a resident process status when this session owns one."""
        return None


class HarnessDriver:
    """Launches sessions for one registered harness."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime

    @property
    def id(self) -> str:
        """The registry id this driver answers for."""
        return self.runtime.name

    def capabilities(self) -> HarnessCapabilities:
        """The semantic capability row for this harness, as implemented."""
        return HarnessCapabilities()

    def launch(
        self,
        context: LaunchContext,
        state: HarnessState | None = None,
    ) -> HarnessSession:
        """Create a session; ``state`` restores one exported earlier (P2)."""
        if state is not None:
            raise StateRestoreUnsupported(
                f"the {self.id!r} driver does not restore session state yet"
            )
        return HarnessSession(self, context)


class NativeResumeSession(HarnessSession):
    """One native session continued by an explicit id on a fresh process."""

    def __init__(
        self,
        driver: HarnessDriver,
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__(driver, context)
        self._resume_session_id = resume_session_id

    def _extra_prepare_kwargs(self) -> dict[str, Any]:
        if self._context.state_dir is None:
            return {}
        result: dict[str, Any] = {
            # A legacy container bind-mounts the host state directory here;
            # a retained chat owns this path in its Docker writable layer.
            # The CLI must never receive the host-side path.
            "session_state_dir": STATE_MOUNT_TARGET,
            "resume_session_id": self._resume_session_id,
        }
        if self._context.activation_dir is not None:
            result["activation_dir"] = self._context.activation_dir
        return result


class NativeResumeDriver(HarnessDriver):
    """Shared driver for CLIs with on-disk state plus ``--session <id>``."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise local native resume without claiming portable export."""
        return HarnessCapabilities(continuity="resume")

    def launch(
        self,
        context: LaunchContext,
        state: HarnessState | None = None,
    ) -> HarnessSession:
        """Launch a new native session; portable checkpoint import is deferred."""
        if state is not None:
            raise StateRestoreUnsupported(
                f"the {self.id!r} driver resumes local native state but does not "
                "import a portable checkpoint yet"
            )
        return NativeResumeSession(self, context)

    def launch_resident(self, context: LaunchContext, *, session_id: str) -> HarnessSession:
        """Resume one explicit native session from the retained state tree."""
        if context.state_dir is None:
            raise StateRestoreUnsupported("a native resumed turn needs persistent session state")
        if not session_id:
            raise StateRestoreUnsupported("a native resumed turn needs a session locator")
        return NativeResumeSession(self, context, resume_session_id=session_id)


class CodexDriver(NativeResumeDriver):
    """Codex ``exec resume`` over its persisted rollout files."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise Codex app-server live, interrupt, and native-fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="mid_turn",
            fork="latest_immediate",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(
        self, context: LaunchContext, state: HarnessState | None = None
    ) -> CodexAppServerSession:
        """Start a new persistent Codex app-server thread."""
        if state is not None:
            raise StateRestoreUnsupported("Codex portable checkpoint import is not implemented")
        return CodexAppServerSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> CodexAppServerSession:
        """Resume one Codex thread through a persistent app-server transport."""
        if context.state_dir is None:
            raise StateRestoreUnsupported("a resident Codex session needs a state mount")
        return CodexAppServerSession(self, context, resume_session_id=session_id)


class PiDriver(NativeResumeDriver):
    """pi ``--session`` over its persisted JSONL session tree."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise Pi RPC live, interrupt, and prompt-free fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="mid_turn",
            fork="latest_immediate",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(
        self, context: LaunchContext, state: HarnessState | None = None
    ) -> PiRpcSession:
        """Start a new persistent Pi RPC session."""
        if state is not None:
            raise StateRestoreUnsupported("pi portable checkpoint import is not implemented")
        return PiRpcSession(self, context)

    def launch_resident_live(self, context: LaunchContext, *, session_id: str) -> PiRpcSession:
        """Resume one Pi session through its persistent RPC transport."""
        if context.state_dir is None:
            raise StateRestoreUnsupported("a resident pi session needs a state mount")
        return PiRpcSession(self, context, resume_session_id=session_id)

    def launch_fork_live(
        self,
        context: LaunchContext,
        *,
        source_session_id: str,
    ) -> PiRpcSession:
        """Open pi's prompt-free ``--fork`` RPC process for one probe."""
        if context.state_dir is None:
            raise StateRestoreUnsupported("a pi native fork needs a state mount")
        if not source_session_id:
            raise ForkUnsupported("a pi native fork needs a source session id")
        return PiRpcSession(self, context, fork_session_id=source_session_id)


class OpenCodeDriver(NativeResumeDriver):
    """OpenCode ``run --session`` over its XDG data store."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise OpenCode serve live, interrupt, and native-fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="mid_turn",
            fork="latest_immediate",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(
        self, context: LaunchContext, state: HarnessState | None = None
    ) -> HttpAgentLiveSession:
        """Start a new authenticated OpenCode serve session."""
        if state is not None:
            raise StateRestoreUnsupported("OpenCode portable checkpoint import is not implemented")
        return HttpAgentLiveSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> HttpAgentLiveSession:
        """Resume one OpenCode session through its authenticated bridge."""
        if context.state_dir is None:
            raise StateRestoreUnsupported("a resident OpenCode session needs a state mount")
        return HttpAgentLiveSession(self, context, resume_session_id=session_id)


class KiloDriver(NativeResumeDriver):
    """Kilo ``run --session`` over its XDG data store."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise Kilo serve live, interrupt, and native-fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="mid_turn",
            fork="latest_immediate",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(
        self, context: LaunchContext, state: HarnessState | None = None
    ) -> HttpAgentLiveSession:
        """Start a new authenticated Kilo serve session."""
        if state is not None:
            raise StateRestoreUnsupported("Kilo portable checkpoint import is not implemented")
        return HttpAgentLiveSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> HttpAgentLiveSession:
        """Resume one Kilo session through its authenticated bridge."""
        if context.state_dir is None:
            raise StateRestoreUnsupported("a resident Kilo session needs a state mount")
        return HttpAgentLiveSession(self, context, resume_session_id=session_id)


class _JsonResidentTurn(HarnessTurn):
    """One framed activation on a process whose stdout survives the turn."""

    def __init__(self, session: _JsonResidentSession, process: SandboxProcess) -> None:
        super().__init__(session, process)
        self._saw_result = False
        self._protocol_failed = False
        self.protocol_turn_id: str | None = None
        # Several native protocols publish mutable updates for one tool item.
        # Keep this turn-scoped: item identifiers may be reused on later turns.
        self.reported_tools: set[str] = set()

    def raw_lines(self) -> Iterator[str]:
        for line in self._session.turn_lines(self):
            yield line
            settled, failed = self._session.line_settles_turn(line, self)
            if settled:
                self._saw_result = True
                self._protocol_failed = failed
                self._process.poll()
                return

    def iter_events(self) -> Iterator[HarnessEvent]:
        yield HarnessEvent(TURN_STARTED)
        for line in self.raw_lines():
            yield self.event_for_line(line)
        yield self.finish_event()

    def event_for_line(self, line: str) -> HarnessEvent:
        event = self._session.event_for_protocol_line(line, self)
        if event.event_type == MESSAGE:
            text = str(event.payload.get("text", ""))
            if text.strip():
                self._final_messages.append(text)
        return event

    def cancel(self) -> None:
        """Use the exact native interrupt; preserve the resident transport."""
        if self._cancelled:
            return
        self._cancelled = True
        self._session.interrupt_turn(self)

    def wait(self) -> TurnResult:
        if self._result is None:
            if not self._saw_result:
                for line in self.raw_lines():
                    self.event_for_line(line)
            if self._saw_result:
                exit_code = 1 if self._protocol_failed else 0
            else:
                exit_code = self._process.wait() or 1
            self._result = self._build_result(exit_code)
            self._session.turn_finished()
        return self._result


class _JsonResidentSession(HarnessSession):
    """Common process lifetime and framing for exact native JSON protocols."""

    def __init__(
        self,
        driver: HarnessDriver,
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__(driver, context)
        if context.spawn_live is None:
            raise DriverError("a resident session needs a live-input spawn")
        if context.state_dir is None:
            raise DriverError("a resident session needs a state mount")
        self._resume_session_id = resume_session_id
        self._process: SandboxProcess | None = None
        self._lines: Iterator[str] | None = None
        self._model: str | None = None
        self._reasoning_effort: str | None = None
        self._live_turn: _JsonResidentTurn | None = None
        self._request_sequence = 0
        self._prefetched: list[str] = []

    @property
    def container(self) -> str | None:
        return getattr(self._process, "container", None)

    def _request_id(self, prefix: str = "req") -> str:
        self._request_sequence += 1
        return f"{prefix}-{self._request_sequence}"

    def _write_json(self, value: dict[str, Any]) -> None:
        if self._process is None:
            raise DriverError("resident process has not started")
        try:
            self._process.write_line(json.dumps(value, separators=(",", ":")))
        except OSError as exc:
            raise DriverError(f"resident process is gone: {exc}") from exc

    def _next_line(self) -> str:
        if self._lines is None:
            raise DriverError("resident stdout is not attached")
        try:
            return next(self._lines)
        except StopIteration as exc:
            code = self._process.wait() if self._process is not None else 1
            # Keep enough local diagnostics to distinguish a protocol crash
            # from an ordinary EOF.  The sandbox process already retains only
            # a bounded stderr tail; cap it again here because this exception
            # crosses the broker boundary.  Agent credentials are supplied in
            # environment variables, never stderr/argv, but avoid reflecting
            # arbitrary multiline CLI output to an API caller all the same.
            diagnostic = ""
            if self._process is not None:
                diagnostic = " ".join(self._process.stderr_text().strip().split())[-500:]
            detail = f"resident process exited unexpectedly ({code})"
            if diagnostic:
                detail += f": {diagnostic}"
            raise DriverError(detail) from exc

    @staticmethod
    def _object(line: str) -> dict[str, Any] | None:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _read_until(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        while True:
            line = self._next_line()
            value = self._object(line)
            if value is not None and predicate(value):
                return value
            self._prefetched.append(line)

    def _prepare_live(
        self, *, model: str, reasoning_effort: str | None
    ) -> tuple[list[str], dict[str, str]]:
        prepare_live = getattr(self.runtime, "prepare_live", None)
        if prepare_live is None:
            raise DriverError(f"runtime {self.runtime.name!r} has no live invocation")
        return prepare_live(
            workdir=self._context.workdir,
            model=model,
            gateway_base_url=self._context.gateway_base_url,
            credential=self._context.credential,
            provides_isolation=self._context.provides_isolation,
            mcp_config=self._context.mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=self._context.session_state_dir,
            resume_session_id=self._resume_session_id,
            activation_dir=self._context.activation_dir,
        )

    def ensure_started(self, *, model: str, reasoning_effort: str | None) -> None:
        if self._closed:
            raise DriverError("session is closed")
        if self._process is None:
            argv, extra_env = self._prepare_live(
                model=model,
                reasoning_effort=reasoning_effort,
            )
            env = dict(self._context.base_env)
            env.update(extra_env)
            assert self._context.spawn_live is not None
            self._process = self._context.spawn_live(argv, env)
            self._lines = self._process.lines()
            self._model = model
            self._reasoning_effort = reasoning_effort
            if self._process.poll() is not None:
                raise DriverError("resident process exited during initialization")
            self._initialize_protocol()
        elif model != self._model or reasoning_effort != self._reasoning_effort:
            raise DriverError("resident model/reasoning change takes a member-only restart")

    def _initialize_protocol(self) -> None:
        raise NotImplementedError

    def _new_turn(self) -> _JsonResidentTurn:
        assert self._process is not None
        return _JsonResidentTurn(self, self._process)

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn:
        if self._closed:
            raise DriverError("session is closed")
        if self._live_turn is not None and self._live_turn._result is None:
            raise DriverError("a turn is already running on this session")
        self.ensure_started(
            model=turn_input.model,
            reasoning_effort=turn_input.reasoning_effort,
        )
        turn = self._new_turn()
        self._start_protocol_turn(turn_input, turn)
        self._live_turn = turn
        return turn

    def _start_protocol_turn(self, turn_input: TurnInput, turn: _JsonResidentTurn) -> None:
        raise NotImplementedError

    def turn_lines(self, turn: _JsonResidentTurn) -> Iterator[str]:
        while self._prefetched:
            yield self._prefetched.pop(0)
        while True:
            yield self._next_line()

    def line_settles_turn(self, line: str, turn: _JsonResidentTurn) -> tuple[bool, bool]:
        raise NotImplementedError

    def event_for_protocol_line(self, line: str, turn: _JsonResidentTurn) -> HarnessEvent:
        event = self.runtime.parse_event(line)
        if event is None:
            return HarnessEvent(LINE, raw_line=line)
        return HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)

    def interrupt_turn(self, turn: _JsonResidentTurn) -> None:
        raise NotImplementedError

    def turn_finished(self) -> None:
        return

    def process_alive(self) -> bool:
        return not self._closed and self._process is not None and self._process.poll() is None

    def stderr_text(self) -> str:
        """Return the resident process's retained stderr tail."""
        return "" if self._process is None else self._process.stderr_text()

    def process_exit_code(self) -> int | None:
        """Return the resident process's host-side exit status, if known."""
        return None if self._process is None else self._process.poll()

    def close(self) -> None:
        self._closed = True
        if self._process is not None:
            self._process.close_stdin()
            self._process.kill()
            self._process.wait()


class CodexAppServerSession(_JsonResidentSession):
    """Codex 0.145.0 app-server, one persisted thread per live process."""

    _PROVIDER = "hybridinference"

    def _initialize_protocol(self) -> None:
        initialize_id = self._request_id("initialize")
        self._write_json(
            {
                "id": initialize_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "freeinference-cloud-agent",
                        "title": "FreeInference Cloud Agent",
                        "version": "1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            }
        )
        initialized = self._read_until(lambda value: value.get("id") == initialize_id)
        if initialized.get("error") is not None:
            raise DriverError(f"Codex initialize failed: {initialized['error']}")
        self._write_json({"method": "initialized"})

        method = "thread/resume" if self._resume_session_id else "thread/start"
        request_id = self._request_id("thread")
        params: dict[str, Any] = {
            "model": self._model,
            "modelProvider": self._PROVIDER,
            "cwd": self._context.workdir,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access"
            if self._context.provides_isolation
            else "workspace-write",
        }
        if self._resume_session_id:
            params["threadId"] = self._resume_session_id
        self._write_json({"id": request_id, "method": method, "params": params})
        response = self._read_until(lambda value: value.get("id") == request_id)
        if response.get("error") is not None:
            raise DriverError(f"Codex {method} failed: {response['error']}")
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        session_id = thread.get("id") if isinstance(thread, dict) else None
        if not session_id:
            raise DriverError(f"Codex {method} returned no thread id")
        if self._resume_session_id and session_id != self._resume_session_id:
            raise DriverError("Codex resumed a different native thread")
        self.runtime.session_id = str(session_id)

    def _start_protocol_turn(self, turn_input: TurnInput, turn: _JsonResidentTurn) -> None:
        request_id = self._request_id("turn")
        self._write_json(
            {
                "id": request_id,
                "method": "turn/start",
                "params": {
                    "threadId": self.runtime.session_id,
                    "input": [{"type": "text", "text": turn_input.prompt}],
                    "model": turn_input.model,
                    "effort": turn_input.reasoning_effort,
                },
            }
        )
        response = self._read_until(lambda value: value.get("id") == request_id)
        if response.get("error") is not None:
            raise DriverError(f"Codex turn/start failed: {response['error']}")
        result = response.get("result")
        protocol_turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = protocol_turn.get("id") if isinstance(protocol_turn, dict) else None
        if not turn_id:
            raise DriverError("Codex turn/start returned no turn id")
        turn.protocol_turn_id = str(turn_id)

    def line_settles_turn(self, line: str, turn: _JsonResidentTurn) -> tuple[bool, bool]:
        """Recognize the completion notification for this exact Codex turn."""
        value = self._object(line)
        if value is None or value.get("method") != "turn/completed":
            return False, False
        params = value.get("params")
        completed = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(completed, dict) or completed.get("id") != turn.protocol_turn_id:
            return False, False
        status = str(completed.get("status") or "").lower()
        return True, status not in {"completed", "interrupted"}

    def event_for_protocol_line(self, line: str, turn: _JsonResidentTurn) -> HarnessEvent:
        """Normalize one Codex app-server notification for the active turn."""
        value = self._object(line)
        if value is None:
            return HarnessEvent(LINE, raw_line=line)
        if value.get("error") is not None:
            return HarnessEvent(ERROR, {"text": str(value["error"])[:2000]}, raw_line=line)
        method = str(value.get("method") or "")
        params = value.get("params")
        params = params if isinstance(params, dict) else {}
        item = params.get("item")
        item = item if isinstance(item, dict) else {}
        item_type = str(item.get("type") or "")
        if method == "item/completed" and item_type == "agentMessage":
            return HarnessEvent(MESSAGE, {"text": str(item.get("text") or "")}, raw_line=line)
        if method == "item/completed" and item_type == "reasoning":
            text = "\n".join(str(value) for value in item.get("summary") or [])
            return HarnessEvent(THINKING, {"text": text[:4000]}, raw_line=line)
        if method in {"item/started", "item/updated"} and item_type in {
            "commandExecution",
            "mcpToolCall",
        }:
            item_id = str(item.get("id") or "")
            if (item_id and item_id in turn.reported_tools) or (
                not item_id and method == "item/updated"
            ):
                return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
            if item_id:
                turn.reported_tools.add(item_id)
            return HarnessEvent(
                TOOL_USE,
                {
                    "name": item.get("tool") or item_type,
                    "id": item_id or None,
                    "input": item.get("command") or item.get("arguments"),
                },
                raw_line=line,
            )
        if method == "item/completed" and item_type in {"commandExecution", "mcpToolCall"}:
            exit_code = item.get("exitCode")
            return HarnessEvent(
                TOOL_RESULT,
                {
                    "tool_use_id": item.get("id"),
                    "is_error": (isinstance(exit_code, int) and exit_code != 0)
                    or bool(item.get("error")),
                    "content": str(
                        item.get("aggregatedOutput")
                        or item.get("result")
                        or item.get("error")
                        or ""
                    )[:4000],
                    "exit_code": exit_code,
                },
                raw_line=line,
            )
        if method in {"turn/diff/updated", "turn/diffUpdated"}:
            return HarnessEvent(DIFF, {"text": str(params.get("diff") or "")[:4000]}, raw_line=line)
        if method == "turn/completed":
            completed = params.get("turn")
            return HarnessEvent(
                LIFECYCLE,
                {
                    "phase": "result",
                    "status": completed.get("status") if isinstance(completed, dict) else None,
                    "usage": completed.get("usage") if isinstance(completed, dict) else None,
                },
                raw_line=line,
            )
        return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)

    def interrupt_turn(self, turn: _JsonResidentTurn) -> None:
        """Interrupt only the active Codex turn on the resident thread."""
        if not turn.protocol_turn_id:
            raise DriverError("Codex turn id is not available for interrupt")
        self._write_json(
            {
                "id": self._request_id("interrupt"),
                "method": "turn/interrupt",
                "params": {
                    "threadId": self.runtime.session_id,
                    "turnId": turn.protocol_turn_id,
                },
            }
        )

    def fork_native(self, *, source_session_id: str, operation_id: str) -> str:
        """Create and return one prompt-free native Codex child thread."""
        if source_session_id != self.runtime.session_id:
            raise DriverError("Codex fork source no longer matches the live thread")
        request_id = self._request_id(f"fork-{operation_id}")
        self._write_json(
            {
                "id": request_id,
                "method": "thread/fork",
                "params": {
                    "threadId": source_session_id,
                    "model": self._model,
                    "modelProvider": self._PROVIDER,
                    "cwd": self._context.workdir,
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access"
                    if self._context.provides_isolation
                    else "workspace-write",
                    # We need only the child locator.  Asking Codex to rebuild
                    # and serialize every historical turn after the native
                    # mutation makes the response slower and adds a second
                    # failure surface after the child rollout already exists.
                    # 0.145.0 explicitly provides this fork mode for clients
                    # that will resume the returned thread separately.
                    "excludeTurns": True,
                },
            }
        )
        response = self._read_until(lambda value: value.get("id") == request_id)
        if response.get("error") is not None:
            raise DriverError(f"Codex thread/fork failed: {response['error']}")
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        child_id = thread.get("id") if isinstance(thread, dict) else None
        if not child_id or child_id == source_session_id:
            raise DriverError("Codex thread/fork returned an invalid child id")
        return str(child_id)


class PiRpcSession(_JsonResidentSession):
    """pi 0.73.1 RPC mode with an explicit native session locator."""

    def __init__(
        self,
        driver: HarnessDriver,
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
        fork_session_id: str | None = None,
    ) -> None:
        if resume_session_id and fork_session_id:
            raise DriverError("pi cannot resume and fork in one live process")
        super().__init__(driver, context, resume_session_id=resume_session_id)
        self._fork_session_id = fork_session_id

    def _prepare_live(
        self, *, model: str, reasoning_effort: str | None
    ) -> tuple[list[str], dict[str, str]]:
        prepare_live = getattr(self.runtime, "prepare_live", None)
        if prepare_live is None:
            raise DriverError("pi has no live invocation")
        return prepare_live(
            workdir=self._context.workdir,
            model=model,
            gateway_base_url=self._context.gateway_base_url,
            credential=self._context.credential,
            provides_isolation=self._context.provides_isolation,
            mcp_config=self._context.mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=self._context.session_state_dir,
            resume_session_id=self._resume_session_id,
            activation_dir=self._context.activation_dir,
            fork_session_id=self._fork_session_id,
        )

    def _initialize_protocol(self) -> None:
        request_id = self._request_id("state")
        self._write_json({"id": request_id, "type": "get_state"})
        response = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == request_id
        )
        if response.get("success") is not True:
            raise DriverError(f"pi get_state failed: {response.get('error')}")
        data = response.get("data")
        session_id = data.get("sessionId") if isinstance(data, dict) else None
        if not session_id:
            raise DriverError("pi get_state returned no session id")
        if self._resume_session_id and session_id != self._resume_session_id:
            raise DriverError("pi resumed a different native session")
        self.runtime.session_id = str(session_id)

    def _start_protocol_turn(self, turn_input: TurnInput, turn: _JsonResidentTurn) -> None:
        request_id = self._request_id("prompt")
        turn.protocol_turn_id = request_id
        self._write_json({"id": request_id, "type": "prompt", "message": turn_input.prompt})

    def line_settles_turn(self, line: str, turn: _JsonResidentTurn) -> tuple[bool, bool]:
        """Recognize Pi's response failure or terminal agent-end event."""
        value = self._object(line)
        if value is None:
            return False, False
        if (
            value.get("type") == "response"
            and value.get("id") == turn.protocol_turn_id
            and value.get("success") is False
        ):
            return True, True
        return value.get("type") == "agent_end", False

    def event_for_protocol_line(self, line: str, turn: _JsonResidentTurn) -> HarnessEvent:
        """Normalize one Pi RPC protocol event for the active turn."""
        event = self.runtime.parse_event(line)
        if event is None:
            return HarnessEvent(LINE, raw_line=line)
        return HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)

    def interrupt_turn(self, turn: _JsonResidentTurn) -> None:
        """Abort the active Pi RPC turn without closing its session process."""
        self._write_json({"id": self._request_id("abort"), "type": "abort"})


class HttpAgentLiveTurn(_JsonResidentTurn):
    """One OpenCode/Kilo activation, deduplicating mutable part updates."""

    def __init__(self, session: HttpAgentLiveSession, process: SandboxProcess) -> None:
        super().__init__(session, process)
        self.text_parts: dict[str, str] = {}


class HttpAgentLiveSession(_JsonResidentSession):
    """OpenCode/Kilo serve hidden behind the private loopback bridge."""

    def _initialize_protocol(self) -> None:
        ready = self._read_until(lambda value: value.get("bridge") in {"ready", "fatal"})
        if ready.get("bridge") != "ready":
            raise DriverError(f"{self.runtime.name} live bridge failed: {ready.get('detail')}")
        session_id = str(ready.get("session_id") or "")
        if not session_id:
            raise DriverError(f"{self.runtime.name} bridge returned no session id")
        if self._resume_session_id and session_id != self._resume_session_id:
            raise DriverError(f"{self.runtime.name} resumed a different native session")
        self.runtime.session_id = session_id

    def _new_turn(self) -> HttpAgentLiveTurn:
        assert self._process is not None
        return HttpAgentLiveTurn(self, self._process)

    def _start_protocol_turn(self, turn_input: TurnInput, turn: _JsonResidentTurn) -> None:
        request_id = self._request_id("turn")
        turn.protocol_turn_id = request_id
        self._write_json({"id": request_id, "type": "turn", "prompt": turn_input.prompt})

    def line_settles_turn(self, line: str, turn: _JsonResidentTurn) -> tuple[bool, bool]:
        """Recognize the bridge settlement or command failure for this turn."""
        value = self._object(line)
        if value is None:
            return False, False
        bridge = value.get("bridge")
        if bridge == "settled" and value.get("turn_id") == turn.protocol_turn_id:
            return True, value.get("status") != "completed"
        if bridge in {"fatal", "command_error"}:
            command_id = value.get("id")
            if bridge == "fatal" or command_id in {"", turn.protocol_turn_id}:
                return True, True
        return False, False

    def event_for_protocol_line(self, line: str, turn: _JsonResidentTurn) -> HarnessEvent:
        """Normalize one OpenCode/Kilo bridge event for the active turn."""
        assert isinstance(turn, HttpAgentLiveTurn)
        value = self._object(line)
        if value is None:
            return HarnessEvent(LINE, raw_line=line)
        bridge = value.get("bridge")
        if bridge == "event":
            event = value.get("event")
            event = event if isinstance(event, dict) else {}
            event_type = event.get("type")
            properties = event.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            part = properties.get("part")
            part = part if isinstance(part, dict) else {}
            part_type = part.get("type")
            part_id = str(part.get("id") or "")
            if event_type == "message.part.updated" and part_type == "text":
                if part_id:
                    turn.text_parts[part_id] = str(part.get("text") or "")
                return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
            if event_type == "message.part.updated" and part_type == "reasoning":
                text = str(part.get("text") or "")
                if part.get("time", {}).get("end") if isinstance(part.get("time"), dict) else False:
                    return HarnessEvent(THINKING, {"text": text[:4000]}, raw_line=line)
                return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
            if event_type == "message.part.updated" and part_type == "tool":
                state = part.get("state")
                state = state if isinstance(state, dict) else {}
                call_id = str(part.get("callID") or part_id)
                status = state.get("status")
                if status in {"pending", "running"} and call_id not in turn.reported_tools:
                    turn.reported_tools.add(call_id)
                    return HarnessEvent(
                        TOOL_USE,
                        {
                            "name": part.get("tool"),
                            "id": call_id,
                            "input": state.get("input"),
                        },
                        raw_line=line,
                    )
                if status in {"completed", "error"}:
                    return HarnessEvent(
                        TOOL_RESULT,
                        {
                            "tool_use_id": call_id,
                            "is_error": status == "error",
                            "content": str(state.get("output") or state.get("error") or "")[:4000],
                        },
                        raw_line=line,
                    )
            if event_type == "session.error":
                return HarnessEvent(
                    ERROR,
                    {"text": str(properties.get("error") or "session failed")[:2000]},
                    raw_line=line,
                )
            return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
        if bridge == "settled":
            final = "\n\n".join(text for text in turn.text_parts.values() if text.strip())
            if final:
                return HarnessEvent(MESSAGE, {"text": final}, raw_line=line)
            return HarnessEvent(LIFECYCLE, {"phase": "result"}, raw_line=line)
        if bridge in {"fatal", "command_error"}:
            return HarnessEvent(
                ERROR, {"text": str(value.get("detail") or "")[:2000]}, raw_line=line
            )
        return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)

    def interrupt_turn(self, turn: _JsonResidentTurn) -> None:
        """Interrupt only the active bridge turn on the resident session."""
        self._write_json(
            {
                "id": self._request_id("interrupt"),
                "type": "interrupt",
                "turn_id": turn.protocol_turn_id,
            }
        )

    def fork_native(self, *, source_session_id: str, operation_id: str) -> str:
        """Create and return one prompt-free OpenCode/Kilo child session."""
        if source_session_id != self.runtime.session_id:
            raise DriverError(f"{self.runtime.name} fork source no longer matches")
        request_id = self._request_id(f"fork-{operation_id}")
        self._write_json({"id": request_id, "type": "fork"})
        response = self._read_until(
            lambda value: (
                value.get("bridge") in {"fork_result", "command_error", "fatal"}
                and (value.get("id") in {None, "", request_id})
            )
        )
        if response.get("bridge") != "fork_result":
            raise DriverError(f"{self.runtime.name} fork failed: {response.get('detail')}")
        child_id = str(response.get("session_id") or "")
        if not child_id or child_id == source_session_id:
            raise DriverError(f"{self.runtime.name} fork returned an invalid child id")
        return child_id

    def close(self) -> None:
        """Let the HTTP server flush SQLite before using the kill backstop."""
        self._closed = True
        process = self._process
        if process is None:
            return
        process.close_stdin()
        deadline = time.monotonic() + 6
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
        process.wait()


#: Where the claude CLI keeps its config inside the sandbox: under the state
#: mount, so sessions survive the container (paired with the constant
#: ``/workspace`` workdir, which keeps the project-dir encoding stable).
CLAUDE_STATE_CONFIG_DIR = f"{STATE_MOUNT_TARGET}/claude"


class ClaudeCodeSession(HarnessSession):
    """A claude-code session: persistable, restorable, still one turn (COLD)."""

    def __init__(
        self,
        driver: HarnessDriver,
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__(driver, context)
        self._resume_session_id = resume_session_id

    def _extra_prepare_kwargs(self) -> dict[str, Any]:
        if self._context.state_dir is None:
            return {}
        return {
            "config_dir": CLAUDE_STATE_CONFIG_DIR,
            "resume_session_id": self._resume_session_id,
        }

    def export_state(self) -> HarnessState:
        """Package the session tree the turn just wrote (design §6).

        Complete or refuse — never a best-effort partial: a checkpoint that
        might be half a session is worse than replay, which is always there.
        """
        if self._context.state_dir is None:
            raise StateExportUnsupported("this session ran without a state mount")
        session_id = getattr(self.runtime, "session_id", None)
        if not session_id:
            raise StateExportUnsupported("the CLI never announced a session id")
        root = pathlib.Path(self._context.state_dir) / "claude"
        if not any(root.glob(f"projects/*/{session_id}.jsonl")):
            raise StateExportUnsupported(
                f"no session file for {session_id!r} under the state mount"
            )
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            archive.add(root / "projects", arcname="projects")
        return HarnessState(session_id=session_id, blob=buffer.getvalue())


class ClaudeCodeDriver(HarnessDriver):
    """Claude Code native resume plus portable checkpoint export (issue #56)."""

    def capabilities(self) -> HarnessCapabilities:
        """Native resume/export plus the supervisor-owned stream-json pipe."""
        return HarnessCapabilities(
            continuity="resume",
            checkpoint_export=True,
            steering="mid_turn",
            fork="latest_immediate",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch(self, context: LaunchContext, state: HarnessState | None = None) -> HarnessSession:
        """Fresh session, or one that continues an exported state's memory."""
        if state is None:
            return ClaudeCodeSession(self, context)
        self._restore(context, state)
        return ClaudeCodeSession(self, context, resume_session_id=state.session_id)

    def launch_resident(self, context: LaunchContext, *, session_id: str) -> HarnessSession:
        """A hot turn in a resident workspace: resume from state already on disk.

        No blob restore — the state mount still holds the session files the
        previous turn wrote. The turn runs the verified one-shot ``--resume``
        mechanics with **this attempt's own credential in its own process
        environment**, which is what the credential-rotation experiments
        (tests/external) proved is the only per-turn key mechanism the pinned
        CLI actually honours.
        """
        if context.state_dir is None:
            raise StateRestoreUnsupported("a resident turn needs the thread's state mount")
        return ClaudeCodeSession(self, context, resume_session_id=session_id)

    def launch_live(
        self, context: LaunchContext, state: HarnessState | None = None
    ) -> ClaudeCodeLiveSession:
        """A stdin-fed multi-turn session over a fresh or restored state tree."""
        if state is not None:
            self._restore(context, state)
            return ClaudeCodeLiveSession(self, context, resume_session_id=state.session_id)
        return ClaudeCodeLiveSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> ClaudeCodeLiveSession:
        """The HOT transport: a live process resumed over a resident workspace.

        The mirror of :meth:`launch_resident` — same on-disk resume, but the
        process stays alive after the turn settles and takes the next turn on
        stdin. Only correct with a residency-lifetime credential in
        ``context.credential`` (design §14: the thread-attributed grant that
        host beats renew), because the pinned CLI runs its whole life on its
        launch key — the measured fact that forced warm_native before §14
        relaxed attribution to the thread.
        """
        if context.state_dir is None:
            raise StateRestoreUnsupported("a resident turn needs the thread's state mount")
        return ClaudeCodeLiveSession(self, context, resume_session_id=session_id)

    @staticmethod
    def _restore(context: LaunchContext, state: HarnessState) -> None:
        if context.state_dir is None:
            raise StateRestoreUnsupported("restore needs a state mount")
        root = pathlib.Path(context.state_dir) / "claude"
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(state.blob), mode="r:gz") as archive:
            _extract_below(archive, root)


def _extract_below(archive: tarfile.TarFile, root: pathlib.Path) -> None:
    """Extract refusing traversal: a checkpoint is data, not a path authority."""
    base = root.resolve()
    for member in archive.getmembers():
        target = (base / member.name).resolve()
        if not target.is_relative_to(base):
            raise DriverError(f"checkpoint entry escapes the state dir: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise DriverError(f"checkpoint entry has a refused type: {member.name!r}")
    # The loop above is the filter: only plain files and directories, only
    # below the state dir. Works on every Python this repo supports.
    archive.extractall(base)


class ClaudeCodeLiveTurn(HarnessTurn):
    """One turn on a resident process: borrows the shared stdout, ends at result."""

    def __init__(self, session: ClaudeCodeLiveSession, process: SandboxProcess) -> None:
        super().__init__(session, process)
        self._saw_result = False

    def raw_lines(self) -> Iterator[str]:
        """This turn's slice of the shared stream, up to its result line."""
        for line in self._session.shared_lines():
            yield line
            if self._runtime.turn_settled(line):
                self._saw_result = True
                # Opportunistic reap: a process that died right after its
                # result must not linger as a zombie until the session closes.
                self._process.poll()
                return

    def iter_events(self) -> Iterator[HarnessEvent]:
        """Floor + dialect events for this turn only."""
        yield HarnessEvent(TURN_STARTED)
        for line in self.raw_lines():
            yield self.event_for_line(line)
        yield self.finish_event()

    def wait(self) -> TurnResult:
        """Settle this turn without waiting for the resident process to die."""
        if self._result is None:
            if not self._cancelled and not self._saw_result:
                # Settling without having drained the slice: consume it now —
                # the boundary and the final message both live in the stream.
                for line in self.raw_lines():
                    self.event_for_line(line)
            # The process is deliberately still alive after a result. A stream
            # ending without one is a mid-turn death even if its code was zero.
            exit_code = 0 if self._saw_result else self._process.wait() or 1
            self._result = self._build_result(exit_code)
            self._session.turn_finished()
        return self._result

    def cancel(self) -> None:
        """Send Claude's control ``interrupt`` without killing the session."""
        if self._cancelled:
            return
        self._cancelled = True
        self._session.interrupt_turn()


class ClaudeCodeLiveSession(ClaudeCodeSession):
    """A stdin-fed multi-turn session: the process survives between turns.

    Measured against the pinned CLI (2.1.220, headless stream-json; see
    ``tests/external/test_credential_rotation.py``): the ``apiKeyHelper``
    result is fetched exactly once per process — ``CLAUDE_CODE_API_KEY_HELPER_
    TTL_MS`` of 0 and 1 change nothing, and a 401 on a revoked key produces an
    error result without a refetch or retry. A live process therefore runs its
    whole life on its launch credential — which is why it must be launched on
    the *residency's* credential, the thread-attributed grant of design §14
    that host beats renew and release/takeover revokes, never on one turn's
    per-attempt token. With attribution per thread, one long-lived key for
    the whole conversation is the ledger telling the truth, not a lie.
    """

    def __init__(
        self,
        driver: HarnessDriver,
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__(driver, context, resume_session_id=resume_session_id)
        if context.spawn_live is None:
            raise DriverError("a live session needs a live-input spawn")
        if context.state_dir is None:
            raise DriverError("a resident session needs a state mount")
        argv, extra_env = self.runtime.prepare_live(
            workdir=context.workdir,
            model="",  # pinned per session below, chosen by the first turn's model
            gateway_base_url=context.gateway_base_url,
            credential=context.credential,
            provides_isolation=context.provides_isolation,
            mcp_config=context.mcp_config,
            **self._extra_prepare_kwargs(),
        )
        self._argv_template = argv
        self._extra_env = extra_env
        self._process: SandboxProcess | None = None
        self._lines: Iterator[str] | None = None
        self._model: str | None = None
        #: Pinned by the first turn alongside the model, and compared against
        #: every later one. ``None`` is a real value here (Auto), so this is
        #: only meaningful once ``_process`` exists.
        self._reasoning_effort: str | None = None
        self._live_turn: ClaudeCodeLiveTurn | None = None

    @property
    def container(self) -> str | None:
        """The container the live process runs in, once it has been spawned.

        Read defensively: the process is whatever the platform's spawn seam
        returned, and a process that cannot say where it runs is one the
        sampler simply does not measure. An observer must never be the reason
        a turn fails.
        """
        return getattr(self._process, "container", None)

    def shared_lines(self) -> Iterator[str]:
        """The session's single stdout reader, borrowed by the current turn."""
        assert self._lines is not None
        return self._lines

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn:
        """Feed one user turn to the resident process, spawning it lazily.

        The first turn fixes the session's model — a resident process reads
        its model once (D6: ``model_switch: restart``). A later turn naming a
        different model refuses here and the platform restarts the session.
        The reasoning effort is fixed the same way and for the same reason:
        it is a launch flag, read once at spawn, so a turn that wants a
        different depth needs a different process.
        """
        if self._closed:
            raise DriverError("session is closed")
        if self._live_turn is not None and self._live_turn._result is None:
            raise DriverError("a turn is already running on this session")
        if self._process is None:
            self.ensure_started(
                model=turn_input.model,
                reasoning_effort=turn_input.reasoning_effort,
            )
        elif turn_input.model != self._model:
            raise DriverError(
                f"this resident session runs {self._model!r}; switching to "
                f"{turn_input.model!r} takes a restart (model_switch=restart)"
            )
        elif turn_input.reasoning_effort != self._reasoning_effort:
            raise DriverError(
                f"this resident session runs at reasoning effort "
                f"{self._reasoning_effort!r}; switching to "
                f"{turn_input.reasoning_effort!r} takes a restart "
                "(model_switch=restart)"
            )
        assert self._process is not None
        try:
            self._process.write_line(self.runtime.user_message_line(turn_input.prompt))
        except OSError as exc:
            # A broken pipe is the resident process having died idle. One
            # typed refusal, so the caller's fallback ladder catches it the
            # same way it catches every other driver refusal.
            raise DriverError(f"resident process is gone: {exc}") from exc
        self._live_turn = ClaudeCodeLiveTurn(self, self._process)
        return self._live_turn

    def ensure_started(self, *, model: str, reasoning_effort: str | None) -> None:
        """Spawn and probe the resident transport without injecting a prompt."""
        if self._closed:
            raise DriverError("session is closed")
        if self._process is None:
            argv = [*self._argv_template]
            model_at = argv.index("--model") + 1
            argv[model_at] = model
            # The template was built without an effort, so the flag is spliced
            # in beside the model rather than replaced. Spelling comes from the
            # dialect layer; this method only knows there is one.
            argv[model_at + 1 : model_at + 1] = self.runtime.effort_args(reasoning_effort)
            env = dict(self._context.base_env)
            env.update(self._extra_env)
            env["ANTHROPIC_MODEL"] = model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = model
            assert self._context.spawn_live is not None
            self._process = self._context.spawn_live(argv, env)
            self._lines = self._process.lines()
            self._model = model
            self._reasoning_effort = reasoning_effort
            # ``poll`` is the non-consuming readiness probe. A process that
            # exits during initialization is not a resident transport.
            if self._process.poll() is not None:
                raise DriverError("resident process exited during initialization")
        elif model != self._model:
            raise DriverError(
                f"this resident session runs {self._model!r}; switching to "
                f"{model!r} takes a restart (model_switch=restart)"
            )
        elif reasoning_effort != self._reasoning_effort:
            raise DriverError(
                f"this resident session runs at reasoning effort "
                f"{self._reasoning_effort!r}; switching to "
                f"{reasoning_effort!r} takes a restart "
                "(model_switch=restart)"
            )

    def turn_finished(self) -> None:
        """A settled turn leaves a live session ready for the next one."""
        return

    def interrupt_turn(self) -> None:
        """Interrupt only the active query over the pinned control protocol."""
        if self._process is None or not self.process_alive():
            raise DriverError("Claude resident process is not alive")
        self._process.write_line(
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": f"interrupt-{id(self._live_turn)}",
                    "request": {"subtype": "interrupt"},
                },
                separators=(",", ":"),
            )
        )

    def process_alive(self) -> bool:
        """Whether the resident process is spawned and still running.

        The platform checks this before labelling a turn ``hot``: a session
        whose process died idle (or never spawned) restarts, and calling that
        restart ``hot`` would make the hit-rate lie.
        """
        return not self._closed and self._process is not None and self._process.poll() is None

    def close(self) -> None:
        """End the session: EOF stdin, the kill backstop, and the reap.

        The final ``wait`` matters: a killed child that nobody waits on stays
        a zombie until the runner exits, and a long-lived runner accumulates
        one per closed session.
        """
        self._closed = True
        if self._process is not None:
            self._process.close_stdin()
            self._process.kill()
            self._process.wait()


_DRIVER_TYPES: dict[str, type[HarnessDriver]] = {
    "claude-code": ClaudeCodeDriver,
    "codex": CodexDriver,
    "pi": PiDriver,
    "opencode": OpenCodeDriver,
    "kilo": KiloDriver,
}


def get_driver(runtime_id: str, *, generic_command: str | None = None) -> HarnessDriver:
    """Resolve a registry id to its driver. Raises ``KeyError`` like get_runtime."""
    return driver_for(get_runtime(runtime_id, generic_command=generic_command))


def driver_for(runtime: AgentRuntime) -> HarnessDriver:
    """The driver for an already-resolved dialect adapter.

    ``getattr`` for the same reason the runner reads backend hooks that way:
    a hand-rolled adapter without a ``name`` is a generic driver, not a crash.
    """
    return _DRIVER_TYPES.get(getattr(runtime, "name", "generic"), HarnessDriver)(runtime)
