"""Claude Code's live session: stream-json turns on one resident process."""

from __future__ import annotations

import contextlib
import copy
import json
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from cloud_agent_host.bounded_output import OutputPump
from cloud_agent_host.drivers import (
    TURN_STARTED,
    DriverError,
    HarnessSession,
    HarnessTurn,
    InputOutcome,
    LaunchContext,
    LiveDriver,
    PendingInput,
    TurnInput,
    TurnResult,
    question_choices,
    user_message_event,
)
from cloud_agent_host.harness_event import LINE, HarnessEvent
from cloud_agent_host.runtimes import (
    ERROR,
    ClaudeCodeRuntime,
)
from cloud_agent_host.runtimes.claude.background import (
    BackgroundWork,
    SessionTasks,
    is_task_notification,
)
from cloud_agent_host.sandbox import STATE_MOUNT_TARGET
from cloud_agent_host.tool_windows import TOOL_MARK_FILE, hook_settings
from cloud_agent_shared.shared_protocol import (
    INPUT_PROCESS_GONE,
    INPUT_TURN_ENDED,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.sandbox import SandboxProcess


#: Claude answers a message it had not read by its result as a new turn of its
#: own, at once (the pinned 2.1.220). How long to wait for that turn to start,
#: and then for it to end once interrupted, before giving up on it.
CLAUDE_UNANSWERED_START_S = 5.0
CLAUDE_UNANSWERED_END_S = 60.0


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


#: Where the claude CLI keeps its config inside the sandbox: under the state
#: mount, so sessions survive the container (paired with the constant
#: ``/workspace`` workdir, which keeps the project-dir encoding stable).
CLAUDE_STATE_CONFIG_DIR = f"{STATE_MOUNT_TARGET}/claude"

#: The sandbox-side hooks append exact tool boundaries here. Keeping this on
#: the state mount makes it platform data rather than part of the repository,
#: and lets resident turns share one append-only stream safely.
CLAUDE_TOOL_MARKS = f"{STATE_MOUNT_TARGET}/{TOOL_MARK_FILE}"


class ClaudeCodeLiveTurn(HarnessTurn[ClaudeCodeRuntime]):
    """One turn on a resident process: borrows the shared stdout, ends as ``claude -p`` would."""

    def __init__(
        self, session: ClaudeCodeLiveSession, process: SandboxProcess, *, prompt: str = ""
    ) -> None:
        super().__init__(session, process)
        self._session: ClaudeCodeLiveSession = session
        self._prompt = prompt
        self._saw_result = False
        self._error_result = False
        self._saw_api_retry = False
        # Ending the turn the way the one-shot CLI ends its run: it holds its
        # result while a background agent runs, and until every finished
        # background task is answered (see ``claude_background``).
        self._prompt_read = False
        self._answered = False
        self._background = BackgroundWork(session._claude_tasks)
        # Tool calls the model made that have no result yet. Claude folds a
        # message written while one is running into this turn's next model
        # request; written at any other moment it opens a turn of its own.
        self._open_tool_uses: set[str] = set()
        # Messages the owner sent into this turn: held until the model next
        # calls a tool, then written, then taken once the CLI echoes them.
        self._held: list[PendingInput] = []
        self._written: list[PendingInput] = []
        # An accepted sandbox manager response can be lost after the CLI takes the
        # message and removes it from ``_written``. Keep its ID for this turn
        # so a later durable replay returns the same receipt without writing it
        # to the native process a second time.
        self._input_receipts: set[str] = set()
        # Set when the stream reaches the result: nothing is taken after it.
        self._input_closed = False
        self._input_lock = threading.Lock()

    def event_for_line(self, line: str) -> HarnessEvent:
        """Preserve terminal failure independently of the resident process exit."""
        # Inspect the wire record: the runtime deduplicates unknown system
        # subtypes, so normalized events can hide a later retry notification.
        record = self._background.read(line)[0]
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

    def _settles(self, line: str) -> bool:
        """Whether this line ends the turn, as the one-shot CLI's run would end.

        Not while a background agent is still running, and not before Claude
        has answered a finished background task. One whose notification it
        read inside this turn is answered by this turn's result; any other gets
        a notification turn after it, and read by the next turn that would end
        that one with this one's answer. A notification turn read before this
        turn's prompt was answered, left by a task that finished between
        turns, does not end it either: a notification read is never the prompt.
        """
        record, text, read = self._background.read(line)
        if record is None:
            return False
        kind = record.get("type")
        if read is not None:
            self._background.observe_read(read)
        elif text is not None and self._prompt:
            self._prompt_read = self._prompt_read or self._prompt in text
        if kind == "system" and not record.get("parent_tool_use_id"):
            self._background.observe_system(record)
        if kind == "control_response" and self._cancelled and self._answered:
            # An interrupt stops the background agents and opens no turn.
            if self._background.running_agents:
                self._session.retire_after_turn = True
            return True
        if kind != "result":
            return False
        notification = is_task_notification(record)
        self._answered = self._answered or not notification or self._prompt_read
        if not self._answered or self._background.running_agents:
            return False
        return self._cancelled or not self._background.unanswered

    def raw_lines(self) -> Iterator[str]:
        """This turn's slice of the shared stream, up to the line that settles it."""
        for line in self._session.shared_lines():
            settles = self._settles(line)
            if settles:
                self._clear_questions()
            yield line
            if settles:
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
        That echo is where the turn took the message. A task notification's
        echo is Claude reading the notification, never an owner's message.
        """
        _, echoed, notice = self._background.read(line)
        if echoed is not None and notice is None:
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
        """Hold native questions and plan approval until the turn's owner answers."""
        kind = record.get("type")
        native_id = record.get("request_id")
        if kind == "control_cancel_request" and isinstance(native_id, str):
            self.forget_question(native_id)
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
        tool_name = request.get("tool_name")
        if tool_name == "ExitPlanMode":
            self._track_plan_approval(native_id, original)
            return
        if tool_name != "AskUserQuestion":
            self._reply_permission(
                native_id,
                {
                    "behavior": "deny",
                    "message": "This interaction is unavailable. Ask the user in chat instead.",
                },
            )
            return
        questions = question_choices(
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

        self.remember_question(native_id, questions, respond)

    def _track_plan_approval(self, native_id: str, original: Any) -> None:
        """Approve the native mode transition through the existing question channel."""
        if not isinstance(original, dict) or (
            "plan" in original and not isinstance(original["plan"], str)
        ):
            self._reply_permission(
                native_id,
                {
                    "behavior": "deny",
                    "message": "The plan approval request is invalid. Ask the user in chat instead.",
                },
            )
            return
        original_input = copy.deepcopy(original)
        text = "Approve this plan and continue implementation?"
        plan = original_input.get("plan")
        if isinstance(plan, str) and plan.strip():
            text += "\n\n" + plan
        if len(text) > 10_000:
            suffix = "\n\n[Plan preview truncated.]"
            text = text[: 10_000 - len(suffix)] + suffix
        questions = [
            {
                "id": "0",
                "header": "Plan approval",
                "question": text,
                "multiple": False,
                "options": [
                    {"label": "Approve plan", "description": "Continue with implementation."}
                ],
                "allow_custom": True,
                "kind": "plan_approval",
            }
        ]

        def respond(answers: dict[str, list[str]], reject: bool) -> None:
            feedback = ", ".join(answers.get("0", [])).strip()
            approved = not reject and feedback == "Approve plan"
            if approved:
                response: dict[str, Any] = {"behavior": "allow", "updatedInput": original_input}
            else:
                declined = (
                    "The user declined this plan. Keep planning."
                    if not feedback
                    else f"The user declined this plan: {feedback}"
                )
                response = {"behavior": "deny", "message": declined}
            self._reply_permission(native_id, response)

        self.remember_question(native_id, questions, respond)

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
                if self._answered:
                    self._input_closed = True
                    # Held messages were never written: the turn answered
                    # without another tool call, so they go as the next turn.
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
                self._input_receipts.discard(pending.message_id)
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
            if message_id in self._input_receipts:
                return InputOutcome(True)
            self._input_receipts.add(message_id)
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
        driver: LiveDriver[ClaudeCodeRuntime],
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
        # user their turn. The retained sandbox manager writes into the container's
        # Docker-owned state; tests and non-observing embeddings omit the seam.
        if context.install_tool_hooks is not None:
            try:
                context.install_tool_hooks(hook_settings(CLAUDE_TOOL_MARKS))
            except Exception as exc:  # pragma: no cover - defensive degradation
                print(f"tool hooks not installed: {exc}", file=sys.stderr)
        argv, extra_env = self.runtime.prepare_live(
            workdir=context.workdir,
            model="",  # pinned per session below, chosen by the first turn's model
            model_base_url=context.model_base_url,
            credential=context.credential,
            provides_isolation=context.provides_isolation,
            mcp_config=context.mcp_config,
            config_dir=context.claude_config_dir or CLAUDE_STATE_CONFIG_DIR,
            resume_session_id=self._resume_session_id,
        )
        self._argv_template = argv
        self._extra_env = extra_env
        self._process: SandboxProcess | None = None
        self._lines: Iterator[str] | None = None
        # Reading with a deadline takes a thread that reads stdout into this
        # queue from then on; ``None`` in it is the end of the stream. Started
        # only when a turn needs one, which is rare.
        self._stdout: OutputPump[str] | None = None
        self._stdout_ended = False
        self._pushed_back: deque[str] = deque()
        self._model: str | None = None
        #: Pinned by the first turn alongside the model, and compared against
        #: every later one. ``None`` is a real value here (Auto), so this is
        #: only meaningful once ``_process`` exists.
        self._reasoning_effort: str | None = None
        self._live_turn: ClaudeCodeLiveTurn | None = None
        # Claude's background tasks across turns: see ``claude_background``.
        self._claude_tasks = SessionTasks()
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

    def next_line(self, timeout: float | None) -> str | None:
        """The next stdout line, ``None`` at its end; ``queue.Empty`` on timeout."""
        if self._pushed_back:
            return self._pushed_back.popleft()
        assert self._lines is not None
        if self._stdout_ended:
            return None
        if self._stdout is None:
            if timeout is None:
                return next(self._lines, None)
            self._stdout = OutputPump(self._lines, name="claude-resident-output")
        line = self._stdout.get(timeout=timeout)
        if line is None:
            self._stdout_ended = True
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
        self._live_turn = ClaudeCodeLiveTurn(self, self._process, prompt=turn_input.prompt)
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
            # Request EOF after sandbox manager settlement has recorded the native id.
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
        if self._stdout is not None:
            # A graceful EOF can still produce native JSONL while it flushes
            # history. Discard that output under the same memory limits so
            # a full pipe does not turn the grace period into a forced kill.
            self._stdout.close(drain=True)
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
