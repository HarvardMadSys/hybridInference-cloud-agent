"""Driver → Session → Turn: the platform's handle on an Agent CLI.

This is the object model used by the retained native-session design
(``docs/specs/2026-08-31-native-session-fork-shared-workspace-design.md``),
layered over the dialect adapters in :mod:`cloud_agent_host.runtimes`:

- **runtimes.py** stays the *dialect* layer — how one CLI is invoked and how
  one line of its output maps onto normalized events. It knows nothing about
  sessions or the platform.
- **drivers.py** is the *contract* layer the supervisor drives: launch a live
  session, ``start_turn`` on it, iterate its events, ``wait`` for a
  :class:`TurnResult` whose guarantees the platform can lean on.

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

import contextlib
import copy
import json
import queue
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar

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
    ClaudeCodeRuntime,
    CodexRuntime,
    KiloRuntime,
    NormalizedEvent,
    OpencodeRuntime,
    PiRuntime,
    ReplyPreview,
    RuntimeMCPConfig,
    get_runtime,
)
from cloud_agent_host.sandbox import STATE_MOUNT_TARGET
from cloud_agent_host.tool_windows import TOOL_MARK_FILE, hook_settings
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
        gateway_base_url: str,
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

#: Yielded for a stdout line the dialect adapter had no event for. Carries the
#: raw line so the caller can keep its tail; never uploaded.
LINE = "line"


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


def _question_choices(value: Any, *, dialect: str) -> list[dict[str, Any]]:
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


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _record_text(record: dict[str, Any]) -> str:
    """The text blocks of one stream-json message record, joined."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


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
#: How long a steer waits for the CLI to say it took the message. The reply is
#: read by the turn's own stream, which never pauses while a turn is live.
STEER_REPLY_TIMEOUT_S = 10.0
#: Claude answers a message it had not read by its result as a new turn of its
#: own, at once (the pinned 2.1.220). How long to wait for that turn to start,
#: and then for it to end once interrupted, before giving up on it.
CLAUDE_UNANSWERED_START_S = 5.0
CLAUDE_UNANSWERED_END_S = 60.0


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
class LaunchContext:
    """Everything a session needs that is not the turn itself.

    ``spawn`` is the sandbox boundary made explicit: the *platform* places the
    process inside the sandbox (no driver may create a harness process
    anywhere else), and the driver owns everything that flows over the
    returned handle's transport.
    """

    workdir: str
    gateway_base_url: str
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

    def _remember_question(
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

    def _forget_question(self, native_key: str) -> None:
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
        """Yield floor and dialect events as the harness produces them.

        Blocking, line-driven iteration. The caller that must interleave
        deadline/lease checks reads the process on its own thread exactly as
        before — this iterator is the single place a line becomes an event.
        """
        yield HarnessEvent(TURN_STARTED)
        for line in self._process.lines():
            yield from self.events_for_line(line)
        yield self.finish_event()

    def events_for_line(self, line: str) -> list[HarnessEvent]:
        """Every event one stdout line stands for; usually exactly one."""
        return [self.event_for_line(line)]

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
        self._clear_questions()
        self._process.kill()

    def inject(self, text: str, *, message_id: str) -> InputOutcome:
        """Hand ``text`` to this running turn, as the owner's next message in it.

        A driver that declares ``steering="mid_turn"`` overrides this. Called
        from a thread other than the one reading the turn's stream.
        """
        return InputOutcome(False, INPUT_UNSUPPORTED)

    def wait(self) -> TurnResult:
        """Wait for exit and return the contract-checked result."""
        if self._result is None:
            exit_code = self._process.wait()
            self._result = self._build_result(exit_code)
            self._clear_questions()
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
            # Tool-only runs still owe the owner a displayable line. This
            # text is deliberately explicit about being synthesized — a driver
            # must never put words in the agent's mouth.
            final = "(the agent finished without a closing message)"
        return TurnResult(status="completed", final_output=final, exit_code=exit_code)


class HarnessSession(Generic[_RuntimeT]):
    """Common state for a live Agent CLI session."""

    def __init__(self, driver: HarnessDriver[_RuntimeT], context: LaunchContext) -> None:
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


class HarnessDriver(Generic[_RuntimeT_co]):
    """Base metadata for one registered live harness."""

    def __init__(self, runtime: _RuntimeT_co) -> None:
        self._runtime = runtime

    @property
    def runtime(self) -> _RuntimeT_co:
        """The dialect adapter this driver speaks through."""
        return self._runtime

    @property
    def id(self) -> str:
        """The registry id this driver answers for."""
        return self.runtime.name

    def capabilities(self) -> HarnessCapabilities:
        """The semantic capability row for this live harness."""
        raise NotImplementedError


class CodexDriver(HarnessDriver[CodexRuntime]):
    """Codex app-server over its persisted rollout files."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise Codex app-server live, interrupt, and native-fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="mid_turn",
            fork="at_anchor",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(self, context: LaunchContext) -> CodexAppServerSession:
        """Start a new persistent Codex app-server thread."""
        return CodexAppServerSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> CodexAppServerSession:
        """Resume one Codex thread through a persistent app-server transport."""
        if context.state_dir is None:
            raise SessionStateUnavailable("a resident Codex session needs native state")
        return CodexAppServerSession(self, context, resume_session_id=session_id)


class PiDriver(HarnessDriver[PiRuntime]):
    """pi RPC over its persisted JSONL session tree."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise Pi RPC live, interrupt, and prompt-free fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="none",
            fork="at_anchor",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(self, context: LaunchContext) -> PiRpcSession:
        """Start a new persistent Pi RPC session."""
        return PiRpcSession(self, context)

    def launch_resident_live(self, context: LaunchContext, *, session_id: str) -> PiRpcSession:
        """Resume one Pi session through its persistent RPC transport."""
        if context.state_dir is None:
            raise SessionStateUnavailable("a resident pi session needs native state")
        return PiRpcSession(self, context, resume_session_id=session_id)


class OpenCodeDriver(HarnessDriver[OpencodeRuntime]):
    """OpenCode serve over its XDG data store."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise OpenCode serve live, interrupt, and native-fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="none",
            fork="at_anchor",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(self, context: LaunchContext) -> HttpAgentLiveSession:
        """Start a new authenticated OpenCode serve session."""
        return HttpAgentLiveSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> HttpAgentLiveSession:
        """Resume one OpenCode session through its authenticated bridge."""
        if context.state_dir is None:
            raise SessionStateUnavailable("a resident OpenCode session needs native state")
        return HttpAgentLiveSession(self, context, resume_session_id=session_id)


class KiloDriver(HarnessDriver[KiloRuntime]):
    """Kilo serve over its XDG data store."""

    def capabilities(self) -> HarnessCapabilities:
        """Advertise Kilo serve live, interrupt, and native-fork support."""
        return HarnessCapabilities(
            continuity="resume",
            steering="none",
            fork="at_anchor",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(self, context: LaunchContext) -> HttpAgentLiveSession:
        """Start a new authenticated Kilo serve session."""
        return HttpAgentLiveSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> HttpAgentLiveSession:
        """Resume one Kilo session through its authenticated bridge."""
        if context.state_dir is None:
            raise SessionStateUnavailable("a resident Kilo session needs native state")
        return HttpAgentLiveSession(self, context, resume_session_id=session_id)


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


class _JsonResidentTurn(HarnessTurn[_LiveRuntimeT], Generic[_LiveRuntimeT]):
    """One framed activation on a process whose stdout survives the turn."""

    def __init__(
        self, session: _JsonResidentSession[_LiveRuntimeT], process: SandboxProcess
    ) -> None:
        super().__init__(session, process)
        self._session: _JsonResidentSession[_LiveRuntimeT] = session
        self._saw_result = False
        self._saw_native_result = False
        self._protocol_failed = False
        self.protocol_turn_id: str | None = None
        # Several native protocols publish mutable updates for one tool item.
        # Keep this turn-scoped: item identifiers may be reused on later turns.
        self.reported_tools: set[str] = set()
        #: Batches the answer text the model is writing, for the same reason.
        self.preview = ReplyPreview()
        # Answers to requests sent while the turn runs. Only the stream reads
        # stdout, so it hands each one over instead of the sender reading it.
        self._replies: dict[str, _ProtocolReply] = {}
        # The message each steer carries, kept after its sender stops waiting:
        # the stream reports the message taken whenever its answer arrives.
        self._steers: dict[str, PendingInput] = {}
        self._replies_lock = threading.Lock()
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
                self._forget_question(native_key)

    def raw_lines(self) -> Iterator[str]:
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
        yield HarnessEvent(TURN_STARTED)
        for line in self.raw_lines():
            yield from self.events_for_line(line)
        yield self.finish_event()

    def events_for_line(self, line: str) -> list[HarnessEvent]:
        reply = self._reply_event(line)
        if reply is not None:
            return [reply]
        events = self._session.events_for_protocol_line(line, self)
        for event in events:
            if event.event_type == MESSAGE:
                text = str(event.payload.get("text", ""))
                if text.strip():
                    self._final_messages.append(text)
        return events

    def event_for_line(self, line: str) -> HarnessEvent:
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
        if self._cancelled or self._saw_result or self._result is not None:
            return InputOutcome(False, INPUT_TURN_ENDED)
        return self._session.inject_input(self, PendingInput(message_id, text))

    def cancel(self) -> None:
        """Use the exact native interrupt; preserve the resident transport."""
        if self._cancelled:
            return
        self._cancelled = True
        self._clear_questions()
        self._session.interrupt_turn(self)

    def wait(self) -> TurnResult:
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


class _JsonResidentSession(HarnessSession[_LiveRuntimeT], Generic[_LiveRuntimeT]):
    """Common process lifetime and framing for exact native JSON protocols."""

    def __init__(
        self,
        driver: HarnessDriver[_LiveRuntimeT],
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
        self._live_turn: _JsonResidentTurn[_LiveRuntimeT] | None = None
        self._request_sequence = 0
        self._prefetched: list[str] = []
        # A mid-turn message is written from the broker's request thread while
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
        self, turn: _JsonResidentTurn[_LiveRuntimeT], pending: PendingInput
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

    def _new_turn(self) -> _JsonResidentTurn[_LiveRuntimeT]:
        assert self._process is not None
        return _JsonResidentTurn(self, self._process)

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn[_LiveRuntimeT]:
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
        self, turn_input: TurnInput, turn: _JsonResidentTurn[_LiveRuntimeT]
    ) -> None:
        raise NotImplementedError

    def turn_lines(self, turn: _JsonResidentTurn[_LiveRuntimeT]) -> Iterator[str]:
        while self._prefetched:
            yield self._prefetched.pop(0)
        while True:
            yield self._next_line()

    def line_settles_turn(
        self, line: str, turn: _JsonResidentTurn[_LiveRuntimeT]
    ) -> tuple[bool, bool, bool]:
        """Return terminal, failed, and proof of a native turn boundary."""
        raise NotImplementedError

    def event_for_protocol_line(
        self, line: str, turn: _JsonResidentTurn[_LiveRuntimeT]
    ) -> HarnessEvent:
        event = self.runtime.parse_event(line)
        if event is None:
            return HarnessEvent(LINE, raw_line=line)
        return HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)

    def events_for_protocol_line(
        self, line: str, turn: _JsonResidentTurn[_LiveRuntimeT]
    ) -> list[HarnessEvent]:
        """Every event one protocol line stands for; usually exactly one."""
        return [self.event_for_protocol_line(line, turn)]

    def interrupt_turn(self, turn: _JsonResidentTurn[_LiveRuntimeT]) -> None:
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


class CodexAppServerSession(_JsonResidentSession[CodexRuntime]):
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
                        "name": "hybridinference-cloud-agent",
                        "title": "Cloud Agent",
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

        if self._context.mcp_config.server_ids:
            status_id = self._request_id("mcp-status")
            self._write_json(
                {
                    "id": status_id,
                    "method": "mcpServerStatus/list",
                    "params": {"threadId": str(session_id), "detail": "full"},
                }
            )
            status = self._read_until(lambda value: value.get("id") == status_id)
            result = status.get("result")
            inventory = result.get("data", []) if isinstance(result, dict) else []
            session = next((entry for entry in inventory if entry.get("name") == "session"), None)
            if status.get("error") or not session or not session.get("serverInfo"):
                raise DriverError("Codex could not initialize the session MCP relay")

    def _start_protocol_turn(
        self, turn_input: TurnInput, turn: _JsonResidentTurn[CodexRuntime]
    ) -> None:
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

    def line_settles_turn(
        self, line: str, turn: _JsonResidentTurn[CodexRuntime]
    ) -> tuple[bool, bool, bool]:
        """Recognize the completion notification for this exact Codex turn."""
        value = self._object(line)
        if value is None or value.get("method") != "turn/completed":
            return False, False, False
        params = value.get("params")
        completed = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(completed, dict) or completed.get("id") != turn.protocol_turn_id:
            return False, False, False
        status = str(completed.get("status") or "").lower()
        return True, status not in {"completed", "interrupted"}, True

    def event_for_protocol_line(
        self, line: str, turn: _JsonResidentTurn[CodexRuntime]
    ) -> HarnessEvent:
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
        # Keep the app-server item identity separate from provider message/usage
        # identity. Trajectory can join deltas to this item without inventing a
        # provider request or attributing turn-wide usage to one reply.
        identity = {"native_item_id": str(item["id"])} if item.get("id") else {}
        raw_notification = HarnessEvent(
            RAW,
            {"text": line[:4000], "protocol": "codex-app-server", "notification": value},
            raw_line=line,
        )
        if method == "item/tool/requestUserInput":
            native_id = value.get("id")
            questions = _question_choices(params.get("questions"), dialect="codex")
            if (
                isinstance(native_id, (str, int))
                and not isinstance(native_id, bool)
                and params.get("threadId") == self.runtime.session_id
                and params.get("turnId") == turn.protocol_turn_id
                and questions
            ):

                def respond(answers: dict[str, list[str]], reject: bool) -> None:
                    # Codex's pinned protocol has no reject discriminator:
                    # an empty answers map is its skipped-question response.
                    self._write_json(
                        {
                            "id": native_id,
                            "result": {
                                "answers": {}
                                if reject
                                else {key: {"answers": values} for key, values in answers.items()}
                            },
                        }
                    )

                turn._remember_question(json.dumps(native_id), questions, respond)
            elif isinstance(native_id, (str, int)) and not isinstance(native_id, bool):
                self._write_json(
                    {
                        "id": native_id,
                        "error": {
                            "code": -32602,
                            "message": "Invalid or stale user question request.",
                        },
                    }
                )
            return raw_notification
        if method == "serverRequest/resolved" and params.get("threadId") == self.runtime.session_id:
            native_id = params.get("requestId")
            if isinstance(native_id, (str, int)) and not isinstance(native_id, bool):
                turn._forget_question(json.dumps(native_id))
            return raw_notification
        if method == "item/agentMessage/delta" and params.get("itemId"):
            # Batched rather than one stored notification per token.
            piece = turn.preview.add(str(params["itemId"]), str(params.get("delta") or ""))
            return (
                HarnessEvent(RAW, piece, raw_line=line)
                if piece
                else HarnessEvent(LINE, raw_line=line)
            )
        if method == "item/completed" and item_type == "agentMessage":
            preview = {"preview_id": str(item["id"])} if item.get("id") else {}
            turn.preview.finish(preview.get("preview_id", ""))
            return HarnessEvent(
                MESSAGE,
                {"text": str(item.get("text") or ""), **identity, **preview},
                raw_line=line,
            )
        if method == "item/completed" and item_type == "reasoning":
            text = "\n".join(str(value) for value in item.get("summary") or [])
            return HarnessEvent(THINKING, {"text": text[:4000], **identity}, raw_line=line)
        if method in {"item/started", "item/updated"} and item_type in {
            "commandExecution",
            "mcpToolCall",
        }:
            item_id = str(item.get("id") or "")
            if (item_id and item_id in turn.reported_tools) or (
                not item_id and method == "item/updated"
            ):
                return raw_notification
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
        return raw_notification

    def inject_input(
        self, turn: _JsonResidentTurn[CodexRuntime], pending: PendingInput
    ) -> InputOutcome:
        """Steer the running Codex turn: its next model request carries the text.

        ``turn/steer`` names the turn it expects, so a message can never land in
        a different turn than the one the owner saw running. Codex puts it in
        the same turn's next model request (verified against the pinned
        0.145.0); the turn's stream reports it taken where the acceptance
        arrives. An answer that is slow to come is still reported by the
        stream if it accepts, so waiting too long is not a refusal.
        """
        if not turn.protocol_turn_id:
            return InputOutcome(False, INPUT_TURN_ENDED)
        request_id = self._request_id(STEER_REQUEST_PREFIX)
        reply = turn.expect_steer(request_id, pending)
        try:
            self._write_json(
                {
                    "id": request_id,
                    "method": "turn/steer",
                    "params": {
                        "threadId": self.runtime.session_id,
                        "expectedTurnId": turn.protocol_turn_id,
                        "input": [{"type": "text", "text": pending.text}],
                    },
                }
            )
        except DriverError:
            turn.forget_reply(request_id)
            return InputOutcome(False, INPUT_PROCESS_GONE)
        answer = reply.wait(STEER_REPLY_TIMEOUT_S)
        if answer is None:
            turn.forget_reply(request_id)
            return InputOutcome(True)
        error = answer.get("error")
        if error is None:
            return InputOutcome(True)
        message = str(error.get("message") if isinstance(error, dict) else error)
        # Codex refuses with "no active turn to steer" once the turn is over and
        # "expected active turn id `X` but found `Y`" once another has begun:
        # either way the turn this message was for has ended.
        if "no active turn" in message or "expected active turn" in message:
            return InputOutcome(False, INPUT_TURN_ENDED)
        return InputOutcome(False, INPUT_REFUSED)

    def interrupt_turn(self, turn: _JsonResidentTurn[CodexRuntime]) -> None:
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

    def fork_native(
        self,
        *,
        source_session_id: str,
        operation_id: str,
        source_native_turn_no: int,
    ) -> str:
        """Fork Codex through the requested settled native turn, inclusive."""
        if source_session_id != self.runtime.session_id:
            raise DriverError("Codex fork source no longer matches the live thread")
        if source_native_turn_no < 1:
            raise DriverError("Codex fork turn must be positive")

        read_id = self._request_id(f"fork-read-{operation_id}")
        self._write_json(
            {
                "id": read_id,
                "method": "thread/read",
                "params": {"threadId": source_session_id, "includeTurns": True},
            }
        )
        read_response = self._read_until(lambda value: value.get("id") == read_id)
        if read_response.get("error") is not None:
            raise DriverError(f"Codex thread/read failed: {read_response['error']}")
        read_result = read_response.get("result")
        source_thread = read_result.get("thread") if isinstance(read_result, dict) else None
        turns = source_thread.get("turns") if isinstance(source_thread, dict) else None
        if not isinstance(turns, list) or source_native_turn_no > len(turns):
            raise DriverError(
                "Codex native history does not contain the requested fork turn "
                f"({source_native_turn_no} of {len(turns) if isinstance(turns, list) else 0})"
            )
        anchor = turns[source_native_turn_no - 1]
        anchor_id = anchor.get("id") if isinstance(anchor, dict) else None
        if not isinstance(anchor_id, str) or not anchor_id:
            raise DriverError("Codex fork turn has no native id")
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
                    # Pinned app-server 0.145.0 defines this as inclusive. The
                    # platform resolves its stable native-turn ordinal against
                    # the source thread at mutation time instead of persisting
                    # provider ids that a later fork may rewrite.
                    "lastTurnId": anchor_id,
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


class PiRpcSession(_JsonResidentSession[PiRuntime]):
    """pi 0.73.1 RPC mode with an explicit native session locator."""

    def __init__(
        self,
        driver: HarnessDriver[PiRuntime],
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__(driver, context, resume_session_id=resume_session_id)

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

    def _start_protocol_turn(
        self, turn_input: TurnInput, turn: _JsonResidentTurn[PiRuntime]
    ) -> None:
        request_id = self._request_id("prompt")
        turn.protocol_turn_id = request_id
        self._write_json({"id": request_id, "type": "prompt", "message": turn_input.prompt})

    def line_settles_turn(
        self, line: str, turn: _JsonResidentTurn[PiRuntime]
    ) -> tuple[bool, bool, bool]:
        """Recognize Pi's response failure or terminal agent-end event."""
        value = self._object(line)
        if value is None:
            return False, False, False
        if (
            value.get("type") == "response"
            and value.get("id") == turn.protocol_turn_id
            and value.get("success") is False
        ):
            return True, True, False
        if value.get("type") == "agent_end":
            return True, self.runtime.agent_error(value) is not None, True
        return False, False, False

    def event_for_protocol_line(
        self, line: str, turn: _JsonResidentTurn[PiRuntime]
    ) -> HarnessEvent:
        """Normalize one Pi RPC protocol event for the active turn."""
        event = self.runtime.parse_event(line)
        if event is None:
            return HarnessEvent(LINE, raw_line=line)
        return HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)

    def interrupt_turn(self, turn: _JsonResidentTurn[PiRuntime]) -> None:
        """Abort the active Pi RPC turn without closing its session process."""
        self._write_json({"id": self._request_id("abort"), "type": "abort"})

    def fork_native(
        self,
        *,
        source_session_id: str,
        operation_id: str,
        source_native_turn_no: int,
    ) -> str:
        """Fork Pi before the next prompt, or clone at the current leaf."""
        if source_session_id != self.runtime.session_id:
            raise DriverError("pi fork source no longer matches the live session")
        if source_native_turn_no < 1:
            raise DriverError("pi fork turn must be positive")

        list_id = self._request_id(f"fork-list-{operation_id}")
        self._write_json({"id": list_id, "type": "get_fork_messages"})
        listed = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == list_id
        )
        data = listed.get("data") if listed.get("success") is True else None
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list) or source_native_turn_no > len(messages):
            detail = listed.get("error") if listed.get("success") is not True else None
            raise DriverError(
                str(detail)
                if detail
                else "pi native history does not contain the requested fork turn "
                f"({source_native_turn_no} of "
                f"{len(messages) if isinstance(messages, list) else 0})"
            )

        fork_id = self._request_id(f"fork-{operation_id}")
        if source_native_turn_no < len(messages):
            # Pi's RPC `fork` branches immediately *before* the selected user
            # entry. Selecting turn N+1 therefore includes the complete Nth
            # exchange and excludes the prompt the user is about to edit.
            next_message = messages[source_native_turn_no]
            entry_id = next_message.get("entryId") if isinstance(next_message, dict) else None
            if not isinstance(entry_id, str) or not entry_id:
                raise DriverError("pi next fork message has no native entry id")
            command: dict[str, Any] = {"id": fork_id, "type": "fork", "entryId": entry_id}
            expected_command = "fork"
        else:
            command = {"id": fork_id, "type": "clone"}
            expected_command = "clone"
        self._write_json(command)
        forked = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == fork_id
        )
        fork_data = forked.get("data") if forked.get("success") is True else None
        if (
            forked.get("command") != expected_command
            or not isinstance(fork_data, dict)
            or fork_data.get("cancelled") is not False
        ):
            raise DriverError(f"pi native fork failed: {forked.get('error') or fork_data}")

        state_id = self._request_id(f"fork-state-{operation_id}")
        self._write_json({"id": state_id, "type": "get_state"})
        state = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == state_id
        )
        state_data = state.get("data") if state.get("success") is True else None
        child_id = state_data.get("sessionId") if isinstance(state_data, dict) else None
        if not isinstance(child_id, str) or not child_id or child_id == source_session_id:
            raise DriverError("pi native fork returned an invalid child session id")
        self.runtime.session_id = child_id
        return child_id


class HttpAgentLiveTurn(_JsonResidentTurn[ResidentLiveRuntime]):
    """One OpenCode/Kilo activation, deduplicating mutable part updates."""

    def __init__(self, session: HttpAgentLiveSession, process: SandboxProcess) -> None:
        super().__init__(session, process)
        self.text_parts: dict[str, tuple[str, str]] = {}
        self.message_roles: dict[str, str] = {}
        #: Each part's kind, from its first update: a ``message.part.delta``
        #: does not say whether it extends answer text or reasoning.
        self.part_types: dict[str, str] = {}


class HttpAgentLiveSession(_JsonResidentSession[ResidentLiveRuntime]):
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

    def _start_protocol_turn(
        self, turn_input: TurnInput, turn: _JsonResidentTurn[ResidentLiveRuntime]
    ) -> None:
        request_id = self._request_id("turn")
        turn.protocol_turn_id = request_id
        self._write_json({"id": request_id, "type": "turn", "prompt": turn_input.prompt})

    def line_settles_turn(
        self, line: str, turn: _JsonResidentTurn[ResidentLiveRuntime]
    ) -> tuple[bool, bool, bool]:
        """Recognize the bridge settlement or command failure for this turn."""
        value = self._object(line)
        if value is None:
            return False, False, False
        bridge = value.get("bridge")
        if bridge == "settled" and value.get("turn_id") == turn.protocol_turn_id:
            return True, value.get("status") != "completed", True
        if bridge in {"fatal", "command_error"}:
            command_id = value.get("id")
            if bridge == "fatal" or command_id in {"", turn.protocol_turn_id}:
                return True, True, False
        return False, False, False

    def event_for_protocol_line(
        self, line: str, turn: _JsonResidentTurn[ResidentLiveRuntime]
    ) -> HarnessEvent:
        """Normalize one OpenCode/Kilo bridge event for the active turn."""
        assert isinstance(turn, HttpAgentLiveTurn)
        value = self._object(line)
        if value is None:
            return HarnessEvent(LINE, raw_line=line)
        bridge = value.get("bridge")
        turn.settle_question_reply(value)
        if bridge in {"event", "settled"} and value.get("turn_id") != turn.protocol_turn_id:
            return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
        if bridge == "event":
            event = value.get("event")
            event = event if isinstance(event, dict) else {}
            event_type = event.get("type")
            properties = event.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            if event_type == "question.asked":
                native_id = properties.get("id")
                questions = _question_choices(properties.get("questions"), dialect="http")
                if (
                    isinstance(native_id, str)
                    and native_id
                    and properties.get("sessionID") == self.runtime.session_id
                    and questions
                ):
                    question_request_id: str = native_id

                    def respond(answers: dict[str, list[str]], reject: bool) -> None:
                        command_id = self._request_id("question")
                        turn.track_question_reply(command_id, question_request_id)
                        self._write_json(
                            {
                                "id": command_id,
                                "type": "question",
                                "turn_id": turn.protocol_turn_id,
                                "request_id": question_request_id,
                                "reject": reject,
                                "answers": []
                                if reject
                                else [answers[question["id"]] for question in questions],
                            }
                        )

                    turn._remember_question(question_request_id, questions, respond)
                elif (
                    isinstance(native_id, str)
                    and native_id
                    and properties.get("sessionID") == self.runtime.session_id
                ):
                    # A malformed native question must not leave a tool
                    # blocked behind an interaction the UI cannot present.
                    self._write_json(
                        {
                            "id": self._request_id("question"),
                            "type": "question",
                            "turn_id": turn.protocol_turn_id,
                            "request_id": native_id,
                            "reject": True,
                            "answers": [],
                        }
                    )
            elif event_type in {"question.replied", "question.rejected"}:
                native_id = properties.get("requestID")
                if isinstance(native_id, str):
                    turn._forget_question(native_id)
            part = properties.get("part")
            part = part if isinstance(part, dict) else {}
            part_type = part.get("type")
            part_id = str(part.get("id") or "")
            if event_type == "message.updated":
                info = properties.get("info")
                if isinstance(info, dict) and info.get("id"):
                    turn.message_roles[str(info["id"])] = str(info.get("role") or "")
            if event_type == "message.part.updated" and part_id and part_type:
                turn.part_types[part_id] = str(part_type)
            if event_type == "message.part.updated" and part_type == "text":
                if not part_id:
                    return HarnessEvent(LINE, raw_line=line)
                # User prompts also emit text parts. Resolve ownership at
                # settlement so even a part preceding its message metadata
                # cannot turn the user's input into an assistant reply.
                message_id = str(part.get("messageID") or "")
                turn.text_parts[part_id] = (message_id, str(part.get("text") or ""))
                timing = part.get("time")
                ended = isinstance(timing, dict) and bool(timing.get("end"))
                return self._answer_preview(turn, part_id, ended=ended, line=line)
            if (
                event_type == "message.part.delta"
                and properties.get("field") == "text"
                and turn.part_types.get(str(properties.get("partID") or "")) == "text"
            ):
                # The pinned 1.18.9 (and Kilo) send a text part as an empty
                # update, then one delta per token, then the complete update.
                delta_part = str(properties["partID"])
                owner, text = turn.text_parts.get(
                    delta_part, (str(properties.get("messageID") or ""), "")
                )
                turn.text_parts[delta_part] = (owner, text + str(properties.get("delta") or ""))
                return self._answer_preview(turn, delta_part, ended=False, line=line)
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
            # Reported by events_for_protocol_line, one message per part.
            return HarnessEvent(LIFECYCLE, {"phase": "result"}, raw_line=line)
        if bridge in {"fatal", "command_error"}:
            return HarnessEvent(
                ERROR, {"text": str(value.get("detail") or "")[:2000]}, raw_line=line
            )
        return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)

    @staticmethod
    def _answer_preview(
        turn: HttpAgentLiveTurn, part_id: str, *, ended: bool, line: str
    ) -> HarnessEvent:
        """Preview a text part, if it is already known to be the assistant's.

        A part whose message metadata arrives later is previewed from its next
        update on, with all the text it has by then.
        """
        message_id, text = turn.text_parts[part_id]
        if turn.message_roles.get(message_id) != "assistant":
            return HarnessEvent(LINE, raw_line=line)
        piece = turn.preview.grow_to(part_id, text)
        if piece is None and ended:
            # A finished part may sit before a long tool call; show all of it.
            piece = turn.preview.flush(part_id)
        if piece is None:
            return HarnessEvent(LINE, raw_line=line)
        return HarnessEvent(RAW, piece, raw_line=line)

    def events_for_protocol_line(
        self, line: str, turn: _JsonResidentTurn[ResidentLiveRuntime]
    ) -> list[HarnessEvent]:
        """At settlement, the assistant's answer: one message per text part.

        Each carries its part's ``preview_id``, so the complete text takes the
        place of its preview, between the tool calls it was written around.
        The turn's final output is still every part joined, as before.
        """
        assert isinstance(turn, HttpAgentLiveTurn)
        value = self._object(line)
        if (
            value is None
            or value.get("bridge") != "settled"
            or value.get("turn_id") != turn.protocol_turn_id
        ):
            return [self.event_for_protocol_line(line, turn)]
        turn.preview.clear()
        answer = [
            HarnessEvent(MESSAGE, {"text": text, "preview_id": part_id}, raw_line=line)
            for part_id, (message_id, text) in turn.text_parts.items()
            if text.strip() and turn.message_roles.get(message_id) == "assistant"
        ]
        return answer or [self.event_for_protocol_line(line, turn)]

    def interrupt_turn(self, turn: _JsonResidentTurn[ResidentLiveRuntime]) -> None:
        """Interrupt only the active bridge turn on the resident session."""
        self._write_json(
            {
                "id": self._request_id("interrupt"),
                "type": "interrupt",
                "turn_id": turn.protocol_turn_id,
            }
        )

    def fork_native(
        self,
        *,
        source_session_id: str,
        operation_id: str,
        source_native_turn_no: int,
    ) -> str:
        """Create an OpenCode/Kilo child at one settled native turn."""
        if source_session_id != self.runtime.session_id:
            raise DriverError(f"{self.runtime.name} fork source no longer matches")
        request_id = self._request_id(f"fork-{operation_id}")
        self._write_json(
            {
                "id": request_id,
                "type": "fork",
                "source_native_turn_no": source_native_turn_no,
            }
        )
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

#: The sandbox-side hooks append exact tool boundaries here. Keeping this on
#: the state mount makes it platform data rather than part of the repository,
#: and lets resident turns share one append-only stream safely.
CLAUDE_TOOL_MARKS = f"{STATE_MOUNT_TARGET}/{TOOL_MARK_FILE}"


class ClaudeCodeDriver(HarnessDriver[ClaudeCodeRuntime]):
    """Claude Code native live transport, resume, and immediate fork."""

    def capabilities(self) -> HarnessCapabilities:
        """Native resume plus the supervisor-owned stream-json pipe."""
        return HarnessCapabilities(
            continuity="resume",
            steering="mid_turn",
            fork="at_anchor",
            model_switch="restart",
            resident_transport="supervisor_live",
            concurrent_shared_workspace=True,
        )

    def launch_live(self, context: LaunchContext) -> ClaudeCodeLiveSession:
        """Start a fresh stdin-fed multi-turn native session."""
        return ClaudeCodeLiveSession(self, context)

    def launch_resident_live(
        self, context: LaunchContext, *, session_id: str
    ) -> ClaudeCodeLiveSession:
        """The HOT transport resumed over the retained native-state directory.

        The process stays alive after the turn settles and takes the next turn
        on stdin. It is only correct with a residency-lifetime credential in
        ``context.credential`` — the thread-attributed residency grant from the
        retained-session design's §6, renewed by host beats — because the pinned
        CLI runs its whole life on its launch key. That measured behavior is why
        the grant's lifetime and attribution follow the member residency.
        """
        if context.state_dir is None:
            raise SessionStateUnavailable("a resident turn needs native session state")
        return ClaudeCodeLiveSession(self, context, resume_session_id=session_id)


class ClaudeCodeLiveTurn(HarnessTurn[ClaudeCodeRuntime]):
    """One turn on a resident process: borrows the shared stdout, ends at result."""

    def __init__(self, session: ClaudeCodeLiveSession, process: SandboxProcess) -> None:
        super().__init__(session, process)
        self._session: ClaudeCodeLiveSession = session
        self._saw_result = False
        self._error_result = False
        self._saw_api_retry = False
        # Tool calls the model made that have no result yet. Claude folds a
        # message written while one is running into this turn's next model
        # request; written at any other moment it opens a turn of its own.
        self._open_tool_uses: set[str] = set()
        # Messages the owner sent into this turn: held until the model next
        # calls a tool, then written, then taken once the CLI echoes them.
        self._held: list[PendingInput] = []
        self._written: list[PendingInput] = []
        # Set when the stream reaches the result: nothing is taken after it.
        self._input_closed = False
        self._input_lock = threading.Lock()

    def event_for_line(self, line: str) -> HarnessEvent:
        """Preserve terminal failure independently of the resident process exit."""
        # Inspect the wire record: the runtime deduplicates unknown system
        # subtypes, so normalized events can hide a later retry notification.
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            record = None
        if isinstance(record, dict) and not record.get("parent_tool_use_id"):
            observed = str(record.get("session_id") or "").strip()
            expected = self._runtime.session_id or self._session._resume_session_id
            if observed and expected and observed != expected:
                # Check before the normalizer can overwrite its saved id or
                # expose another session's answer as a successful follow-up.
                raise DriverError("Claude reported a different native session")
        if isinstance(record, dict) and record.get("type") == "user" and record.get("isReplay"):
            # The CLI echoing a user message it just read: the prompt, or one
            # the owner sent into the turn. Never an event of its own.
            return HarnessEvent(LINE, raw_line=line)
        if isinstance(record, dict):
            self._track_questions(record)
            self._track_tool_calls(record)
        event = super().event_for_line(line)
        if isinstance(record, dict) and (
            record.get("type") == "system" and record.get("subtype") == "api_retry"
        ):
            self._saw_api_retry = True
        # A live process survives API/authentication failures too. Its result
        # boundary is not proof of success; honor the dialect's terminal error.
        if self._runtime.turn_settled(line) and event.event_type == ERROR:
            self._error_result = True
        return event

    def raw_lines(self) -> Iterator[str]:
        """This turn's slice of the shared stream, up to its result line."""
        for line in self._session.shared_lines():
            if self._runtime.turn_settled(line):
                self._clear_questions()
            yield line
            if self._runtime.turn_settled(line):
                self._saw_result = True
                with self._input_lock:
                    self._input_closed = True
                    self._held.clear()
                    unread, self._written = self._written, []
                if unread:
                    # Written, but the CLI had not read them by its result: it
                    # is about to answer them as a turn of its own. They are
                    # the next turn's, so that one is stopped and discarded.
                    self._session.discard_unanswered_turn()
                # Opportunistic reap: a process that died right after its
                # result must not linger as a zombie until the session closes.
                self._process.poll()
                return

    def iter_events(self) -> Iterator[HarnessEvent]:
        """Floor + dialect events for this turn only."""
        yield HarnessEvent(TURN_STARTED)
        for line in self.raw_lines():
            yield from self.events_for_line(line)
        yield self.finish_event()

    def events_for_line(self, line: str) -> list[HarnessEvent]:
        """The line's event, or the owner's messages the CLI just took.

        With ``--replay-user-messages`` the CLI echoes a message on stdout at
        the moment it reads it into the conversation: after the tool it was
        written during, before the model's next request (the pinned 2.1.220).
        That echo is where the turn took the message.
        """
        record = _json_object(line)
        if record is not None and record.get("type") == "user" and record.get("isReplay"):
            echoed = _record_text(record)
            with self._input_lock:
                taken = [pending for pending in self._written if pending.text in echoed]
                self._written = [pending for pending in self._written if pending not in taken]
            if taken:
                return [user_message_event(pending, raw_line=line) for pending in taken]
        return [self.event_for_line(line)]

    def wait(self) -> TurnResult:
        """Settle this turn without waiting for the resident process to die."""
        if self._result is None:
            if not self._cancelled and not self._saw_result:
                # Settling without having drained the slice: consume it now —
                # the boundary and the final message both live in the stream.
                for line in self.raw_lines():
                    self.events_for_line(line)
            # The process is deliberately still alive after a result. A stream
            # ending without one is a mid-turn death even if its code was zero.
            exit_code = int(self._error_result) if self._saw_result else self._process.wait() or 1
            self._result = replace(
                self._build_result(exit_code), native_turn_settled=self._saw_result
            )
            self._clear_questions()
            self._session.turn_finished()
        return self._result

    def cancel(self) -> None:
        """Send Claude's control ``interrupt`` without killing the session."""
        if self._cancelled:
            return
        self._cancelled = True
        self._clear_questions()
        self._session.interrupt_turn()

    def _track_questions(self, record: dict[str, Any]) -> None:
        """Hold AskUserQuestion's stdio permission request until its owner answers."""
        kind = record.get("type")
        native_id = record.get("request_id")
        if kind == "control_cancel_request" and isinstance(native_id, str):
            self._forget_question(native_id)
            return
        if kind == "result":
            self._clear_questions()
            return
        request = record.get("request")
        if (
            kind != "control_request"
            or not isinstance(native_id, str)
            or not native_id
            or not isinstance(request, dict)
            or request.get("subtype") != "can_use_tool"
        ):
            return
        original = request.get("input")
        if request.get("tool_name") != "AskUserQuestion":
            self._reply_permission(
                native_id,
                {
                    "behavior": "deny",
                    "message": "This interaction is unavailable. Ask the user in chat instead.",
                },
            )
            return
        questions = _question_choices(
            original.get("questions") if isinstance(original, dict) else None,
            dialect="claude",
        )
        if not questions:
            self._reply_permission(
                native_id,
                {
                    "behavior": "deny",
                    "message": "The question request is invalid. Ask the user in chat instead.",
                },
            )
            return
        assert isinstance(original, dict)
        original_input: dict[str, Any] = copy.deepcopy(original)

        def respond(answers: dict[str, list[str]], reject: bool) -> None:
            response: dict[str, Any] = {
                "behavior": "deny",
                "message": "The user skipped this question.",
            }
            if not reject:
                response = {
                    "behavior": "allow",
                    "updatedInput": {
                        **original_input,
                        "answers": {
                            question["question"]: ", ".join(answers[question["id"]])
                            for question in questions
                        },
                    },
                }
            self._reply_permission(native_id, response)

        self._remember_question(native_id, questions, respond)

    def _reply_permission(self, native_id: str, response: dict[str, Any]) -> None:
        self._session._write_line(
            json.dumps(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": native_id,
                        "response": response,
                    },
                }
            )
        )

    def _track_tool_calls(self, record: dict[str, Any]) -> None:
        """Keep the set of this turn's tool calls that are still running.

        Every block counts, not only the first one the normalizer reports: the
        model may call several tools at once, and a subagent's calls run while
        the call that started it is still open.
        """
        kind = record.get("type")
        if kind == "result":
            with self._input_lock:
                self._open_tool_uses.clear()
                self._input_closed = True
                # Held messages were never written: the turn answered without
                # another tool call, so they wait and go as the next turn.
                self._held.clear()
            return
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if kind not in {"assistant", "user"} or not isinstance(content, list):
            return
        release: list[PendingInput] = []
        with self._input_lock:
            for block in content:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "tool_use" and block.get("id"):
                    self._open_tool_uses.add(str(block["id"]))
                elif kind == "user" and block.get("type") == "tool_result":
                    self._open_tool_uses.discard(str(block.get("tool_use_id") or ""))
            if self._open_tool_uses and self._held:
                # The model just called a tool: the CLI reads what is written
                # now while it runs, and puts it before the model's next step.
                release, self._held = self._held, []
                self._written.extend(release)
        for pending in release:
            self._write_input(pending)

    def _write_input(self, pending: PendingInput) -> bool:
        """Write one held message; one the CLI cannot take goes as the next turn."""
        try:
            self._session.send_to_turn(pending.text)
        except (OSError, DriverError):
            with self._input_lock:
                if pending in self._written:
                    self._written.remove(pending)
            return False
        return True

    def inject(self, text: str, *, message_id: str) -> InputOutcome:
        """Take the owner's message for the model's next step before a tool call.

        Written while a tool call is running, Claude reads it after that tool
        and puts it in the same turn's next model request, where the model sees
        it as a message sent while it was working (verified against the pinned
        2.1.220). So it is written now if a tool is running, and otherwise held
        until the model calls one. Written at any other moment the CLI would
        answer it as a turn of its own after this one's result, which the
        platform never created: a turn that answers without another tool call
        never writes it, and it goes as the next turn.
        """
        pending = PendingInput(message_id, text)
        with self._input_lock:
            if self._cancelled or self._input_closed or self._result is not None:
                return InputOutcome(False, INPUT_TURN_ENDED)
            if any(held.message_id == message_id for held in (*self._held, *self._written)):
                return InputOutcome(True)
            if not self._open_tool_uses:
                self._held.append(pending)
                return InputOutcome(True)
            self._written.append(pending)
        if not self._write_input(pending):
            return InputOutcome(False, INPUT_PROCESS_GONE)
        return InputOutcome(True)


class ClaudeCodeLiveSession(HarnessSession[ClaudeCodeRuntime]):
    """A stdin-fed multi-turn session: the process survives between turns.

    Measured against the pinned CLI (2.1.220, headless stream-json; see
    ``tests/external/test_credential_rotation.py``): the ``apiKeyHelper``
    result is fetched exactly once per process — ``CLAUDE_CODE_API_KEY_HELPER_
    TTL_MS`` of 0 and 1 change nothing, and a 401 on a revoked key produces an
    error result without a refetch or retry. A live process therefore runs its
    whole life on its launch credential — which is why it must be launched on
    the *residency's* credential, the thread-attributed grant of the retained-
    session design's §6 that host beats renew and release/takeover revokes,
    never on one turn's
    per-attempt token. With attribution per thread, one long-lived key for
    the whole conversation is the ledger telling the truth, not a lie.
    """

    def __init__(
        self,
        driver: HarnessDriver[ClaudeCodeRuntime],
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__(driver, context)
        self._resume_session_id = resume_session_id
        if context.spawn_live is None:
            raise DriverError("a live session needs a live-input spawn")
        if context.state_dir is None:
            raise DriverError("a resident session needs a state mount")
        # Hook installation is an observability upgrade and must never cost a
        # user their turn. The retained broker writes into the container's
        # Docker-owned state; tests and non-observing embeddings omit the seam.
        if context.install_tool_hooks is not None:
            try:
                context.install_tool_hooks(hook_settings(CLAUDE_TOOL_MARKS))
            except Exception as exc:  # pragma: no cover - defensive degradation
                print(f"tool hooks not installed: {exc}", file=sys.stderr)
        argv, extra_env = self.runtime.prepare_live(
            workdir=context.workdir,
            model="",  # pinned per session below, chosen by the first turn's model
            gateway_base_url=context.gateway_base_url,
            credential=context.credential,
            provides_isolation=context.provides_isolation,
            mcp_config=context.mcp_config,
            config_dir=CLAUDE_STATE_CONFIG_DIR,
            resume_session_id=self._resume_session_id,
        )
        self._argv_template = argv
        self._extra_env = extra_env
        self._process: SandboxProcess | None = None
        self._lines: Iterator[str] | None = None
        # Reading with a deadline takes a thread that reads stdout into this
        # queue from then on; ``None`` in it is the end of the stream. Started
        # only when a turn needs one, which is rare.
        self._stdout: queue.Queue[str | None] | None = None
        self._pushed_back: deque[str] = deque()
        self._model: str | None = None
        #: Pinned by the first turn alongside the model, and compared against
        #: every later one. ``None`` is a real value here (Auto), so this is
        #: only meaningful once ``_process`` exists.
        self._reasoning_effort: str | None = None
        self._live_turn: ClaudeCodeLiveTurn | None = None
        # The turn's prompt, an interrupt and a mid-turn message can come from
        # different threads; stdin takes them one whole line at a time.
        self._write_lock = threading.Lock()

    def _write_line(self, line: str) -> None:
        assert self._process is not None
        with self._write_lock:
            self._process.write_line(line)

    def send_to_turn(self, text: str) -> None:
        """Write one more user message to the running process (see ``inject``)."""
        if self._process is None or not self.process_alive():
            raise DriverError("Claude resident process is not alive")
        self._write_line(self.runtime.user_message_line(text))

    @staticmethod
    def _pump(lines: Iterator[str], into: queue.Queue[str | None]) -> None:
        try:
            for line in lines:
                into.put(line)
        finally:
            into.put(None)

    def next_line(self, timeout: float | None) -> str | None:
        """The next stdout line, ``None`` at its end; ``queue.Empty`` on timeout."""
        if self._pushed_back:
            return self._pushed_back.popleft()
        assert self._lines is not None
        if self._stdout is None:
            if timeout is None:
                return next(self._lines, None)
            self._stdout = queue.Queue()
            threading.Thread(
                target=self._pump, args=(self._lines, self._stdout), daemon=True
            ).start()
        line = self._stdout.get(timeout=timeout)
        if line is None:
            # Stay at the end for whoever reads next.
            self._stdout.put(None)
        return line

    def shared_lines(self) -> Iterator[str]:
        """The session's single stdout stream, borrowed by the current turn."""
        while True:
            line = self.next_line(None)
            if line is None:
                return
            yield line

    def discard_unanswered_turn(self) -> None:
        """Stop the turn Claude opens by itself for messages it read too late.

        Claude answers a message still unread at its result as a new turn of
        its own, starting at once with a ``system``/``init`` line (verified
        against the pinned 2.1.220). That turn belongs to nobody: it is
        interrupted as soon as it starts and read to its end, so the platform's
        next turn begins on a clean stream. The message it carried goes as that
        next turn, so the native history holds it twice, once unanswered.
        """
        try:
            line = self.next_line(CLAUDE_UNANSWERED_START_S)
        except queue.Empty:
            return
        deadline = time.monotonic() + CLAUDE_UNANSWERED_END_S
        interrupted = acknowledged = False
        while line is not None:
            record = _json_object(line) or {}
            kind = record.get("type")
            if not interrupted and kind == "system" and record.get("subtype") == "init":
                with contextlib.suppress(DriverError, OSError):
                    self.interrupt_turn()
                interrupted = True
            elif kind == "control_response":
                acknowledged = True
            elif kind == "result":
                break
            try:
                line = self.next_line(max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return
        if interrupted and not acknowledged:
            # The interrupt's acknowledgement may follow the result.
            try:
                line = self.next_line(1.0)
            except queue.Empty:
                return
            if line is not None and (_json_object(line) or {}).get("type") != "control_response":
                self._pushed_back.append(line)

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn[ClaudeCodeRuntime]:
        """Feed one user turn to the resident process, spawning it lazily.

        The first turn fixes the session's model — a resident process reads
        its model once (``model_switch: restart``). A later turn naming a
        different model refuses here and the platform restarts the session.
        The reasoning effort is fixed the same way and for the same reason:
        it is a launch flag, read once at spawn, so a turn that wants a
        different depth needs a different process.
        """
        if self._closed:
            raise DriverError("session is closed")
        if self._live_turn is not None and self._live_turn._result is None:
            raise DriverError("a turn is already running on this session")
        if self.retire_after_turn:
            raise DriverError("API retry requires a native session restart before the next turn")
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
            self._write_line(self.runtime.user_message_line(turn_input.prompt))
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
            # The launch template was built with model=""; bind delegated
            # requests to the actual model chosen for this residency too.
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = model
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
        """Flush retry-affected native history before another user arrives."""
        turn = self._live_turn
        if turn is not None and turn._saw_result and turn._saw_api_retry:
            # Claude 2.1.220 can append delayed api_error records after its
            # successful answer, then parent the next user to that error.
            # Resume follows that branch and loses the successful answer.
            # EOF at the result boundary prevents the stale parent update.
            # Request EOF after broker settlement has recorded the native id.
            # The supervisor then restores that id on the next turn; ordinary
            # turns keep their resident process.
            self.retire_after_turn = True

    def interrupt_turn(self) -> None:
        """Interrupt only the active query over the pinned control protocol."""
        if self._process is None or not self.process_alive():
            raise DriverError("Claude resident process is not alive")
        self._write_line(
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
        if self._closed:
            return
        self._closed = True
        if self._process is not None:
            self._process.close_stdin()
            # A result event can precede Claude's asynchronous JSONL flush.
            # Killing immediately after EOF loses the last assistant message;
            # native resume then substitutes "No response requested.". Allow
            # a settled process to flush and exit, with a bounded kill backstop.
            if self._live_turn is not None and self._live_turn._result is not None:
                deadline = time.monotonic() + 2.0
                while self._process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait()


_DRIVER_TYPES: dict[str, type[HarnessDriver[DialectRuntime]]] = {
    "claude-code": ClaudeCodeDriver,
    "codex": CodexDriver,
    "pi": PiDriver,
    "opencode": OpenCodeDriver,
    "kilo": KiloDriver,
}


def get_driver(runtime_id: str) -> HarnessDriver[DialectRuntime]:
    """Resolve a registry id to its driver. Raises ``KeyError`` like get_runtime."""
    return driver_for(get_runtime(runtime_id))


def driver_for(runtime: AgentRuntime) -> HarnessDriver[DialectRuntime]:
    """The driver for an already-resolved dialect adapter.

    Only the five live-session drivers are executable. A dialect adapter with
    no registered live transport is rejected instead of silently falling back
    to the removed one-shot harness.
    """
    driver_type = _DRIVER_TYPES.get(getattr(runtime, "name", ""))
    if driver_type is None:
        raise KeyError(getattr(runtime, "name", ""))
    return driver_type(runtime)
