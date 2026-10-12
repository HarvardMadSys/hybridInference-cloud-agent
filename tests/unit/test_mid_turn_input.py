"""Giving the owner's message to a running turn ("send now"), on the host side.

The drivers are driven with their real dialect adapters over a fake live
process whose stdout is fed line by line, exactly as the broker's stream thread
feeds a turn; the message arrives from another thread, as it does in the broker.
The rule under test: the model gets the message before its next tool call, and
a turn that answers without another tool call never gets it.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cloud_agent_host import drivers
from cloud_agent_host.drivers import (
    LINE,
    TURN_COMPLETED,
    ClaudeCodeDriver,
    CodexDriver,
    InputOutcome,
    KiloDriver,
    LaunchContext,
    OpenCodeDriver,
    PendingInput,
    PiDriver,
    TurnInput,
)
from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    ClaudeCodeRuntime,
    CodexRuntime,
    KiloRuntime,
    OpencodeRuntime,
    PiRuntime,
)
from cloud_agent_host.sandbox import ProcessBackend
from cloud_agent_host.shared_session_supervisor import (
    HELD_INPUT_TTL_S,
    SharedChatSessionSupervisor,
)
from cloud_agent_host.workspace_broker import create_app
from cloud_agent_shared.shared_protocol import (
    INPUT_PROCESS_GONE,
    INPUT_REFUSED,
    INPUT_TURN_ENDED,
    INPUT_UNSUPPORTED,
    USER_MESSAGE_EVENT,
)


class _LiveProcess:
    """A resident process: stdout fed by the test, stdin recorded."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.feed: list[str] = []
        self.broken = False

    def write_line(self, text: str) -> None:
        if self.broken:
            raise BrokenPipeError("stdin closed")
        self.sent.append(text)

    def close_stdin(self) -> None:
        pass

    def lines(self):
        while self.feed:
            yield self.feed.pop(0)

    def kill(self) -> None:
        pass

    def poll(self) -> int | None:
        return None

    def wait(self) -> int:
        return 0

    def stderr_text(self) -> str:
        return ""


class _StreamingProcess(_LiveProcess):
    """A process whose stdout stays open between turns, as a real one does."""

    def __init__(self) -> None:
        super().__init__()
        self._stdout: queue.Queue[str] = queue.Queue()

    def emit(self, *records: dict) -> None:
        for line in _lines(*records):
            self._stdout.put(line)

    def lines(self):
        while True:
            yield self._stdout.get()


def _context(process: _LiveProcess, tmp_path) -> LaunchContext:
    return LaunchContext(
        workdir="/w",
        gateway_base_url="http://gw",
        credential="agr",
        provides_isolation=True,
        mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
        state_dir=str(tmp_path),
        spawn_live=lambda _argv, _env: process,
    )


def _lines(*records: dict) -> list[str]:
    return [json.dumps(record) for record in records]


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks), "usage": {}}}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use(tool_id: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "sleep 5"}}


def _tool_result(tool_id: str) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]},
    }


def _echo(*texts: str) -> dict:
    """What the CLI prints when it reads user messages (``--replay-user-messages``)."""
    return {
        "type": "user",
        "isReplay": True,
        "message": {"role": "user", "content": [_text(text) for text in texts]},
    }


_INIT = {"type": "system", "subtype": "init", "session_id": "s-1"}
_RESULT = {"type": "result", "subtype": "success", "result": "done"}


def _claude_turn(tmp_path, process: _LiveProcess | None = None):
    process = process or _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_context(process, tmp_path))
    turn = session.start_turn(TurnInput(prompt="fix it", model="m"))
    events = turn.iter_events()
    next(events)  # turn started
    return process, session, turn, events


def _read(process: _LiveProcess, events, *records: dict) -> list:
    """Let the turn's stream read ``records``, as the broker's thread would."""
    process.feed.extend(_lines(*records))
    return [next(events) for _ in records]


def _written(process: _LiveProcess) -> list[str]:
    """The user messages written to the CLI after the prompt."""
    texts = []
    for line in process.sent[1:]:
        record = json.loads(line)
        if record.get("type") == "user":
            texts.append(record["message"]["content"][0]["text"])
    return texts


def test_claude_the_launch_echoes_what_it_reads():
    argv, _ = ClaudeCodeRuntime().prepare_live(
        workdir="/w", model="m", gateway_base_url="http://gw", credential="agr"
    )

    assert "--replay-user-messages" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"


def test_claude_holds_a_message_until_the_model_calls_a_tool(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)

    # Thinking or answering, nothing would carry it yet: it is held, not written.
    assert turn.inject("also check the docs", message_id="apmsg_1") == InputOutcome(True)
    assert _written(process) == []

    # The model calls a tool: now it is written, and the CLI reads it while the
    # tool runs, ahead of the model's next step.
    _read(process, events, _assistant(_tool_use("toolu_1")))
    assert _written(process) == ["also check the docs"]

    # The CLI echoes it at the point it took it: that is the event.
    (taken,) = _read(process, events, _echo("also check the docs"))
    assert taken.event_type == USER_MESSAGE_EVENT
    assert taken.payload == {"text": "also check the docs", "pending_message_id": "apmsg_1"}


def test_claude_writes_at_once_while_a_tool_is_running(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))

    assert turn.inject("also check the docs", message_id="apmsg_1") == InputOutcome(True)

    assert process.sent[-1] == ClaudeCodeRuntime().user_message_line("also check the docs")


def test_claude_counts_every_parallel_tool_call_not_only_the_first(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_a"), _tool_use("toolu_b")))
    _read(process, events, _tool_result("toolu_a"))

    # toolu_b is still running; the reported event only named toolu_a.
    assert turn.inject("and the other file", message_id="apmsg_1") == InputOutcome(True)
    assert _written(process) == ["and the other file"]


def test_a_turn_that_answers_without_another_tool_call_never_writes_it(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    turn.inject("also check the docs", message_id="apmsg_1")

    _read(process, events, _assistant(_text("all done")), _RESULT)

    # It goes as the next turn instead, and the turn takes nothing more.
    assert _written(process) == []
    assert next(events).event_type == TURN_COMPLETED
    assert turn.inject("too late", message_id="apmsg_2") == InputOutcome(False, INPUT_TURN_ENDED)
    assert _written(process) == []


def test_one_echo_can_take_several_messages(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))
    turn.inject("first", message_id="apmsg_1")
    turn.inject("second", message_id="apmsg_2")

    taken = turn.events_for_line(json.dumps(_echo("first", "second")))

    assert [(event.event_type, event.payload["pending_message_id"]) for event in taken] == [
        (USER_MESSAGE_EVENT, "apmsg_1"),
        (USER_MESSAGE_EVENT, "apmsg_2"),
    ]
    assert turn._written == []


def test_the_prompts_echo_is_not_an_event(tmp_path):
    process, _, _turn, events = _claude_turn(tmp_path)

    (echo,) = _read(process, events, _echo("fix it"))

    assert echo.event_type == LINE


def test_the_same_message_is_given_once(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))

    assert turn.inject("again", message_id="apmsg_1") == InputOutcome(True)
    assert turn.inject("again", message_id="apmsg_1") == InputOutcome(True)

    assert _written(process) == ["again"]


def test_claude_replays_a_taken_message_receipt_without_writing_it_again(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))
    assert turn.inject("again", message_id="apmsg_1") == InputOutcome(True)
    (taken,) = _read(process, events, _echo("again"))

    replayed = turn.inject("again", message_id="apmsg_1")

    assert taken.event_type == USER_MESSAGE_EVENT
    assert replayed == InputOutcome(True)
    assert _written(process) == ["again"]


def test_claude_refuses_a_message_for_a_turn_that_is_over(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))

    turn.cancel()

    assert turn.inject("never mind", message_id="apmsg_1") == InputOutcome(False, INPUT_TURN_ENDED)


def test_claude_reports_a_process_it_cannot_write_to(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))
    process.broken = True

    assert turn.inject("hello?", message_id="apmsg_1") == InputOutcome(False, INPUT_PROCESS_GONE)
    assert turn._written == []


def test_a_held_message_the_process_cannot_take_is_not_reported_taken(tmp_path):
    process, _, turn, events = _claude_turn(tmp_path)
    turn.inject("hello?", message_id="apmsg_1")
    process.broken = True

    _read(process, events, _assistant(_tool_use("toolu_1")))

    assert turn._written == []


def test_a_message_the_cli_read_too_late_is_stopped_and_the_next_turn_starts_clean(tmp_path):
    process = _StreamingProcess()
    _, session, turn, events = _claude_turn(tmp_path, process)
    process.emit(_assistant(_tool_use("toolu_1")))
    next(events)
    turn.inject("also check the docs", message_id="apmsg_1")
    # The tool ended before the CLI read the message, and the model answered:
    # no echo came, so the CLI will answer it as a turn of its own.
    process.emit(_tool_result("toolu_1"), _assistant(_text("all done")), _RESULT)
    process.emit(
        _INIT,
        _echo("also check the docs"),
        _assistant(_text("an answer nobody asked this turn for")),
        {"type": "control_response", "response": {"subtype": "success"}},
        {"type": "result", "subtype": "error_during_execution", "is_error": True},
    )

    rest = list(events)

    # This turn ends on its own answer; the extra one is interrupted and
    # discarded, and the message is not reported as taken.
    assert [event.event_type for event in rest][-1] == TURN_COMPLETED
    assert USER_MESSAGE_EVENT not in [event.event_type for event in rest]
    assert turn.wait().final_output == "all done"
    interrupts = [
        json.loads(line)
        for line in process.sent
        if json.loads(line).get("type") == "control_request"
    ]
    assert [request["request"]["subtype"] for request in interrupts] == ["interrupt"]

    # The next turn reads its own lines, not the discarded turn's.
    process.emit(_assistant(_text("second answer")), _RESULT)
    second = session.start_turn(TurnInput(prompt="and then", model="m"))
    assert second.wait().final_output == "second answer"


def test_when_no_turn_of_its_own_follows_the_next_turn_is_not_kept_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers, "CLAUDE_UNANSWERED_START_S", 0.05)
    process = _StreamingProcess()
    _, _session, turn, events = _claude_turn(tmp_path, process)
    process.emit(_assistant(_tool_use("toolu_1")))
    next(events)
    turn.inject("also check the docs", message_id="apmsg_1")
    process.emit(_tool_result("toolu_1"), _assistant(_text("all done")), _RESULT)

    started = time.monotonic()
    rest = list(events)

    assert rest[-1].event_type == TURN_COMPLETED
    assert time.monotonic() - started < 5


def _codex_turn(tmp_path):
    process = _LiveProcess()
    process.feed = _lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(_context(process, tmp_path))
    session.ensure_started(model="gpt", reasoning_effort=None)
    process.feed = _lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    turn = session.start_turn(TurnInput(prompt="run it", model="gpt"))
    return process, turn


def _steer_in_background(turn, text: str, message_id: str = "apmsg_1"):
    outcome: list[InputOutcome] = []
    worker = threading.Thread(
        target=lambda: outcome.append(turn.inject(text, message_id=message_id))
    )
    worker.start()
    return worker, outcome


def _sent_steer(process: _LiveProcess) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for line in process.sent:
            request = json.loads(line)
            if request.get("method") == "turn/steer":
                return request
        time.sleep(0.01)
    raise AssertionError("no turn/steer was written")


def test_codex_steers_the_turn_it_saw_running_and_its_stream_reports_it_taken(tmp_path):
    process, turn = _codex_turn(tmp_path)

    worker, outcome = _steer_in_background(turn, "also run the linter")
    steer = _sent_steer(process)
    assert steer["params"] == {
        "threadId": "thread-a",
        "expectedTurnId": "turn-a",
        "input": [{"type": "text", "text": "also run the linter"}],
    }
    # The answer arrives in the turn's own stdout: that is where the turn took
    # the message, and the stream says so.
    (answer,) = turn.events_for_line(
        json.dumps({"id": steer["id"], "result": {"turnId": "turn-a"}})
    )
    worker.join(5)

    assert answer.event_type == USER_MESSAGE_EVENT
    assert answer.payload == {"text": "also run the linter", "pending_message_id": "apmsg_1"}
    assert outcome == [InputOutcome(True)]


def test_codex_replays_one_receipt_for_concurrent_duplicate_message_ids(tmp_path):
    process, turn = _codex_turn(tmp_path)

    first, first_outcome = _steer_in_background(turn, "also run the linter")
    steer = _sent_steer(process)
    second, second_outcome = _steer_in_background(turn, "also run the linter")
    (answer,) = turn.events_for_line(
        json.dumps({"id": steer["id"], "result": {"turnId": "turn-a"}})
    )
    first.join(5)
    second.join(5)

    assert answer.event_type == USER_MESSAGE_EVENT
    assert first_outcome == second_outcome == [InputOutcome(True)]
    assert sum(json.loads(line).get("method") == "turn/steer" for line in process.sent) == 1


def test_codex_replays_an_uncertain_native_failure_to_concurrent_duplicates(tmp_path, monkeypatch):
    _process, turn = _codex_turn(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    waiter_started = threading.Event()
    native_calls = 0

    def lose_native_response(_turn, _pending):
        nonlocal native_calls
        native_calls += 1
        entered.set()
        assert release.wait(5)
        raise drivers.DriverError("native response lost")

    original_wait = drivers._InputReceipt.wait

    def mark_waiting(receipt):
        waiter_started.set()
        return original_wait(receipt)

    monkeypatch.setattr(turn._session, "inject_input", lose_native_response)
    monkeypatch.setattr(drivers._InputReceipt, "wait", mark_waiting)
    errors: list[BaseException] = []

    def inject() -> None:
        try:
            turn.inject("also run the linter", message_id="apmsg_1")
        except BaseException as exc:
            errors.append(exc)

    owner = threading.Thread(target=inject)
    duplicate = threading.Thread(target=inject)
    owner.start()
    assert entered.wait(5)
    duplicate.start()
    assert waiter_started.wait(5)
    release.set()
    owner.join(5)
    duplicate.join(5)

    assert not owner.is_alive()
    assert not duplicate.is_alive()
    assert native_calls == 1
    assert len(errors) == 2
    assert all(isinstance(error, drivers.DriverError) for error in errors)
    assert [str(error) for error in errors] == ["native response lost"] * 2
    assert "apmsg_1" not in turn._input_receipts


def test_codex_replays_settled_receipt_without_a_second_native_steer(tmp_path):
    process, turn = _codex_turn(tmp_path)

    first, first_outcome = _steer_in_background(turn, "also run the linter")
    steer = _sent_steer(process)
    turn.events_for_line(json.dumps({"id": steer["id"], "result": {"turnId": "turn-a"}}))
    first.join(5)

    replayed = turn.inject("also run the linter", message_id="apmsg_1")

    assert first_outcome == [InputOutcome(True)]
    assert replayed == InputOutcome(True)
    assert sum(json.loads(line).get("method") == "turn/steer" for line in process.sent) == 1


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("no active turn to steer", INPUT_TURN_ENDED),
        ("expected active turn id `turn-a` but found `turn-b`", INPUT_TURN_ENDED),
        ("activeTurnNotSteerable", INPUT_REFUSED),
    ],
)
def test_codex_refusals_say_why(tmp_path, message, reason):
    process, turn = _codex_turn(tmp_path)

    worker, outcome = _steer_in_background(turn, "also run the linter")
    steer = _sent_steer(process)
    (answer,) = turn.events_for_line(
        json.dumps({"id": steer["id"], "error": {"code": -32600, "message": message}})
    )
    worker.join(5)

    assert outcome == [InputOutcome(False, reason)]
    assert answer.event_type == LINE


def test_a_slow_steer_is_not_a_refusal_and_its_late_acceptance_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers, "STEER_REPLY_TIMEOUT_S", 0.05)
    process, turn = _codex_turn(tmp_path)

    assert turn.inject("anyone there?", message_id="apmsg_1") == InputOutcome(True)

    steer = _sent_steer(process)
    (late,) = turn.events_for_line(json.dumps({"id": steer["id"], "result": {}}))
    assert late.event_type == USER_MESSAGE_EVENT


def test_a_late_refusal_is_no_error_of_the_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers, "STEER_REPLY_TIMEOUT_S", 0.05)
    process, turn = _codex_turn(tmp_path)
    turn.inject("anyone there?", message_id="apmsg_1")

    steer = _sent_steer(process)
    (late,) = turn.events_for_line(
        json.dumps({"id": steer["id"], "error": {"code": -32600, "message": "no active turn"}})
    )

    assert late.event_type == LINE


def test_codex_refuses_a_message_for_a_turn_that_is_over(tmp_path):
    process, turn = _codex_turn(tmp_path)
    process.feed = _lines(
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}}
    )
    turn.wait()

    assert turn.inject("too late", message_id="apmsg_1") == InputOutcome(False, INPUT_TURN_ENDED)
    assert not any(json.loads(line).get("method") == "turn/steer" for line in process.sent)


def test_a_driver_without_the_seam_declines(tmp_path):
    process = _LiveProcess()
    process.feed = _lines(
        {
            "id": "state-1",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "pi-a", "sessionFile": "/agent-state/pi-a.jsonl"},
        }
    )
    session = PiDriver(PiRuntime()).launch_live(_context(process, tmp_path))
    session.ensure_started(model="m", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="hello", model="m"))
    sent = len(process.sent)

    assert turn.inject("hi", message_id="apmsg_1") == InputOutcome(False, INPUT_UNSUPPORTED)
    assert len(process.sent) == sent


def test_only_the_drivers_that_wire_it_declare_mid_turn_steering():
    declared = {
        runtime.name: driver(runtime).capabilities().steering
        for driver, runtime in (
            (ClaudeCodeDriver, ClaudeCodeRuntime()),
            (CodexDriver, CodexRuntime()),
            (PiDriver, PiRuntime()),
            (OpenCodeDriver, OpencodeRuntime()),
            (KiloDriver, KiloRuntime()),
        )
    }

    assert declared == {
        "claude-code": "mid_turn",
        "codex": "mid_turn",
        "pi": "none",
        "opencode": "none",
        "kilo": "none",
    }


class _Supervisor:
    """The broker's view of the supervisor: one method, recorded."""

    def __init__(self, outcome: InputOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str, str]] = []

    def inject_input(
        self, thread_id: str, *, owner: str, message_id: str, text: str
    ) -> InputOutcome:
        self.calls.append((thread_id, owner, message_id, text))
        return self.outcome

    def reconcile(self):
        return []


def _broker(supervisor) -> AsyncClient:
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="broker-secret",
        shared_session_supervisor=supervisor,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://broker")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [InputOutcome(True), InputOutcome(False, INPUT_TURN_ENDED)],
)
async def test_the_broker_answers_whether_the_turn_has_it(outcome):
    supervisor = _Supervisor(outcome)
    async with _broker(supervisor) as client:
        response = await client.post(
            "/shared-sandboxes/athr_1/input",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json={"owner": "attempt:7", "message_id": "apmsg_1", "text": "also this"},
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": outcome.accepted, "reason": outcome.reason}
    assert supervisor.calls == [("athr_1", "attempt:7", "apmsg_1", "also this")]


@pytest.mark.asyncio
async def test_the_broker_takes_input_only_from_the_control_plane():
    supervisor = _Supervisor(InputOutcome(True))
    async with _broker(supervisor) as client:
        response = await client.post(
            "/shared-sandboxes/athr_1/input",
            headers={"X-Agent-Workspace-Token": "wrong"},
            json={"owner": "attempt:7", "message_id": "apmsg_1", "text": "also this"},
        )

    assert response.status_code == 401
    assert supervisor.calls == []


def _supervisor(members: dict, now: list[float]) -> SharedChatSessionSupervisor:
    supervisor = SharedChatSessionSupervisor.__new__(SharedChatSessionSupervisor)
    supervisor._lock = threading.RLock()
    supervisor._members = {
        thread_id: SimpleNamespace(members={thread_id: member})
        for thread_id, member in members.items()
    }
    supervisor._held_inputs = {}
    supervisor._clock = lambda: now[0]
    return supervisor


def _recording_turn():
    turn = SimpleNamespace(received=[])
    turn.inject = lambda text, *, message_id: (
        turn.received.append((message_id, text)),
        InputOutcome(True),
    )[1]
    return turn


def test_supervisor_gives_the_message_only_to_the_activation_it_names():
    turn = _recording_turn()
    member = SimpleNamespace(lease_owner="attempt:1", current_turn=turn)
    supervisor = _supervisor({"athr_1": member}, [0.0])

    assert supervisor.inject_input(
        "athr_1", owner="attempt:1", message_id="apmsg_1", text="y"
    ) == InputOutcome(True)
    # Another attempt's message is kept for that attempt, not given to this one.
    assert supervisor.inject_input(
        "athr_1", owner="attempt:2", message_id="apmsg_2", text="x"
    ) == InputOutcome(True)

    assert turn.received == [("apmsg_1", "y")]
    assert supervisor._take_held_inputs("athr_1", "attempt:2") == [PendingInput("apmsg_2", "x")]


def test_a_turn_that_has_not_started_here_gets_its_message_when_it_starts():
    now = [100.0]
    member = SimpleNamespace(lease_owner=None, current_turn=None)
    supervisor = _supervisor({"athr_1": member}, now)

    # Before the member is leased, and for a thread this broker has not seen.
    supervisor.inject_input("athr_1", owner="attempt:3", message_id="apmsg_1", text="a")
    supervisor.inject_input("athr_1", owner="attempt:3", message_id="apmsg_1", text="a")
    supervisor.inject_input("athr_9", owner="attempt:4", message_id="apmsg_2", text="b")
    supervisor.inject_input("athr_1", owner="attempt:2", message_id="apmsg_3", text="stale")

    # The turn that starts takes its own, once; a stale attempt's is dropped.
    assert supervisor._take_held_inputs("athr_1", "attempt:3") == [PendingInput("apmsg_1", "a")]
    assert supervisor._take_held_inputs("athr_1", "attempt:2") == []
    # One that waits too long is left to the control plane.
    now[0] += HELD_INPUT_TTL_S + 1
    assert supervisor._take_held_inputs("athr_9", "attempt:4") == []
