"""A resident Claude turn ends when Claude has answered everything it owes.

Claude Code tells the driver a background task finished with a ``system``
``task_notification``. It then answers that notification either inside the
turn it is running, by reading it into the model's next request between tool
calls, or in a turn of its own that it opens after its result with a
``system``/``init``. With ``--replay-user-messages`` the read is visible on
stdout: the notification is echoed as a ``user`` replay whose text is the
``<task-notification><task-id>...`` message (Claude Code 2.1.220, the pin) and
which also carries ``origin: {"kind": "task-notification"}`` (2.1.300).

The process here stays alive and silent after its last line, as a resident
CLI does between turns: a turn that waits for a notification turn Claude will
never open hangs exactly as it did on a host, instead of ending at the end of
a scripted feed.
"""

from __future__ import annotations

import json
import queue
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from tests.unit.test_agent_runner import _run_shared_boundary_case
from tests.unit.test_agent_sandbox_manager import _app, _SharedSupervisor

from cloud_agent_host import shared_live_attempt, turn_stream
from cloud_agent_host.drivers import (
    TURN_COMPLETED,
    USER_MESSAGE_EVENT,
    InputOutcome,
    LaunchContext,
    TurnInput,
    TurnResult,
)
from cloud_agent_host.observe.guest_observer import GuestObservation
from cloud_agent_host.resident_transport import ResidentTurn
from cloud_agent_host.runtimes import EMPTY_RUNTIME_MCP_CONFIG, ClaudeCodeRuntime
from cloud_agent_host.runtimes.claude import background as claude_background
from cloud_agent_host.runtimes.claude.background import notified_tasks
from cloud_agent_host.runtimes.claude.session import ClaudeCodeLiveSession
from cloud_agent_host.runtimes.live import driver_for
from cloud_agent_host.sandbox_manager_client import SandboxManagerClient
from cloud_agent_host.supervisor.member_sessions import SharedTurnTelemetry

if TYPE_CHECKING:
    from collections.abc import Callable

#: Long enough for a loaded CI worker, far shorter than "never".
_SETTLE_S = 10.0
#: How long a turn that must keep waiting is watched not to end.
_STILL_WAITING_S = 0.3

#: Captured before any test replaces it, so the end-to-end case can put the
#: real relay back under the runner harness that stubs it.
_REAL_STREAM_SHARED_TURN = turn_stream._stream_shared_agent_turn


class _ResidentProcess:
    """A resident CLI: stdout stays open and silent after its last line."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._exit: int | None = None

    def emit(self, *records: dict[str, Any]) -> None:
        for record in records:
            self._stdout.put(json.dumps(record))

    def write_line(self, text: str) -> None:
        self.sent.append(text)

    def lines(self):
        while (line := self._stdout.get()) is not None:
            yield line

    def close_stdin(self) -> None:
        self._end(0)

    def kill(self) -> None:
        self._end(-9)

    def _end(self, code: int) -> None:
        if self._exit is None:
            self._exit = code
            self._stdout.put(None)

    def poll(self) -> int | None:
        return self._exit

    def wait(self) -> int:
        return self._exit or 0

    def stderr_text(self) -> str:
        return ""


@pytest.fixture
def resident(tmp_path):
    """A live Claude session on one resident process, closed after the test."""
    process = _ResidentProcess()
    spawned: list[list[str]] = []

    def spawn_live(argv, _env):
        spawned.append(argv)
        return process

    session = driver_for(ClaudeCodeRuntime()).launch_live(
        LaunchContext(
            workdir="/w",
            model_base_url="http://gw",
            credential="agr",
            provides_isolation=True,
            mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
            state_dir=str(tmp_path),
            spawn_live=spawn_live,
        )
    )
    assert isinstance(session, ClaudeCodeLiveSession)
    yield SimpleNamespace(process=process, session=session, spawned=spawned)
    # Ends a turn the test left waiting: its stream reaches EOF.
    session.close()


class _Background:
    """``work`` on a thread of its own, so a test can fail where it would hang."""

    def __init__(self, work: Callable[[], Any]) -> None:
        self._done: list[Any] = []
        self._error: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self._thread.start()

    def _run(self, work: Callable[[], Any]) -> None:
        try:
            self._done.append(work())
        except BaseException as exc:  # re-raised on the test's thread
            self._error.append(exc)

    def still_running(self) -> bool:
        self._thread.join(_STILL_WAITING_S)
        return self._thread.is_alive()

    def result(self, what: str = "the turn did not settle") -> Any:
        self._thread.join(_SETTLE_S)
        if self._thread.is_alive():
            pytest.fail(f"{what} within {_SETTLE_S:.0f}s although Claude is idle after its result")
        if self._error:
            raise self._error[0]
        return self._done[0]


def _settled(turn) -> TurnResult:
    """``turn.wait()``, failing instead of hanging when the turn never ends."""
    return _Background(turn.wait).result()


# The wire records, shaped as Claude Code writes them in headless stream-json.
_INIT = {"type": "system", "subtype": "init"}
_RESULT = {"type": "result", "subtype": "success"}
_NOTIFIED = {"type": "result", "subtype": "success", "origin": {"kind": "task-notification"}}
_INTERRUPTED = {"type": "control_response", "response": {"subtype": "success"}}


def _say(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _call(tool_use_id: str, command: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Bash",
                    "input": {"command": command},
                }
            ]
        },
    }


def _tool_result(tool_use_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}],
        },
    }


def _running(*tasks: tuple[str, str]) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "background_tasks_changed",
        "tasks": [{"task_id": task_id, "task_type": kind} for task_id, kind in tasks],
    }


def _backgrounded(task_id: str) -> dict[str, Any]:
    """A foreground command moved to the background after its tool timeout."""
    return {
        "type": "system",
        "subtype": "task_updated",
        "task_id": task_id,
        "patch": {"is_backgrounded": True},
    }


def _finished(task_id: str, status: str = "completed") -> dict[str, Any]:
    return {"type": "system", "subtype": "task_notification", "task_id": task_id, "status": status}


def _notice(task_id: str, *, origin: bool = True, summary: str = "") -> dict[str, Any]:
    """Claude reading a finished task's notification into the conversation."""
    summary = summary or f'Background command "npm test" ({task_id}) completed (exit code 0)'
    record: dict[str, Any] = {
        "type": "user",
        "isReplay": True,
        "parent_tool_use_id": None,
        "message": {
            "role": "user",
            "content": (
                f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                "<tool-use-id>toolu_tests</tool-use-id>\n"
                f"<output-file>/tmp/claude/tasks/{task_id}.output</output-file>\n"
                f"<status>completed</status>\n<summary>{summary}</summary>\n"
                "</task-notification>"
            ),
        },
    }
    if origin:
        record["origin"] = {"kind": "task-notification"}
    return record


def _read(text: str) -> dict[str, Any]:
    """The CLI echoing a stdin message at the moment it reads it."""
    return {
        "type": "user",
        "isReplay": True,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _shell_backgrounded_by_its_timeout(task_id: str = "bash-1") -> list[dict[str, Any]]:
    """``npm test`` outlives its tool timeout and keeps running in the background."""
    return [
        _call("toolu_tests", "npm test"),
        _running((task_id, "local_bash")),
        _backgrounded(task_id),
        _tool_result(
            "toolu_tests", f"Command is still running in the background with ID: {task_id}"
        ),
    ]


def _absorbed(task_id: str, *, origin: bool, read_first: bool) -> list[dict[str, Any]]:
    """The task ends during a tool call; Claude reads its notice before its next step."""
    finished = [_finished(task_id), _running()]
    notice = [_notice(task_id, origin=origin)]
    return [
        _call("toolu_poll", f"tail -n 20 /tmp/claude/tasks/{task_id}.output"),
        *(notice if read_first else finished),
        _tool_result("toolu_poll", "Tests: 214 passed"),
        *(finished if read_first else notice),
    ]


def _turn(resident, prompt: str):
    return resident.session.start_turn(TurnInput(prompt=prompt, model="m"))


def _prompts(process: _ResidentProcess) -> list[str]:
    return [
        json.loads(line)["message"]["content"][0]["text"]
        for line in process.sent
        if json.loads(line).get("type") == "user"
    ]


@pytest.mark.parametrize("origin", [True, False], ids=["2.1.300-origin", "2.1.220-text-only"])
@pytest.mark.parametrize(
    "read_first", [False, True], ids=["notified-then-read", "read-then-notified"]
)
def test_a_background_shell_notice_read_inside_the_turn_settles_at_its_result(
    resident, origin, read_first
):
    """The hang on a host: Agent Finished was shown, the attempt never ended.

    ``npm test`` was backgrounded by its tool timeout, finished while Claude
    was still working, and Claude answered it in the same turn. No turn of
    its own follows, so the turn ends at its result, and the next prompt on
    the same process is answered on its own.
    """
    process = resident.process
    process.emit(
        _INIT,
        _read("run the tests"),
        *_shell_backgrounded_by_its_timeout(),
        *_absorbed("bash-1", origin=origin, read_first=read_first),
        _say("All 214 tests pass."),
        _RESULT,
    )
    turn = _turn(resident, "run the tests")
    events = _Background(lambda: list(turn.iter_events())).result(
        "the turn's event stream did not reach its boundary"
    )

    assert events[-1].event_type == TURN_COMPLETED
    assert events[-1].payload["final_output"] == "All 214 tests pass."
    result = _settled(turn)
    assert result.status == "completed"
    assert result.native_turn_settled is True
    assert result.exit_code == 0
    # Nothing was interrupted and the process keeps its residency.
    assert resident.session.retire_after_turn is False
    assert resident.session.process_alive()

    process.emit(_INIT, _read("now lint it"), _say("lint is clean"), _RESULT)
    follow_up = _settled(_turn(resident, "now lint it"))

    assert follow_up.status == "completed"
    assert follow_up.final_output == "lint is clean"
    assert len(resident.spawned) == 1
    assert _prompts(process) == ["run the tests", "now lint it"]


def test_a_background_agent_notice_read_inside_the_turn_settles_at_its_result(resident):
    """An agent is waited for until it ends; read in the turn, its answer is the turn's."""
    resident.process.emit(
        _INIT,
        _running(("agent-1", "local_agent")),
        _call("toolu_build", "npm run build"),
        _running(),
        _finished("agent-1"),
        _tool_result("toolu_build", "built"),
        _notice("agent-1", summary='Agent "review the API" completed'),
        _say("The build passes and the agent found nothing."),
        _RESULT,
    )

    result = _settled(_turn(resident, "build it and review the API"))

    assert result.status == "completed"
    assert result.final_output == "The build passes and the agent found nothing."
    assert resident.session.retire_after_turn is False


def test_a_running_background_agent_still_holds_the_result(resident):
    """Reading one task's notice does not end the wait for an agent still running."""
    process = resident.process
    process.emit(
        _INIT,
        _running(("agent-1", "local_agent"), ("bash-1", "local_bash")),
        _call("toolu_poll", "tail -n 20 /tmp/claude/tasks/bash-1.output"),
        _running(("agent-1", "local_agent")),
        _finished("bash-1"),
        _tool_result("toolu_poll", "done"),
        _notice("bash-1"),
        _say("tests pass; the agent is still reviewing"),
        _RESULT,
    )
    waiting = _Background(_turn(resident, "test it and review it").wait)
    assert waiting.still_running(), "the turn ended while its background agent was running"

    process.emit(
        _running(), _finished("agent-1"), _INIT, _say("the review found nothing"), _NOTIFIED
    )
    result = waiting.result()

    assert result.final_output == (
        "tests pass; the agent is still reviewing\n\nthe review found nothing"
    )


@pytest.mark.parametrize("one_was_read", [False, True])
def test_a_notice_left_unread_at_the_result_still_waits_for_its_own_turn(resident, one_was_read):
    """A task that ends during the model's last request is answered after the result.

    Only the notice Claude read is answered by this result; a task whose notice
    it had not read gets a turn of its own, and that answer is still this turn's.
    """
    process = resident.process
    read = (
        [*_absorbed("bash-1", origin=True, read_first=False)]
        if one_was_read
        else [_call("toolu_poll", "sleep 1"), _tool_result("toolu_poll", "")]
    )
    process.emit(
        _INIT,
        _running(("bash-1", "local_bash"), ("bash-2", "local_bash")),
        *read,
        _finished("bash-2"),
        _running(),
        _say("bash-2 is still running"),
        _RESULT,
    )
    waiting = _Background(_turn(resident, "run both").wait)
    assert waiting.still_running(), "the turn ended before Claude answered bash-2"

    process.emit(_INIT, _notice("bash-2"), _say("bash-2 finished too"), _NOTIFIED)
    result = waiting.result()

    assert result.status == "completed"
    assert result.final_output == "bash-2 is still running\n\nbash-2 finished too"

    process.emit(_INIT, _read("next"), _say("next answer"), _RESULT)
    assert _settled(_turn(resident, "next")).final_output == "next answer"


def test_cancelling_a_turn_waiting_for_a_notification_turn_still_cancels(resident):
    process = resident.process
    process.emit(
        _INIT,
        _running(("bash-1", "local_bash")),
        _finished("bash-1"),
        _running(),
        _say("started"),
        _RESULT,
    )
    turn = _turn(resident, "serve")
    for event in turn.iter_events():
        if event.payload.get("phase") == "result":
            turn.cancel()
            process.emit(_INTERRUPTED)

    result = _settled(turn)

    assert result.status == "cancelled"
    assert json.loads(process.sent[-1])["request"]["subtype"] == "interrupt"
    assert resident.session.retire_after_turn is False


def test_a_notice_read_between_turns_is_not_taken_for_the_next_prompt(resident):
    """``status`` appears in every notice; reading one is not reading the prompt.

    A shell that ended between turns opens a notification turn the next turn
    reads first. Taking its notice for the prompt ended the turn with the
    notification's answer and left the prompt's answer to the turn after it.
    """
    process = resident.process
    process.emit(_INIT, _running(("bash-1", "local_bash")), _say("server up"), _RESULT)
    assert _settled(_turn(resident, "serve")).final_output == "server up"

    process.emit(
        _running(),
        _finished("bash-1"),
        _INIT,
        _notice("bash-1"),
        _say("the server exited"),
        _NOTIFIED,
        _read("status"),
        _INIT,
        _say("it is down"),
        _RESULT,
    )
    result = _settled(_turn(resident, "status"))

    assert result.final_output == "the server exited\n\nit is down"

    process.emit(_INIT, _read("next"), _say("next answer"), _RESULT)
    assert _settled(_turn(resident, "next")).final_output == "next answer"


def test_a_notice_is_not_taken_for_an_owner_message_written_into_the_turn(resident):
    """An owner message counts as taken only when its own echo arrives."""
    process = resident.process
    turn = _turn(resident, "run the tests")
    events = turn.iter_events()
    next(events)  # turn started
    process.emit(_INIT, *_shell_backgrounded_by_its_timeout(), _call("toolu_poll", "sleep 5"))
    for _ in range(6):
        next(events)
    assert turn.inject("completed", message_id="apmsg_1") == InputOutcome(True)

    process.emit(_finished("bash-1"), _running(), _tool_result("toolu_poll", ""), _notice("bash-1"))
    taken = [next(events) for _ in range(4)]
    assert all(event.event_type != USER_MESSAGE_EVENT for event in taken)

    process.emit(_read("completed"), _say("noted; tests pass"), _RESULT)
    rest = list(events)
    assert [event.payload for event in rest if event.event_type == USER_MESSAGE_EVENT] == [
        {"text": "completed", "pending_message_id": "apmsg_1"}
    ]
    assert rest[-1].event_type == TURN_COMPLETED
    assert _settled(turn).final_output == "noted; tests pass"


def test_a_notice_read_in_one_turn_and_reported_in_the_next_is_not_owed_again(resident):
    """Read before its ``task_notification``, the read still counts a turn later.

    Should that record reach stdout only after the result, it falls in the
    next turn's slice, after that turn's ``init``; the read is the session's.
    """
    process = resident.process
    process.emit(
        _INIT,
        *_shell_backgrounded_by_its_timeout(),
        _call("toolu_poll", "sleep 5"),
        _notice("bash-1"),
        _tool_result("toolu_poll", ""),
        _say("All 214 tests pass."),
        _RESULT,
    )
    assert _settled(_turn(resident, "run the tests")).final_output == "All 214 tests pass."

    process.emit(
        _INIT, _read("now lint it"), _finished("bash-1"), _running(), _say("lint is clean"), _RESULT
    )
    follow_up = _settled(_turn(resident, "now lint it"))

    assert follow_up.status == "completed"
    assert follow_up.final_output == "lint is clean"


def test_an_owner_message_quoting_a_notice_is_still_the_owners(resident):
    """A notice pasted from a log names no task of this session: it is a message."""
    process = resident.process
    pasted = "what does this mean?\n" + _notice("bash-9")["message"]["content"]
    turn = _turn(resident, "run the tests")
    events = turn.iter_events()
    next(events)  # turn started
    process.emit(_INIT, *_shell_backgrounded_by_its_timeout(), _call("toolu_poll", "sleep 5"))
    for _ in range(6):
        next(events)
    assert turn.inject(pasted, message_id="apmsg_1") == InputOutcome(True)

    process.emit(_tool_result("toolu_poll", ""), _read(pasted), _say("an old run"), _RESULT)
    rest = list(events)

    assert [event.payload for event in rest if event.event_type == USER_MESSAGE_EVENT] == [
        {"text": pasted, "pending_message_id": "apmsg_1"}
    ]
    assert rest[-1].event_type == TURN_COMPLETED
    assert _settled(turn).final_output == "an old run"
    assert resident.session.retire_after_turn is False


def test_a_subagents_read_does_not_answer_the_main_conversations_notice(resident):
    """A read echoed from a subagent's sidechain is not Claude reading the notice."""
    process = resident.process
    sidechain = {**_notice("bash-1"), "parent_tool_use_id": "toolu_agent"}
    process.emit(
        _INIT,
        _running(("bash-1", "local_bash")),
        _call("toolu_poll", "sleep 1"),
        _finished("bash-1"),
        _running(),
        sidechain,
        _tool_result("toolu_poll", ""),
        _say("still working"),
        _RESULT,
    )
    waiting = _Background(_turn(resident, "serve").wait)
    assert waiting.still_running(), "a subagent's read ended the main turn's wait"

    process.emit(_INIT, _notice("bash-1"), _say("the server exited"), _NOTIFIED)

    assert waiting.result().final_output == "still working\n\nthe server exited"


def test_each_line_is_parsed_and_matched_once(resident, monkeypatch):
    """Settling on a line and turning it into events read it once between them."""
    parsed: list[str] = []
    matched: list[str] = []
    real_loads, real_notified = claude_background.json.loads, claude_background.notified_tasks

    def loads(line):
        parsed.append(line)
        return real_loads(line)

    def notified(record, text, known):
        matched.append(text)
        return real_notified(record, text, known)

    monkeypatch.setattr(claude_background, "json", SimpleNamespace(loads=loads))
    monkeypatch.setattr(claude_background, "notified_tasks", notified)
    feed = [
        _INIT,
        _read("run the tests"),
        *_shell_backgrounded_by_its_timeout(),
        *_absorbed("bash-1", origin=True, read_first=False),
        _say("All 214 tests pass."),
        _RESULT,
    ]
    resident.process.emit(*feed)
    turn = _turn(resident, "run the tests")

    events = _Background(lambda: list(turn.iter_events())).result()

    assert events[-1].event_type == TURN_COMPLETED
    assert len(parsed) == len(feed)
    assert len(matched) == 2  # the prompt's echo and the notice's


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_notice("bash-1"), frozenset({"bash-1"})),
        (_notice("bash-1", origin=False), frozenset({"bash-1"})),
        # Text placed ahead of the tag does not hide it.
        (
            _read("<system-reminder>x</system-reminder>" + _notice("b-2")["message"]["content"]),
            {"b-2"},
        ),
        # Marked by Claude, in a shape naming no task: read, matched to none.
        ({**_read("a scheduled trigger fired"), "origin": {"kind": "task-notification"}}, set()),
        (_read("did <task-notification> run?"), None),
        (_read("run the tests"), None),
        # Unmarked, a quoted notice naming no task of this session is the owner's.
        (_read("why did this fail? " + _notice("bash-9")["message"]["content"]), None),
    ],
)
def test_which_replayed_messages_are_task_notifications(record, expected):
    text = record["message"]["content"]
    text = text if isinstance(text, str) else text[0]["text"]

    assert notified_tasks(record, text, {"bash-1", "b-2"}) == expected


@pytest.mark.parametrize(
    "read_first", [False, True], ids=["notified-then-read", "read-then-notified"]
)
def test_a_notice_naming_no_task_still_lets_the_turn_end(resident, read_first):
    """Unable to match it, the driver still ends the turn rather than wait for ever."""
    finished = [_finished("bash-1"), _running()]
    notice = [
        {**_read("[a notification in a newer shape]"), "origin": {"kind": "task-notification"}}
    ]
    resident.process.emit(
        _INIT,
        *_shell_backgrounded_by_its_timeout(),
        _call("toolu_poll", "sleep 5"),
        *(notice if read_first else finished),
        _tool_result("toolu_poll", ""),
        *(finished if read_first else notice),
        _say("the tests passed"),
        _RESULT,
    )

    assert _settled(_turn(resident, "run the tests")).final_output == "the tests passed"


class _ResidentSupervisor(_SharedSupervisor):
    """The sandbox manager's supervisor, reduced to one member's resident session."""

    def __init__(self, session: ClaudeCodeLiveSession) -> None:
        super().__init__()
        self.threads.add("athr_1")
        self.session = session
        self.turn: ResidentTurn | None = None
        self.completed: list[ResidentTurn] = []
        self.aborted: list[ResidentTurn] = []

    def start_turn(self, _thread_id, _token, *, prompt, model, reasoning_effort):
        live = self.session.start_turn(
            TurnInput(prompt=prompt, model=model, reasoning_effort=reasoning_effort)
        )
        self.turn = ResidentTurn(live)
        return self.turn

    def complete_turn(self, _thread_id, _token, turn):
        self.completed.append(turn)
        return SharedTurnTelemetry(observation=GuestObservation.unavailable("not a Kata guest"))

    def abort_turn(self, _thread_id, _token, turn):
        self.aborted.append(turn)

    def interrupt(self, _thread_id, _token):
        assert self.turn is not None
        self.turn.interrupt()


def test_a_completed_resident_turn_finishes_the_platform_attempt(resident, monkeypatch):
    """From Claude's final result to the attempt's terminal ``succeeded``.

    The real driver, the sandbox manager's turn endpoint, the runner's
    supervisor client and its shared-turn relay and settlement. Before the
    fix the sandbox manager never wrote its ``result`` frame and the runner
    kept renewing the lease of a turn Claude had finished.
    """
    from starlette.testclient import TestClient

    process = resident.process
    process.emit(
        _INIT,
        _read("do it"),
        *_shell_backgrounded_by_its_timeout(),
        *_absorbed("bash-1", origin=True, read_first=False),
        _say("All 214 tests pass."),
        _RESULT,
    )
    owner = _ResidentSupervisor(resident.session)
    client = SandboxManagerClient("http://sandbox-manager", "sandbox-manager-secret")
    client._client.close()
    client._client = TestClient(
        _app(owner),
        base_url="http://sandbox-manager",
        headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
    )
    relayed: list[tuple] = []

    def wire(control, supervisor, _finish):
        def relay(**kwargs):
            outcome = _REAL_STREAM_SHARED_TURN(**kwargs)
            relayed.append(outcome)
            return outcome

        monkeypatch.setattr(shared_live_attempt, "_stream_shared_agent_turn", relay)
        supervisor.start_shared_turn.side_effect = client.start_shared_turn
        supervisor.interrupt_shared.side_effect = client.interrupt_shared
        control.lease_lost = False

    exit_code, _control, supervisor, finish = _Background(
        lambda: _run_shared_boundary_case(monkeypatch, 0, None, "claude-code", configure=wire)
    ).result("the platform attempt did not finish")

    ((code, _tail, tool_errors, frame, reported),) = relayed
    assert (code, tool_errors, reported) == (0, [], None)
    assert frame["kind"] == "result"
    assert frame["status"] == "completed"
    assert frame["native_turn_settled"] is True
    assert frame["final_output"] == "All 214 tests pass."
    assert owner.completed == [owner.turn] and owner.aborted == []
    assert exit_code == 0
    supervisor.settle_shared.assert_called_once()
    supervisor.abandon_shared.assert_not_called()
    supervisor.interrupt_shared.assert_not_called()
    finish.assert_called_once()
    assert finish.call_args.args[0] == "succeeded"
    assert resident.session.process_alive()
    assert _prompts(process) == ["do it"]
