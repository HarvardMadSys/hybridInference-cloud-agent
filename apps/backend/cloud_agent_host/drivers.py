"""Driver → Session → Turn: the platform's handle on an Agent CLI.

This is the object model used by the retained native-session design
(``docs/agents/specs/2026-08-31-native-session-fork-shared-workspace-design.md``),
layered over the dialect adapters in :mod:`cloud_agent_host.runtimes`:

- each CLI's package under **runtimes/** is its *dialect* layer — how the CLI
  is invoked and how one line of its output maps onto normalized events — and
  holds the live session that speaks its protocol (``runtimes/<cli>/session``).
- **drivers.py** is the *contract* layer the supervisor drives: launch a live
  session, ``start_turn`` on it, iterate its events, ``wait`` for a
  :class:`TurnResult` whose guarantees the platform can lean on. It holds the
  contract and the base classes the sessions share, and no CLI's protocol;
  :mod:`cloud_agent_host.runtimes.live` maps each CLI to its session.

Every supported CLI persists native session data inside its retained Docker
layer and can resume from an explicit session locator. A live process is kept
while the group is active; after ``docker stop`` the replacement process must
resume that exact locator from the same container layer.

Synchronous by design: the runner is threaded, while the resident transport
keeps each CLI's live input channel open and multiplexes turn events back into
this contract. The design document uses async-shaped pseudocode; these methods
provide the same lifecycle guarantees to the threaded host runner.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar

from cloud_agent_host.harness_event import LINE, HarnessEvent
from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    MESSAGE,
    NormalizedEvent,
    ReplyPreview,
    RuntimeMCPConfig,
)
from cloud_agent_host.sandbox import STATE_MOUNT_TARGET
from cloud_agent_host.stream_deltas import DeltaBatcher, StreamDelta
from cloud_agent_shared.event_payloads import subagent_owned
from cloud_agent_shared.shared_protocol import (
    INPUT_PROCESS_GONE,
    INPUT_REFUSED,
    INPUT_TURN_ENDED,
    INPUT_UNSUPPORTED,
    USER_MESSAGE_EVENT,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cloud_agent_host.sandbox import SandboxProcess


class DialectRuntime(Protocol):
    """The dialect surface the driver, session and turn layers actually use.

    Narrower than :class:`AgentRuntime` on purpose: a runtime that speaks a
    dialect but cannot hold a resident live process stays describable without
    being typed as one that can.
    """

    name: str

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Map one output line to its normalized event, or ``None``."""
        ...


class ResidentLiveRuntime(DialectRuntime, Protocol):
    """The narrow live contract the four JSON-resident runtimes share.

    Claude is absent deliberately: its ``prepare_live`` takes ``config_dir``
    where Codex, pi, OpenCode and Kilo take ``session_state_dir`` and
    ``activation_dir``, so Claude's sessions name :class:`ClaudeCodeRuntime`
    directly rather than pretending the signatures are the same.
    """

    session_id: str | None

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        model_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the argv and environment a resident live process starts with."""
        ...


_RuntimeT = TypeVar("_RuntimeT", bound=DialectRuntime)
_RuntimeT_co = TypeVar("_RuntimeT_co", bound=DialectRuntime, covariant=True)
_LiveRuntimeT = TypeVar("_LiveRuntimeT", bound=ResidentLiveRuntime)

# Floor events: the minimum any driver must yield around a turn,
# synthesized here so a dialect adapter cannot forget them. They are the
# runner's control-flow signal, not stream rows — the platform's stored
# vocabulary stays the runtimes.py kinds.
TURN_STARTED = "turn_started"
TURN_COMPLETED = "turn_completed"
TURN_FAILED = "turn_failed"
FLOOR_EVENT_TYPES = frozenset({TURN_STARTED, TURN_COMPLETED, TURN_FAILED})


class DriverError(Exception):
    """Raised when a driver cannot honour a request."""


class SessionStateUnavailable(DriverError):
    """The retained native state or session locator is unavailable."""


@dataclass(frozen=True)
class HarnessCapabilities:
    """What this harness can do, in the design's semantic vocabulary.

    Values state what is *implemented and wired*, never what the upstream CLI
    could theoretically do.
    """

    continuity: Literal["resume"] = "resume"
    #: ``mid_turn``: :meth:`HarnessTurn.inject` hands a message to the running
    #: turn and the host advertises it; ``none``: a message waits for the turn.
    steering: Literal["mid_turn", "queue_only", "none"] = "none"
    fork: Literal["at_anchor", "none"] = "none"
    #: Whether the host supervisor, rather than an individual runner turn,
    #: can own one bidirectional CLI transport across several messages.
    resident_transport: Literal["supervisor_live", "none"] = "none"
    #: A conformance claim, not an upstream marketing claim: two separately
    #: credentialed resident processes may safely operate in one workspace.
    concurrent_shared_workspace: bool = False
    # A live process must rotate when a session changes model configuration.
    model_switch: Literal["restart", "none"] = "restart"


@dataclass(frozen=True)
class InputOutcome:
    """Whether a running turn has a message, and if not, which shared word says why.

    Accepted means the turn holds it: it will be put in front of the model at
    the runtime's next chance, and the turn's stream says so with a
    ``USER_MESSAGE_EVENT`` when it is. A turn that ends first never took it.
    """

    accepted: bool
    reason: str | None = None


@dataclass(frozen=True)
class PendingInput:
    """A message the owner chose to send into a running turn."""

    message_id: str
    text: str


@dataclass(frozen=True)
class _PendingQuestion:
    """One private native responder and its public question snapshot."""

    snapshot: dict[str, Any]
    respond: Callable[[dict[str, list[str]], bool], None]


def question_choices(value: Any, *, dialect: str) -> list[dict[str, Any]]:
    """Read native questions without giving their identifiers transport authority."""
    if not isinstance(value, list) or not value or len(value) > 10:
        return []
    questions: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, native in enumerate(value):
        if not isinstance(native, dict):
            return []
        question_id = native.get("id") if dialect == "codex" else str(index)
        text, header = native.get("question"), native.get("header")
        if (
            not isinstance(question_id, str)
            or not question_id
            or len(question_id) > 128
            or question_id in ids
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 10_000
            or not isinstance(header, str)
            or len(header) > 1_000
        ):
            return []
        ids.add(question_id)
        choices = native.get("options")
        if choices is None and dialect == "codex":
            choices = []
        if not isinstance(choices, list) or len(choices) > 20:
            return []
        options: list[dict[str, str]] = []
        for option in choices:
            if not isinstance(option, dict):
                return []
            label, description = option.get("label"), option.get("description", "")
            if (
                not isinstance(label, str)
                or not label.strip()
                or len(label) > 1_000
                or not isinstance(description, str)
                or len(description) > 10_000
            ):
                return []
            options.append({"label": label, "description": description})
        questions.append(
            {
                "id": question_id,
                "header": header,
                "question": text,
                "multiple": native.get("multiSelect") is True
                if dialect == "claude"
                else native.get("multiple") is True,
                "options": options,
                "allow_custom": dialect == "claude"
                or native.get("isOther") is True
                or (dialect == "codex" and not options)
                or (dialect == "http" and native.get("custom", True) is not False),
            }
        )
    return questions


def user_message_event(pending: PendingInput, *, raw_line: str | None = None) -> HarnessEvent:
    """The stream event saying the turn took ``pending`` at this point."""
    return HarnessEvent(
        USER_MESSAGE_EVENT,
        {"text": pending.text, "pending_message_id": pending.message_id},
        raw_line=raw_line,
    )


#: Prefix of the request id a Codex steer is sent under. A reply that arrives
#: after its turn has settled is recognised by it and dropped, rather than being
#: read by the next turn as that turn's own protocol error.
STEER_REQUEST_PREFIX = "steer"


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
    """The platform-facing outcome of one turn.

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
    # The CLI returned a native turn boundary, even if the request failed.
    # False for unproven process/transport failures and unsupported dialects.
    native_turn_settled: bool = False

    def __post_init__(self) -> None:
        if self.status == "completed" and not self.final_output.strip():
            raise ValueError("a completed turn must carry a non-empty final_output")


@dataclass(frozen=True)
class LaunchContext:
    """Everything a session needs that is not the turn itself.

    ``spawn`` is the sandbox boundary made explicit: the *platform* places the
    process inside the sandbox (no driver may create a harness process
    anywhere else), and the driver owns everything that flows over the
    returned handle's transport.
    """

    workdir: str
    model_base_url: str
    credential: str
    provides_isolation: bool
    base_env: dict[str, str] = field(default_factory=dict)
    mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG
    #: Stable provider-state root inside the retained container. ``None`` is
    #: accepted only for non-resident dialect tests and setup operations.
    state_dir: str | None = None
    #: Provider-compatible member-private state root inside the same container.
    session_state_dir: str = STATE_MOUNT_TARGET
    #: Spawns with a live stdin (SandboxSpec.live_input). ``None`` means this
    #: caller does not offer residency; ``launch_live`` refuses without it.
    spawn_live: Callable[[list[str], dict[str, str]], SandboxProcess] | None = None
    #: Private per-membership tmpfs root for generated provider configuration.
    #: It is never the durable native session store.
    activation_dir: str | None = None
    #: Retained containers own their state inside Docker rather than at a host
    #: path. They provide this trusted writer so Claude's passive hooks are
    #: installed in the filesystem the CLI will actually read.
    install_tool_hooks: Callable[[dict[str, Any]], None] | None = None
    #: Claude launch settings and its process registry are member-private;
    #: only native project history is linked to the group's shared store.
    claude_config_dir: str | None = None


class HarnessTurn(Generic[_RuntimeT]):
    """One executing turn: its event stream, cancellation, and result."""

    def __init__(self, session: HarnessSession[_RuntimeT], process: SandboxProcess) -> None:
        self._session = session
        self._process = process
        self._runtime = session.runtime
        self._cancelled = False
        self._final_messages: list[str] = []
        self._result: TurnResult | None = None
        self._questions_lock = threading.Lock()
        self._questions: dict[str, _PendingQuestion] = {}
        self._question_records: dict[str, _PendingQuestion] = {}
        self._question_keys: dict[str, str] = {}
        self._questions_closed = False

    def remember_question(
        self,
        native_key: str,
        questions: list[dict[str, Any]],
        respond: Callable[[dict[str, list[str]], bool], None],
    ) -> None:
        """Register a request while the stream keeps reading the active turn."""
        with self._questions_lock:
            if self._questions_closed or self._cancelled or native_key in self._question_keys:
                return
            request_id = uuid.uuid4().hex
            self._question_keys[native_key] = request_id
            pending = _PendingQuestion({"request_id": request_id, "questions": questions}, respond)
            self._questions[request_id] = pending
            self._question_records[native_key] = pending

    def _clear_questions(self) -> None:
        with self._questions_lock:
            self._questions_closed = True
            self._questions.clear()
            self._question_records.clear()

    def forget_question(self, native_key: str) -> None:
        """Drop the request the CLI resolved or withdrew itself."""
        with self._questions_lock:
            request_id = self._question_keys.get(native_key)
            if request_id is not None:
                self._questions.pop(request_id, None)
            self._question_records.pop(native_key, None)

    def _restore_question(self, native_key: str) -> None:
        """A bridge POST failed: offer a fresh token unless the CLI resolved it meanwhile."""
        with self._questions_lock:
            pending = self._question_records.get(native_key)
            if self._questions_closed or pending is None:
                return
            request_id = uuid.uuid4().hex
            restored = _PendingQuestion(
                {**pending.snapshot, "request_id": request_id}, pending.respond
            )
            self._question_keys[native_key] = request_id
            self._question_records[native_key] = restored
            self._questions[request_id] = restored

    def pending_questions(self) -> list[dict[str, Any]]:
        """Read this turn's unanswered requests; snapshots cannot mutate the CLI input."""
        with self._questions_lock:
            return [copy.deepcopy(question.snapshot) for question in self._questions.values()]

    def answer_question(
        self, request_id: str, answers: dict[str, list[str]], *, reject: bool = False
    ) -> InputOutcome:
        """Reply once to an exact active request, even when two tabs submit together."""
        with self._questions_lock:
            if self._questions_closed or self._cancelled or self._result is not None:
                return InputOutcome(False, INPUT_TURN_ENDED)
            pending = self._questions.get(request_id)
            if pending is None:
                return InputOutcome(False, INPUT_REFUSED)
            questions = pending.snapshot["questions"]
            if not reject:
                if not isinstance(answers, dict) or set(answers) != {q["id"] for q in questions}:
                    return InputOutcome(False, INPUT_REFUSED)
                for question in questions:
                    selected = answers[question["id"]]
                    if (
                        not isinstance(selected, list)
                        or not selected
                        or len(selected) > 20
                        or (not question["multiple"] and len(selected) != 1)
                        or any(
                            not isinstance(value, str) or not value.strip() or len(value) > 10_000
                            for value in selected
                        )
                        or len(set(selected)) != len(selected)
                        or (
                            not question["allow_custom"]
                            and any(
                                value not in {option["label"] for option in question["options"]}
                                for value in selected
                            )
                        )
                    ):
                        return InputOutcome(False, INPUT_REFUSED)
            try:
                pending.respond(answers, reject)
            except (DriverError, OSError):
                return InputOutcome(False, INPUT_PROCESS_GONE)
            self._questions.pop(request_id)
            return InputOutcome(True)

    def iter_events(self) -> Iterator[HarnessEvent]:
        """Yield floor and dialect events as the harness produces them."""
        raise NotImplementedError

    def events_for_line(self, line: str) -> list[HarnessEvent]:
        """Every event one stdout line stands for; usually exactly one."""
        raise NotImplementedError

    def flush_events(self) -> list[HarnessEvent]:
        """Rows still gathered from earlier lines; none unless a driver batches."""
        return []

    def event_for_line(self, line: str) -> HarnessEvent:
        """Map one stdout line to its event; :data:`LINE` when there is none."""
        event = self._runtime.parse_event(line)
        if event is None:
            return HarnessEvent(LINE, raw_line=line)
        if event.event_type == MESSAGE and not subagent_owned(event.payload):
            text = str((event.payload or {}).get("text", ""))
            if text.strip():
                self._final_messages.append(text)
        return HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)

    def raw_lines(self) -> Iterator[str]:
        """The transport's line stream, for callers that pump on their own thread."""
        raise NotImplementedError

    def stderr_text(self) -> str:
        """Whatever the harness wrote to stderr, once it has exited."""
        return self._process.stderr_text()

    def cancel(self) -> None:
        """Stop this turn now."""
        raise NotImplementedError

    def inject(self, text: str, *, message_id: str) -> InputOutcome:
        """Hand ``text`` to this running turn, as the owner's next message in it.

        Called from a thread other than the one reading the turn's stream.
        """
        raise NotImplementedError

    def wait(self) -> TurnResult:
        """Wait for exit and return the contract-checked result."""
        raise NotImplementedError

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
            # Tool-only runs still owe the owner a displayable line. This
            # text is deliberately explicit about being synthesized — a driver
            # must never put words in the agent's mouth.
            final = "(the agent finished without a closing message)"
        return TurnResult(status="completed", final_output=final, exit_code=exit_code)


class HarnessSession(Generic[_RuntimeT]):
    """Common state for a live Agent CLI session."""

    def __init__(self, driver: LiveDriver[_RuntimeT], context: LaunchContext) -> None:
        self._driver = driver
        self._context = context
        self._closed = False
        self.retire_after_turn = False

    @property
    def runtime(self) -> _RuntimeT:
        """The dialect adapter this session speaks through."""
        return self._driver.runtime

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has ended this session."""
        return self._closed

    def turn_finished(self) -> None:
        """A settled turn leaves the live session ready for another turn."""
        return

    def close(self) -> None:
        """Mark a session closed; concrete transports release their process."""
        self._closed = True

    def stderr_text(self) -> str:
        """Return bounded process diagnostics when this session owns one."""
        return ""

    def process_exit_code(self) -> int | None:
        """Return a resident process status when this session owns one."""
        return None

    def ensure_started(self, *, model: str, reasoning_effort: str | None) -> None:
        """Start the resident process and prove it is ready for a turn."""
        raise NotImplementedError

    def process_alive(self) -> bool:
        """Whether the resident process still runs."""
        raise NotImplementedError

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn[_RuntimeT]:
        """Send one prompt to this already-open native session."""
        raise NotImplementedError


class SessionType(Protocol):
    """How :class:`LiveDriver` constructs the session class of its CLI."""

    def __call__(
        self,
        driver: LiveDriver[Any],
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> HarnessSession[Any]:
        """Open a session, new or resuming ``resume_session_id``."""
        ...


class LiveDriver(Generic[_RuntimeT_co]):
    """The live transport of one registered Agent CLI, owned by the host supervisor.

    Every CLI holds one resident process per member: it resumes natively,
    forks at an anchor, restarts on a model change, and runs beside its
    siblings in one workspace. CLIs differ only in the session class that
    speaks their protocol, and in whether a message reaches a running turn
    (:meth:`HarnessTurn.inject`). :mod:`cloud_agent_host.runtimes.live` records both.
    """

    def __init__(self, runtime: _RuntimeT_co, *, session_type: SessionType, mid_turn: bool) -> None:
        self._runtime = runtime
        self._session_type = session_type
        self._mid_turn = mid_turn

    @property
    def runtime(self) -> _RuntimeT_co:
        """The dialect adapter this driver speaks through."""
        return self._runtime

    def capabilities(self) -> HarnessCapabilities:
        """The semantic capability row for this live harness."""
        return HarnessCapabilities(
            continuity="resume",
            steering="mid_turn" if self._mid_turn else "none",
            fork="at_anchor",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(self, context: LaunchContext) -> HarnessSession[Any]:
        """Open a new persistent session."""
        return self._session_type(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> HarnessSession[Any]:
        """Resume one native session through its persistent transport."""
        if context.state_dir is None:
            raise SessionStateUnavailable(
                f"resuming a {self.runtime.name} session needs its native session state"
            )
        return self._session_type(self, context, resume_session_id=session_id)


class _ProtocolReply:
    """One JSON-RPC answer awaited by a thread that does not read the stream."""

    def __init__(self) -> None:
        self._settled = threading.Event()
        self._value: dict[str, Any] | None = None

    def settle(self, value: dict[str, Any]) -> None:
        self._value = value
        self._settled.set()

    def wait(self, timeout: float) -> dict[str, Any] | None:
        """The answer, or ``None`` if it did not come in ``timeout`` seconds."""
        return self._value if self._settled.wait(timeout) else None


class _InputReceipt:
    """One turn-local message-ID receipt shared by concurrent sandbox manager retries."""

    def __init__(self) -> None:
        self._settled = threading.Event()
        self._outcome: InputOutcome | None = None
        self._error: BaseException | None = None

    def settle(self, outcome: InputOutcome) -> None:
        self._outcome = outcome
        self._settled.set()

    def fail(self, error: BaseException) -> None:
        """Replay an uncertain native failure to every concurrent waiter."""
        self._error = error
        self._settled.set()

    def wait(self) -> InputOutcome:
        """Wait for the one native write attempt and replay its answer."""
        self._settled.wait()
        if self._error is not None:
            raise self._error
        if self._outcome is None:  # pragma: no cover - settle writes before set
            raise DriverError("input receipt settled without an outcome")
        return self._outcome


class JsonResidentTurn(HarnessTurn[_LiveRuntimeT], Generic[_LiveRuntimeT]):
    """One framed activation on a process whose stdout survives the turn."""

    def __init__(
        self, session: JsonResidentSession[_LiveRuntimeT], process: SandboxProcess
    ) -> None:
        super().__init__(session, process)
        self._session: JsonResidentSession[_LiveRuntimeT] = session
        self._saw_result = False
        self._saw_native_result = False
        self._protocol_failed = False
        self.protocol_turn_id: str | None = None
        # Several native protocols publish mutable updates for one tool item.
        # Keep this turn-scoped: item identifiers may be reused on later turns.
        self.reported_tools: set[str] = set()
        #: Batches the answer text the model is writing, for the same reason.
        self.preview = ReplyPreview()
        #: Merges the other per-token streams, which are stored rather than
        #: previewed: reasoning text and command output.
        self.deltas = DeltaBatcher()
        # Answers to requests sent while the turn runs. Only the stream reads
        # stdout, so it hands each one over instead of the sender reading it.
        self._replies: dict[str, _ProtocolReply] = {}
        # The message each steer carries, kept after its sender stops waiting:
        # the stream reports the message taken whenever its answer arrives.
        self._steers: dict[str, PendingInput] = {}
        self._replies_lock = threading.Lock()
        # The control plane may lose the sandbox manager's accepted response and replay
        # the same durable handoff after its lease expires.  One message ID is
        # one native steer for this exact turn, including concurrent retries.
        self._input_receipts: dict[str, _InputReceipt] = {}
        self._input_receipts_lock = threading.Lock()
        self._question_commands: dict[str, str] = {}

    def track_question_reply(self, command_id: str, native_key: str) -> None:
        """Remember a bridge command whose failure makes its question retryable."""
        with self._replies_lock:
            self._question_commands[command_id] = native_key

    def settle_question_reply(self, value: dict[str, Any]) -> None:
        """Resolve bridge acknowledgements without waiting inside either turn lock."""
        if value.get("bridge") not in {"question_answered", "command_error"}:
            return
        with self._replies_lock:
            native_key = self._question_commands.pop(str(value.get("id") or ""), None)
        if native_key is not None:
            if value.get("bridge") == "command_error":
                self._restore_question(native_key)
            else:
                self.forget_question(native_key)

    def raw_lines(self) -> Iterator[str]:
        """Yield this activation's protocol lines through its settling one."""
        for line in self._session.turn_lines(self):
            settled, failed, native = self._session.line_settles_turn(line, self)
            if settled:
                self._clear_questions()
            yield line
            if settled:
                self._saw_result = True
                self._saw_native_result = native
                self._protocol_failed = failed
                self._process.poll()
                return

    def iter_events(self) -> Iterator[HarnessEvent]:
        """Yield the turn's events between its floor events."""
        yield HarnessEvent(TURN_STARTED)
        try:
            for line in self.raw_lines():
                yield from self.events_for_line(line)
        except Exception:
            yield from self.flush_events()
            raise
        yield self.finish_event()

    def events_for_line(self, line: str) -> list[HarnessEvent]:
        """Map one protocol line to the events it yields, batching stream deltas."""
        delta = self._session.stream_delta(line, self)
        if delta is not None:
            rows = self.deltas.add(delta)
            if self.deltas.holding:
                rows.append(HarnessEvent(LINE, raw_line=line))
            return rows
        # Anything else ends the gathered piece first, so it stays in order.
        flushed = self.deltas.flush()
        reply = self._reply_event(line)
        if reply is not None:
            return [*flushed, reply]
        events = self._session.events_for_protocol_line(line, self)
        for event in events:
            if event.event_type == MESSAGE and not subagent_owned(event.payload):
                text = str(event.payload.get("text", ""))
                if text.strip():
                    self._final_messages.append(text)
        return [*flushed, *events]

    def flush_events(self) -> list[HarnessEvent]:
        """Report the deltas still gathered, as a stream that broke must."""
        return self.deltas.flush()

    def event_for_line(self, line: str) -> HarnessEvent:
        """Map one protocol line to its first event."""
        return self.events_for_line(line)[0]

    def expect_reply(self, request_id: str) -> _ProtocolReply:
        """Register a request whose answer this turn's stream will hand over."""
        reply = _ProtocolReply()
        with self._replies_lock:
            self._replies[request_id] = reply
        return reply

    def expect_steer(self, request_id: str, pending: PendingInput) -> _ProtocolReply:
        """Register a steer: its sender waits, and the stream reports it taken."""
        with self._replies_lock:
            self._steers[request_id] = pending
        return self.expect_reply(request_id)

    def forget_reply(self, request_id: str) -> None:
        """Stop waiting; a late answer is then dropped by its prefix."""
        with self._replies_lock:
            self._replies.pop(request_id, None)

    def _reply_event(self, line: str) -> HarnessEvent | None:
        """What ``line`` is if it answers a request sent mid-turn, else ``None``.

        An answer is not an event of its own, except the one that accepts a
        steer: that is where the turn took the owner's message.
        """
        value = self._session._object(line)
        if value is None or "method" in value or ("result" not in value and "error" not in value):
            return None
        request_id = value.get("id")
        if not isinstance(request_id, str):
            return None
        with self._replies_lock:
            reply = self._replies.pop(request_id, None)
            steer = self._steers.pop(request_id, None)
        if reply is not None:
            reply.settle(value)
        if steer is not None and value.get("error") is None:
            return user_message_event(steer, raw_line=line)
        if (
            reply is None
            and steer is None
            and not request_id.startswith(f"{STEER_REQUEST_PREFIX}-")
        ):
            return None
        # Answered after its turn settled, or a refusal: not this turn's error,
        # and not something anyone is still waiting for.
        return HarnessEvent(LINE, raw_line=line)

    def inject(self, text: str, *, message_id: str) -> InputOutcome:
        """Hand ``text`` to the running turn through the session's native seam."""
        with self._input_receipts_lock:
            receipt = self._input_receipts.get(message_id)
            if receipt is None:
                if self._cancelled or self._saw_result or self._result is not None:
                    return InputOutcome(False, INPUT_TURN_ENDED)
                receipt = _InputReceipt()
                self._input_receipts[message_id] = receipt
                owner = True
            else:
                owner = False
        if not owner:
            return receipt.wait()
        try:
            outcome = self._session.inject_input(self, PendingInput(message_id, text))
        except BaseException as exc:
            # The native call may have taken the message before failing. Every
            # concurrent duplicate must preserve that uncertainty rather than
            # turn it into an explicit refusal that the control plane requeues.
            receipt.fail(exc)
            with self._input_receipts_lock:
                self._input_receipts.pop(message_id, None)
            raise
        receipt.settle(outcome)
        if not outcome.accepted:
            with self._input_receipts_lock:
                self._input_receipts.pop(message_id, None)
        return outcome

    def cancel(self) -> None:
        """Use the exact native interrupt; preserve the resident transport."""
        if self._cancelled:
            return
        self._cancelled = True
        self._clear_questions()
        self._session.interrupt_turn(self)

    def wait(self) -> TurnResult:
        """Drain the activation to its settling line and return its result."""
        if self._result is None:
            if not self._saw_result:
                for line in self.raw_lines():
                    self.events_for_line(line)
            if self._saw_result:
                exit_code = 1 if self._protocol_failed else 0
            else:
                exit_code = self._process.wait() or 1
            self._result = replace(
                self._build_result(exit_code), native_turn_settled=self._saw_native_result
            )
            self._clear_questions()
            self._session.turn_finished()
        return self._result


class JsonResidentSession(HarnessSession[_LiveRuntimeT], Generic[_LiveRuntimeT]):
    """Common process lifetime and framing for exact native JSON protocols."""

    def __init__(
        self,
        driver: LiveDriver[_LiveRuntimeT],
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
        self._live_turn: JsonResidentTurn[_LiveRuntimeT] | None = None
        self._request_sequence = 0
        self._prefetched: list[str] = []
        # A mid-turn message is written from the sandbox manager's request thread while
        # the turn's own thread may be interrupting: one line at a time.
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def _request_id(self, prefix: str = "req") -> str:
        with self._request_lock:
            self._request_sequence += 1
            return f"{prefix}-{self._request_sequence}"

    def _write_json(self, value: dict[str, Any]) -> None:
        if self._process is None:
            raise DriverError("resident process has not started")
        try:
            with self._write_lock:
                self._process.write_line(json.dumps(value, separators=(",", ":")))
        except OSError as exc:
            raise DriverError(f"resident process is gone: {exc}") from exc

    def inject_input(
        self, turn: JsonResidentTurn[_LiveRuntimeT], pending: PendingInput
    ) -> InputOutcome:
        """Hand ``pending`` to ``turn`` mid-turn; a protocol without a seam declines."""
        return InputOutcome(False, INPUT_UNSUPPORTED)

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
            # crosses the sandbox manager boundary.  Agent credentials are supplied in
            # environment variables, never in the CLI's argv; the host's
            # ``docker exec`` names them without their values, which reach
            # the Docker client through its environment, not its argv (see
            # ``ChatSandboxManager.build_exec_command``). Avoid reflecting
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
        return self.runtime.prepare_live(
            workdir=self._context.workdir,
            model=model,
            model_base_url=self._context.model_base_url,
            credential=self._context.credential,
            provides_isolation=self._context.provides_isolation,
            mcp_config=self._context.mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=self._context.session_state_dir,
            resume_session_id=self._resume_session_id,
            activation_dir=self._context.activation_dir,
        )

    def ensure_started(self, *, model: str, reasoning_effort: str | None) -> None:
        """Spawn and initialize the resident process unless it is already running."""
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

    def _new_turn(self) -> JsonResidentTurn[_LiveRuntimeT]:
        assert self._process is not None
        return JsonResidentTurn(self, self._process)

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn[_LiveRuntimeT]:
        """Send one prompt to the resident process as a new activation."""
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

    def _start_protocol_turn(
        self, turn_input: TurnInput, turn: JsonResidentTurn[_LiveRuntimeT]
    ) -> None:
        raise NotImplementedError

    def turn_lines(self, turn: JsonResidentTurn[_LiveRuntimeT]) -> Iterator[str]:
        """Yield the process's protocol lines for ``turn``, prefetched ones first."""
        while self._prefetched:
            yield self._prefetched.pop(0)
        while True:
            yield self._next_line()

    def line_settles_turn(
        self, line: str, turn: JsonResidentTurn[_LiveRuntimeT]
    ) -> tuple[bool, bool, bool]:
        """Return terminal, failed, and proof of a native turn boundary."""
        raise NotImplementedError

    def event_for_protocol_line(
        self, line: str, turn: JsonResidentTurn[_LiveRuntimeT]
    ) -> HarnessEvent:
        """Map one protocol line of ``turn`` to its event; each CLI's session defines it."""
        raise NotImplementedError

    def stream_delta(self, line: str, turn: JsonResidentTurn[_LiveRuntimeT]) -> StreamDelta | None:
        """The per-token delta ``line`` is, if it is one to merge; see :class:`DeltaBatcher`."""
        return None

    def events_for_protocol_line(
        self, line: str, turn: JsonResidentTurn[_LiveRuntimeT]
    ) -> list[HarnessEvent]:
        """Every event one protocol line stands for; usually exactly one."""
        return [self.event_for_protocol_line(line, turn)]

    def interrupt_turn(self, turn: JsonResidentTurn[_LiveRuntimeT]) -> None:
        """Interrupt ``turn`` through the CLI's native seam; each CLI's session defines it."""
        raise NotImplementedError

    def turn_finished(self) -> None:
        """Hook run once an activation has settled; nothing by default."""
        return

    def process_alive(self) -> bool:
        """Whether the resident process is still running and the session open."""
        return not self._closed and self._process is not None and self._process.poll() is None

    def stderr_text(self) -> str:
        """Return the resident process's retained stderr tail."""
        return "" if self._process is None else self._process.stderr_text()

    def process_exit_code(self) -> int | None:
        """Return the resident process's host-side exit status, if known."""
        return None if self._process is None else self._process.poll()

    def close(self) -> None:
        """Close the session and end its resident process."""
        self._closed = True
        if self._process is not None:
            self._process.close_stdin()
            self._process.kill()
            self._process.wait()
