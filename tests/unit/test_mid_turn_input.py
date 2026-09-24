"""Handing the owner's message to a running turn ("send now"), on the host side.

The drivers are driven with their real dialect adapters over a fake live
process whose stdout is fed line by line, exactly as the broker's stream thread
feeds a turn; the message arrives from another thread, as it does in the broker.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cloud_agent_host import drivers
from cloud_agent_host.drivers import (
    LINE,
    ClaudeCodeDriver,
    CodexDriver,
    InputOutcome,
    KiloDriver,
    LaunchContext,
    OpenCodeDriver,
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
from cloud_agent_host.workspace_broker import create_app
from cloud_agent_shared.shared_protocol import (
    INPUT_NO_BOUNDARY,
    INPUT_NO_REPLY,
    INPUT_PROCESS_GONE,
    INPUT_REFUSED,
    INPUT_TURN_ENDED,
    INPUT_UNSUPPORTED,
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


def _tool_use(tool_id: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "sleep 5"}}


def _tool_result(tool_id: str) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]},
    }


_RESULT = {"type": "result", "subtype": "success"}


def _claude_turn(tmp_path):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_context(process, tmp_path))
    turn = session.start_turn(TurnInput(prompt="fix it", model="m"))
    events = turn.iter_events()
    next(events)  # turn started
    return process, turn, events


def _read(process: _LiveProcess, events, *records: dict) -> None:
    """Let the turn's stream read ``records``, as the broker's thread would."""
    process.feed.extend(_lines(*records))
    for _ in records:
        next(events)


def test_claude_takes_a_message_only_while_a_tool_is_running(tmp_path):
    process, turn, events = _claude_turn(tmp_path)
    prompt_lines = len(process.sent)

    # Thinking or answering, nothing is in flight to carry it: it would open a
    # turn of its own after this one's result.
    assert turn.inject("also check the docs") == InputOutcome(False, INPUT_NO_BOUNDARY)
    assert len(process.sent) == prompt_lines

    _read(process, events, _assistant(_tool_use("toolu_1")))
    assert turn.inject("also check the docs") == InputOutcome(True)
    assert process.sent[-1] == ClaudeCodeRuntime().user_message_line("also check the docs")

    _read(process, events, _tool_result("toolu_1"))
    assert turn.inject("one more") == InputOutcome(False, INPUT_NO_BOUNDARY)

    _read(process, events, _RESULT)
    assert turn.inject("too late") == InputOutcome(False, INPUT_NO_BOUNDARY)
    assert len(process.sent) == prompt_lines + 1


def test_claude_counts_every_parallel_tool_call_not_only_the_first(tmp_path):
    process, turn, events = _claude_turn(tmp_path)

    _read(process, events, _assistant(_tool_use("toolu_a"), _tool_use("toolu_b")))
    _read(process, events, _tool_result("toolu_a"))

    # toolu_b is still running; the reported event only named toolu_a.
    assert turn.inject("and the other file") == InputOutcome(True)


def test_claude_refuses_a_message_for_a_turn_that_is_over(tmp_path):
    process, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))

    turn.cancel()

    assert turn.inject("never mind") == InputOutcome(False, INPUT_TURN_ENDED)


def test_claude_reports_a_process_it_cannot_write_to(tmp_path):
    process, turn, events = _claude_turn(tmp_path)
    _read(process, events, _assistant(_tool_use("toolu_1")))
    process.broken = True

    assert turn.inject("hello?") == InputOutcome(False, INPUT_PROCESS_GONE)


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


def _steer_in_background(turn, text: str):
    outcome: list[InputOutcome] = []
    worker = threading.Thread(target=lambda: outcome.append(turn.inject(text)))
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


def test_codex_steers_the_turn_it_saw_running_and_hears_back_through_its_stream(tmp_path):
    process, turn = _codex_turn(tmp_path)

    worker, outcome = _steer_in_background(turn, "also run the linter")
    steer = _sent_steer(process)
    assert steer["params"] == {
        "threadId": "thread-a",
        "expectedTurnId": "turn-a",
        "input": [{"type": "text", "text": "also run the linter"}],
    }
    # The answer arrives in the turn's own stdout; the stream hands it over and
    # does not report it as an event of the turn.
    answer = turn.event_for_line(json.dumps({"id": steer["id"], "result": {"turnId": "turn-a"}}))
    worker.join(5)

    assert answer.event_type == LINE
    assert outcome == [InputOutcome(True)]


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
    turn.event_for_line(
        json.dumps({"id": steer["id"], "error": {"code": -32600, "message": message}})
    )
    worker.join(5)

    assert outcome == [InputOutcome(False, reason)]


def test_an_unanswered_steer_is_not_reported_as_taken_and_its_late_answer_is_no_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(drivers, "STEER_REPLY_TIMEOUT_S", 0.05)
    process, turn = _codex_turn(tmp_path)

    assert turn.inject("anyone there?") == InputOutcome(False, INPUT_NO_REPLY)

    steer = _sent_steer(process)
    late = turn.event_for_line(
        json.dumps({"id": steer["id"], "error": {"code": -32600, "message": "no active turn"}})
    )
    assert late.event_type == LINE


def test_codex_refuses_a_message_for_a_turn_that_is_over(tmp_path):
    process, turn = _codex_turn(tmp_path)
    process.feed = _lines(
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}}
    )
    turn.wait()

    assert turn.inject("too late") == InputOutcome(False, INPUT_TURN_ENDED)
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

    assert turn.inject("hi") == InputOutcome(False, INPUT_UNSUPPORTED)
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
        self.calls: list[tuple[str, str, str]] = []

    def inject_input(self, thread_id: str, *, owner: str, text: str) -> InputOutcome:
        self.calls.append((thread_id, owner, text))
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
    [InputOutcome(True), InputOutcome(False, INPUT_NO_BOUNDARY)],
)
async def test_the_broker_answers_whether_the_turn_took_it(outcome):
    supervisor = _Supervisor(outcome)
    async with _broker(supervisor) as client:
        response = await client.post(
            "/shared-sandboxes/athr_1/input",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json={"owner": "attempt:7", "text": "also this"},
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": outcome.accepted, "reason": outcome.reason}
    assert supervisor.calls == [("athr_1", "attempt:7", "also this")]


@pytest.mark.asyncio
async def test_the_broker_takes_input_only_from_the_control_plane():
    supervisor = _Supervisor(InputOutcome(True))
    async with _broker(supervisor) as client:
        response = await client.post(
            "/shared-sandboxes/athr_1/input",
            headers={"X-Agent-Workspace-Token": "wrong"},
            json={"owner": "attempt:7", "text": "also this"},
        )

    assert response.status_code == 401
    assert supervisor.calls == []


def test_supervisor_hands_the_message_only_to_the_activation_it_names():
    from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor

    turn = SimpleNamespace(received=[])
    turn.inject = lambda text: (turn.received.append(text), InputOutcome(True))[1]
    member = SimpleNamespace(lease_owner="attempt:1", current_turn=turn)
    supervisor = SharedChatSessionSupervisor.__new__(SharedChatSessionSupervisor)
    supervisor._lock = threading.RLock()
    supervisor._member = lambda thread_id: (None, member)

    assert supervisor.inject_input("athr_1", owner="attempt:2", text="x") == InputOutcome(
        False, INPUT_TURN_ENDED
    )
    assert supervisor.inject_input("athr_1", owner="attempt:1", text="y") == InputOutcome(True)
    member.current_turn = None
    assert supervisor.inject_input("athr_1", owner="attempt:1", text="z") == InputOutcome(
        False, INPUT_TURN_ENDED
    )
    assert turn.received == ["y"]
