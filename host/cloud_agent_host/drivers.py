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

Today every registered harness is a one-shot process: a session carries at
most one turn, ``launch(state=...)`` is refused, and continuity is ``replay``.
That is not a limitation of this interface — it is the honest report of what
is implemented. The COLD loop (issue #56) teaches the claude-code driver
``export_state``/``launch(state)`` and flips its continuity to ``resume``; HOT
(issue #57) brings the live-input session and ``hot``. The interface does not
change for either, which is the point of landing it first.

Synchronous by design: the runner is threaded, and the only transport v1
drivers own is container stdio. The design doc's async signatures become
meaningful with the live-input transport and arrive with it.
"""

from __future__ import annotations

import io
import pathlib
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    MESSAGE,
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
    could theoretically do — the capability matrix in the design doc is
    provisional until a contract test pins each cell against the stamped
    image (issue #54), and until then the honest answer everywhere is the
    replay world we actually run.
    """

    continuity: Literal["hot", "resume", "replay"] = "replay"
    steering: Literal["mid_turn", "queue_only", "none"] = "none"
    fork: Literal["at_latest", "at_anchor", "none"] = "none"
    # Replay rebuilds the conversation as text, so any turn may pick any
    # model — per-turn switching is the one thing the replay world does
    # *better* than sessions. Session-capable drivers will downgrade this to
    # "restart" (claude-code) as the design records.
    model_switch: Literal["per_turn", "restart", "none"] = "per_turn"


@dataclass(frozen=True)
class TurnInput:
    """One user turn: the prompt and the model to answer it with."""

    prompt: str
    model: str


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
    #: Spawns with a live stdin (SandboxSpec.live_input). ``None`` means this
    #: caller does not offer residency; ``launch_live`` refuses without it.
    spawn_live: Callable[[list[str], dict[str, str]], SandboxProcess] | None = None


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

    def export_state(self) -> bytes:
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

    def launch(self, context: LaunchContext, state: bytes | None = None) -> HarnessSession:
        """Create a session; ``state`` restores one exported earlier (P2)."""
        if state is not None:
            raise StateRestoreUnsupported(
                f"the {self.id!r} driver does not restore session state yet"
            )
        return HarnessSession(self, context)


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
    """The claude-code driver: the first with real continuity (issue #56)."""

    def capabilities(self) -> HarnessCapabilities:
        """COLD-native continuity; HOT arrives with the live session (#57)."""
        return HarnessCapabilities(continuity="resume")

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
            if self._cancelled:
                exit_code = self._process.wait()
            elif self._saw_result:
                # The process is deliberately still alive: a settled turn on a
                # resident session exits nothing.
                exit_code = 0
            else:
                # The stream ended without a result event: the process died
                # mid-turn. A zero here would be a lie a retry acts on.
                exit_code = self._process.wait() or 1
            self._result = self._build_result(exit_code)
            self._session.turn_finished()
        return self._result


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
        self._live_turn: ClaudeCodeLiveTurn | None = None

    def shared_lines(self) -> Iterator[str]:
        """The session's single stdout reader, borrowed by the current turn."""
        assert self._lines is not None
        return self._lines

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn:
        """Feed one user turn to the resident process, spawning it lazily.

        The first turn fixes the session's model — a resident process reads
        its model once (D6: ``model_switch: restart``). A later turn naming a
        different model refuses here and the platform restarts the session.
        """
        if self._closed:
            raise DriverError("session is closed")
        if self._live_turn is not None and self._live_turn._result is None:
            raise DriverError("a turn is already running on this session")
        if self._process is None:
            argv = [*self._argv_template]
            model_at = argv.index("--model") + 1
            argv[model_at] = turn_input.model
            env = dict(self._context.base_env)
            env.update(self._extra_env)
            env["ANTHROPIC_MODEL"] = turn_input.model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = turn_input.model
            assert self._context.spawn_live is not None
            self._process = self._context.spawn_live(argv, env)
            self._lines = self._process.lines()
            self._model = turn_input.model
        elif turn_input.model != self._model:
            raise DriverError(
                f"this resident session runs {self._model!r}; switching to "
                f"{turn_input.model!r} takes a restart (model_switch=restart)"
            )
        try:
            self._process.write_line(self.runtime.user_message_line(turn_input.prompt))
        except OSError as exc:
            # A broken pipe is the resident process having died idle. One
            # typed refusal, so the caller's fallback ladder catches it the
            # same way it catches every other driver refusal.
            raise DriverError(f"resident process is gone: {exc}") from exc
        self._live_turn = ClaudeCodeLiveTurn(self, self._process)
        return self._live_turn

    def turn_finished(self) -> None:
        """A settled turn leaves a live session ready for the next one."""
        return

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
